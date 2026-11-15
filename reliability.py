"""Transactional update handling, rate limits and durable Telegram delivery."""
from contextlib import contextmanager
import itertools
import json
import time

_SAVEPOINTS = itertools.count()


@contextmanager
def transaction(db):
    name = 'unit_' + str(next(_SAVEPOINTS))
    db.execute('SAVEPOINT ' + name)
    try:
        yield
    except BaseException:
        db.execute('ROLLBACK TO ' + name)
        db.execute('RELEASE ' + name)
        raise
    else:
        db.execute('RELEASE ' + name)


class TelegramError(RuntimeError):
    def __init__(self, code, retry_after=0):
        super().__init__(f'Telegram HTTP {code}')
        self.code = code
        self.retry_after = retry_after


class Delivery:
    def __init__(self, db, api, clock=time.time):
        self.db, self.api, self.clock = db, api, clock
        with transaction(db):
            db.execute('CREATE TABLE IF NOT EXISTS delivery '
                       '(id INTEGER PRIMARY KEY, chat INTEGER NOT NULL, payload TEXT NOT NULL, '
                       'attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL, '
                       "state TEXT NOT NULL DEFAULT 'pending', error INTEGER)")
            db.execute('CREATE INDEX IF NOT EXISTS delivery_due ON delivery(state,due)')
            db.execute('CREATE INDEX IF NOT EXISTS delivery_chat ON delivery(chat,state,id)')
            db.execute('CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY,value REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS rate_limits '
                       '(key TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS rate_started ON rate_limits(started)')

    def call(self, method, **params):
        if method != 'sendMessage':
            try:
                return self.api.call(method, **params)
            except TelegramError as error:
                if method == 'answerCallbackQuery' and error.code == 400:
                    return None  # Expired acknowledgements must not block the update queue.
                raise
        with transaction(self.db):
            self.db.execute('INSERT INTO delivery(chat,payload,due) VALUES(?,?,?)',
                            (params['chat_id'], json.dumps(params, ensure_ascii=False), self.clock()))
        return True

    def allow(self, key, limit, seconds):
        now = self.clock()
        row = self.db.execute('SELECT started,count FROM rate_limits WHERE key=?', (key,)).fetchone()
        if row and now - row[0] < seconds and row[1] >= limit:
            return False
        with transaction(self.db):
            if not row or now - row[0] >= seconds:
                self.db.execute('INSERT OR REPLACE INTO rate_limits VALUES(?,?,1)', (key, now))
            else:
                self.db.execute('UPDATE rate_limits SET count=count+1 WHERE key=?', (key,))
            self.db.execute('DELETE FROM rate_limits WHERE started<?', (now-86400,))
        return True

    @property
    def offset(self):
        row = self.db.execute("SELECT value FROM runtime WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def process(self, update, administration, username):
        if update['update_id'] < self.offset:
            return
        with transaction(self.db):
            message = update.get('message', {})
            chat = message.get('chat', {})
            user = message.get('from', {}).get('id', 0)
            allowed = True
            if message.get('text', '').startswith('/'):
                allowed = (self.allow(f'user:{chat.get("id")}:{user}', 3, 10) and
                           self.allow(f'chat:{chat.get("id")}', 20, 60))
            if allowed:
                administration.handle(update, self, username)
            self.db.execute("INSERT OR REPLACE INTO runtime VALUES('offset',?)", (update['update_id']+1,))

    def flush(self, limit=10):
        now = self.clock()
        cooldown = self.db.execute("SELECT value FROM runtime WHERE key='send_after'").fetchone()
        if cooldown and cooldown[0] > now:
            return
        rows = self.db.execute(
            "SELECT id,payload,attempts FROM delivery d WHERE state='pending' AND due<=? "
            "AND NOT EXISTS(SELECT 1 FROM delivery earlier WHERE earlier.chat=d.chat "
            "AND earlier.state='pending' AND earlier.id<d.id) ORDER BY id LIMIT ?", (now, limit)).fetchall()
        for identifier, payload, attempts in rows:
            try:
                self.api.call('sendMessage', **json.loads(payload))
            except Exception as error:
                code = error.code if isinstance(error, TelegramError) else 0
                permanent = code in {400, 401, 403, 404}
                delay = max(1, error.retry_after) if code == 429 else min(3600, 2 ** min(attempts+1, 12))
                with transaction(self.db):
                    self.db.execute('UPDATE delivery SET attempts=attempts+1,due=?,state=?,error=? WHERE id=?',
                                    (now+delay, 'failed' if permanent else 'pending', code, identifier))
                    if code == 429:
                        self.db.execute("INSERT OR REPLACE INTO runtime VALUES('send_after',?)", (now+delay,))
                if code == 429:
                    break
            else:
                with transaction(self.db):
                    self.db.execute('DELETE FROM delivery WHERE id=?', (identifier,))
        with transaction(self.db):
            self.db.execute("DELETE FROM delivery WHERE state='failed' AND due<?", (now-7*86400,))
