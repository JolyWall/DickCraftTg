"""Telegram group game. Run with BOT_TOKEN set in the environment."""
import json
import logging
import os
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from reliability import Delivery, TelegramError, transaction

LOG = logging.getLogger(__name__)
GROWTH_CHANCE = 0.70
COMMANDS = {'/писюн', '/топ', '/топ_все', '/чат', '/ролл', '/мой_писюн', '/help', '/start'}
ALIASES = {
    '/grow': '/писюн', '/mine': '/мой_писюн', '/top': '/топ',
    '/top_all': '/топ_все', '/chat': '/чат', '/roll': '/ролл',
}
HELP = ('Команди гри:\n/писюн — змінити довжину раз на день на ±1–10 см\n'
        '/мой_писюн — твоя довжина\n/топ — топ 10\n/топ_все — усі гравці\n'
        'Шанс зростання — 70%, зменшення — 30%.\n'
        '/чат — загальна довжина\n/ролл — число від 1 до 100\n'
        'Новий день — о 00:00 за часовим поясом гри (типово Москва). '
        'Початкова довжина — 0 см; довжина може бути від’ємною. Рейтинги включають гравців, які використали /писюн.')


class Game:
    def __init__(self, path, timezone='Europe/Moscow', rng=None):
        self.zone = ZoneInfo(timezone)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS players '
                        '(chat INTEGER, user INTEGER, name TEXT NOT NULL, length INTEGER NOT NULL DEFAULT 0, '
                        'day TEXT, PRIMARY KEY(chat,user))')
        self.db.commit()
        self.rng = rng or random.SystemRandom()

    def handle(self, message, username='', now=None):
        words = message.get('text', '').split()
        if not words:
            return None
        command, _, target = words[0].partition('@')
        if target and target.casefold() != username.casefold():
            return None
        command = ALIASES.get(command, command)
        if command not in COMMANDS:
            return None
        if command in {'/help', '/start'}:
            return HELP
        if message['chat']['type'] not in {'group', 'supergroup'}:
            return 'Додайте мене до чату: гра та рейтинги працюють у групах. /help — команди.'
        sender = message.get('from', {})
        if not sender or sender.get('is_bot') or message.get('sender_chat'):
            return 'Для гри надішли команду зі свого особистого акаунта.'
        chat, user = message['chat']['id'], sender['id']
        name = ' '.join(sender.get(k, '') for k in ('first_name', 'last_name')).strip() or str(user)
        name = ' '.join(name.split())[:100]
        if command == '/ролл':
            return f'{name}: 🎲 {self.rng.randint(1, 100)}'
        day = (now or datetime.now(self.zone)).astimezone(self.zone).date().isoformat()
        if command == '/писюн':
            with transaction(self.db):
                self.db.execute('INSERT OR IGNORE INTO players(chat,user,name) VALUES(?,?,?)', (chat, user, name))
                length, previous = self.db.execute('SELECT length,day FROM players WHERE chat=? AND user=?', (chat, user)).fetchone()
                self.db.execute('UPDATE players SET name=? WHERE chat=? AND user=?', (name, chat, user))
                if previous == day:
                    return f'{name}, сьогодні ти вже грав. Довжина твого писюна {length} см. Повертайся після опівночі ({self.zone.key}).'
                direction = 1 if self.rng.random() < GROWTH_CHANCE else -1
                delta = direction * self.rng.randint(1, 10)
                self.db.execute('UPDATE players SET length=length+?,day=? WHERE chat=? AND user=?', (delta, day, chat, user))
            change = 'виріс' if delta > 0 else 'зменшився'
            return f'{name}, твій пісюн {change} на {abs(delta)} см. Тепер його довжина {length + delta} см.'
        if command == '/мой_писюн':
            row = self.db.execute('SELECT length FROM players WHERE chat=? AND user=?', (chat, user)).fetchone()
            return f'{name}, довжина твого писюна {row[0]} см.' if row else 'Ти ще не грав у цьому чаті. Почни з /писюн.'
        if command == '/чат':
            total = self.db.execute('SELECT coalesce(sum(length),0) FROM players WHERE chat=?', (chat,)).fetchone()[0]
            return f'Довжина писюна вашого чату {total} см.'
        limit = ' LIMIT 10' if command == '/топ' else ''
        rows = self.db.execute('SELECT name,length FROM players WHERE chat=? ORDER BY length DESC,user ASC' + limit, (chat,)).fetchall()
        return '\n'.join(f'{i}. {n} - {length} см.' for i, (n, length) in enumerate(rows, 1)) or 'У чаті ще немає гравців. Почніть з /писюн.'


class Administration:
    """Owner-only controls and a persistent, default-deny group allowlist."""
    def __init__(self, game, owner_id):
        if owner_id <= 0:
            raise ValueError('OWNER_ID must be a positive Telegram user ID')
        self.game = game
        self.owner_id = owner_id
        with transaction(game.db):
            game.db.execute("CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, "
                            "title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' "
                            "CHECK(status IN ('pending','approved','denied')))")
            columns = {row[1] for row in game.db.execute('PRAGMA table_info(chats)')}
            for name, definition in [('requested_at', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('version', 'INTEGER NOT NULL DEFAULT 1')]:
                if name not in columns:
                    game.db.execute(f'ALTER TABLE chats ADD COLUMN {name} {definition}')
            game.db.execute('CREATE TABLE IF NOT EXISTS decisions '
                            '(id INTEGER PRIMARY KEY,chat INTEGER,at INTEGER,actor INTEGER,old TEXT,new TEXT)')
            game.db.execute('CREATE INDEX IF NOT EXISTS decision_chat ON decisions(chat,id)')
            game.db.execute('CREATE TABLE IF NOT EXISTS confirmations '
                            '(chat INTEGER PRIMARY KEY,version INTEGER,expires INTEGER)')


    def register(self, chat):
        with transaction(self.game.db):
            cursor = self.game.db.execute(
                'INSERT OR IGNORE INTO chats(id,title,requested_at) VALUES(?,?,?)',
                (chat['id'], ' '.join(chat.get('title', str(chat['id'])).split())[:200], int(time.time())))
            created = cursor.rowcount == 1
            self.game.db.execute('UPDATE chats SET title=? WHERE id=?',
                                 (' '.join(chat.get('title', str(chat['id'])).split())[:200], chat['id']))
        return created

    def notify_owner(self, api, chat, repeated=False):
        title = ' '.join(chat.get('title', str(chat['id'])).split())[:200]
        heading = 'Повторная заявка на авторизацию' if repeated else 'Новая заявка на авторизацию'
        try:
            self.send(api, self.owner_id,
                      f"{heading}\nБеседа: {title}\nID: {chat['id']}\n"
                      f"Разрешить: /approve {chat['id']}\nЗапретить: /deny {chat['id']}",
                      {'inline_keyboard': [[{'text': 'Открыть заявку', 'callback_data': f"view:{chat['id']}"}]]})
        except Exception as error:
            # The persisted request remains available even if Telegram blocks the DM.
            LOG.warning('Уведомление о заявке чата %s не доставлено владельцу (%s); проверьте /pending',
                        chat['id'], type(error).__name__)

    def decide(self, action, chat_id):
        status = 'approved' if action == 'approve' else 'denied'
        with transaction(self.game.db):
            previous = self.game.db.execute('SELECT status FROM chats WHERE id=?', (chat_id,)).fetchone()
            if previous and previous[0] != status:
                self.game.db.execute('INSERT INTO decisions(chat,at,actor,old,new) VALUES(?,?,?,?,?)',
                                     (chat_id, int(time.time()), self.owner_id, previous[0], status))
            changed = self.game.db.execute('UPDATE chats SET status=?,version=version+1 WHERE id=?',
                                           (status, chat_id)).rowcount
        return f'Чат {chat_id}: {status}.' if changed else 'Чат не найден.'

    def send(self, api, chat_id, text, markup=None):
        for part in chunks(text):
            api.call('sendMessage', chat_id=chat_id, text=part,
                     reply_markup=markup or {'remove_keyboard': True})

    def panel(self, api, section='all', page=0):
        page = max(0, page)
        where = " WHERE status='pending'" if section == 'pending' else ''
        total = self.game.db.execute('SELECT count(*) FROM chats' + where).fetchone()[0]
        page = min(page, max(0, (total-1)//5))
        rows = self.game.db.execute('SELECT id,title,status FROM chats' + where +
                                    ' ORDER BY requested_at DESC,id LIMIT 5 OFFSET ?', (page*5,)).fetchall()
        keyboard = [[{'text': f'{title[:35]} · {status}', 'callback_data': f'view:{cid}'}]
                    for cid, title, status in rows]
        nav = []
        if page:
            nav.append({'text': '←', 'callback_data': f'list:{section}:{page-1}'})
        if (page+1)*5 < total:
            nav.append({'text': '→', 'callback_data': f'list:{section}:{page+1}'})
        if nav:
            keyboard.append(nav)
        keyboard.append([{'text': 'Все беседы', 'callback_data': 'list:all:0'},
                         {'text': 'Заявки', 'callback_data': 'list:pending:0'}])
        queue = ''
        if self.game.db.execute("SELECT 1 FROM sqlite_master WHERE name='delivery' AND type='table'").fetchone():
            counts = dict(self.game.db.execute('SELECT state,count(*) FROM delivery GROUP BY state'))
            queue = f"Доставка: ожидают {counts.get('pending',0)}, ошибок {counts.get('failed',0)}. /retry_delivery — повторить ошибки.\n"
        self.send(api, self.owner_id, queue + f'Панель владельца · страница {page+1} · бесед: {total}\n'
                  '/find ID — найти беседу; /history ID — история решений.\n' +
                  ('\n'.join(f'{title} | {cid} | {status}' for cid,title,status in rows) or 'Чатов нет.'),
                  {'inline_keyboard': keyboard})

    def detail(self, api, cid):
        row = self.game.db.execute('SELECT title,status,requested_at,version FROM chats WHERE id=?', (cid,)).fetchone()
        if not row:
            self.send(api, self.owner_id, 'Чат не найден.')
            return
        title, status, requested, version = row
        date = datetime.fromtimestamp(requested, self.game.zone).strftime('%d.%m.%Y %H:%M') if requested else 'до обновления'
        self.send(api, self.owner_id, f'{title}\nID: {cid}\nСтатус: {status}\nЗаявка: {date}\n/history {cid}',
                  {'inline_keyboard': [[{'text': 'Разрешить', 'callback_data': f'approve:{cid}:{version}'},
                                        {'text': 'Запретить', 'callback_data': f'confirm:{cid}:{version}'}]]})

    def confirm_denial(self, api, cid, version):
        row = self.game.db.execute('SELECT title,version FROM chats WHERE id=?', (cid,)).fetchone()
        if not row or row[1] != version:
            self.send(api, self.owner_id, 'Данные устарели. Откройте беседу заново через /chats.')
            return
        with transaction(self.game.db):
            self.game.db.execute('INSERT OR REPLACE INTO confirmations VALUES(?,?,?)',
                                 (cid, version, int(time.time())+300))
        self.send(api, self.owner_id, f'Отключить бота в беседе «{row[0]}» ({cid})?',
                  {'inline_keyboard': [[{'text': 'Да, отключить', 'callback_data': f'deny:{cid}:{version}'},
                                        {'text': 'Отмена', 'callback_data': f'cancel:{cid}'}]]})

    def handle(self, update, api, username):
        callback = update.get('callback_query')
        if callback:
            message = callback.get('message', {})
            if (callback.get('from', {}).get('id') != self.owner_id or
                    message.get('chat', {}).get('type') != 'private' or
                    message.get('chat', {}).get('id') != self.owner_id):
                api.call('answerCallbackQuery', callback_query_id=callback['id'],
                         text='Доступ запрещён.', show_alert=True)
                return
            parts = callback.get('data', '').split(':')
            # Callback acknowledgement is immediate; messages themselves are queued.
            api.call('answerCallbackQuery', callback_query_id=callback['id'])
            try:
                action = parts[0]
                if action == 'list' and len(parts) == 3 and parts[1] in {'all', 'pending'}:
                    self.panel(api, parts[1], int(parts[2]))
                    return
                cid = int(parts[1])
                if action == 'view':
                    self.detail(api, cid)
                    return
                if action == 'cancel':
                    with transaction(self.game.db):
                        self.game.db.execute('DELETE FROM confirmations WHERE chat=?', (cid,))
                    self.send(api, self.owner_id, 'Отменено.')
                    return
                row = self.game.db.execute('SELECT version FROM chats WHERE id=?', (cid,)).fetchone()
                if not row or len(parts) != 3 or int(parts[2]) != row[0]:
                    self.send(api, self.owner_id, 'Данные устарели. Откройте беседу заново через /chats.')
                    return
                if action == 'confirm':
                    self.confirm_denial(api, cid, row[0])
                elif action in {'approve', 'deny'}:
                    if action == 'deny':
                        confirmation = self.game.db.execute('SELECT version,expires FROM confirmations WHERE chat=?', (cid,)).fetchone()
                        if not confirmation or confirmation[0] != row[0] or confirmation[1] < time.time():
                            self.send(api, self.owner_id, 'Подтверждение истекло. Откройте беседу заново.')
                            return
                    self.send(api, self.owner_id, self.decide(action, cid))
            except (ValueError, IndexError, OverflowError):
                self.send(api, self.owner_id, 'Некорректная кнопка. Откройте /admin.')
            return

        member = update.get('my_chat_member')
        message = update.get('message', {})
        chat = (member or message).get('chat', {})
        if chat.get('type') in {'group', 'supergroup'}:
            if member and member.get('new_chat_member', {}).get('status') not in {'member', 'administrator'}:
                return
            words = message.get('text', '').split()
            command, _, target = (words[0] if words else '').partition('@')
            if target and target.casefold() != username.casefold():
                return
            if command == '/verify' and not member:
                sender = message.get('from', {})
                if not sender or sender.get('is_bot') or message.get('sender_chat'):
                    self.send(api, chat['id'], 'Подать заявку может владелец или администратор беседы со своего личного аккаунта.')
                    return
                try:
                    membership = api.call('getChatMember', chat_id=chat['id'], user_id=sender['id'])
                except Exception:
                    self.send(api, chat['id'], 'Не удалось проверить права администратора. Попробуйте позже; '
                              'для надёжной проверки бот должен быть администратором беседы.')
                    return
                if membership.get('status') not in {'creator', 'administrator'}:
                    self.send(api, chat['id'], 'Подать заявку может только владелец или администратор беседы.')
                    return
                created = self.register(chat)
                with transaction(self.game.db):
                    reopened = self.game.db.execute(
                        "UPDATE chats SET status='pending',version=version+1,requested_at=? WHERE id=? AND status='denied'",
                        (int(time.time()), chat['id'])).rowcount
                    status = self.game.db.execute('SELECT status FROM chats WHERE id=?',
                                                  (chat['id'],)).fetchone()[0]
                if status == 'approved':
                    text = 'Беседа уже верифицирована. Повторная заявка не требуется.'
                elif created or reopened:
                    self.notify_owner(api, chat, repeated=bool(reopened))
                    text = 'Заявка на верификацию подана. Ожидайте решения владельца бота.'
                else:
                    text = 'Заявка уже ожидает решения владельца бота. Повторная заявка не создана.'
                self.send(api, chat['id'], text)
                return
            relevant = member or command in COMMANDS or command in ALIASES
            if not relevant:
                return
            created = self.register(chat)
            if created:
                self.notify_owner(api, chat)
            status = self.game.db.execute('SELECT status FROM chats WHERE id=?', (chat['id'],)).fetchone()[0]
            if status != 'approved':
                if hasattr(api, 'allow') and not api.allow(f'notice:{chat["id"]}', 1, 60):
                    return
                self.send(api, chat['id'], 'Для работы бота необходима верификация этой беседы. '
                          'Заявка создана автоматически и ожидает одобрения владельцем бота.' if status == 'pending'
                          else 'Владелец запретил работу бота в этом чате. '
                          'Владелец или администратор беседы может подать заявку повторно: /verify.')
                return
            if member:
                return
        elif chat.get('type') == 'private':
            sender = message.get('from', {})
            words = message.get('text', '').split()
            command, _, target = (words[0] if words else '').partition('@')
            if target and target.casefold() != username.casefold():
                return
            if command in {'/admin', '/pending', '/chats', '/approve', '/deny', '/find', '/history', '/retry_delivery'}:
                if sender.get('id') != self.owner_id or chat['id'] != self.owner_id:
                    self.send(api, chat['id'], 'Доступ запрещён.')
                    return
                if command == '/retry_delivery':
                    if self.game.db.execute("SELECT 1 FROM sqlite_master WHERE name='delivery' AND type='table'").fetchone():
                        with transaction(self.game.db):
                            self.game.db.execute("UPDATE delivery SET state='pending',due=?,attempts=0 WHERE state='failed'", (time.time(),))
                    self.send(api, self.owner_id, 'Доставка сообщений с постоянными ошибками запрошена повторно.')
                    return
                if command in {'/admin', '/chats', '/pending'}:
                    self.panel(api, 'pending' if command == '/pending' else 'all',
                               int(words[1])-1 if len(words) == 2 and words[1].isdigit() else 0)
                else:
                    try:
                        cid = int(words[1]) if len(words) == 2 else None
                        if cid is None or not -(2**63) <= cid < 2**63:
                            raise ValueError
                    except ValueError:
                        self.send(api, chat['id'], f'Использование: {command} ID')
                        return
                    if command == '/find':
                        self.detail(api, cid)
                    elif command == '/history':
                        rows = self.game.db.execute('SELECT at,actor,old,new FROM decisions WHERE chat=? ORDER BY id DESC LIMIT 20', (cid,)).fetchall()
                        self.send(api, self.owner_id, '\n'.join(
                            f'{datetime.fromtimestamp(at,self.game.zone):%d.%m.%Y %H:%M} · {actor}: {old} → {new}'
                            for at,actor,old,new in rows) or 'История решений пуста.')
                    elif command == '/deny':
                        row = self.game.db.execute('SELECT status,version FROM chats WHERE id=?', (cid,)).fetchone()
                        if row and row[0] == 'approved':
                            self.confirm_denial(api, cid, row[1])
                        else:
                            self.send(api, self.owner_id, self.decide('deny', cid))
                    else:
                        self.send(api, self.owner_id, self.decide('approve', cid))
                return
        answer = self.game.handle(message, username)
        if answer:
            self.send(api, chat['id'], answer)


class Telegram:
    def __init__(self, token):
        self.base = f'https://api.telegram.org/bot{token}/'

    def call(self, method, **params):
        request = Request(self.base + method, data=json.dumps(params).encode(), headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=40 if method == 'getUpdates' else 10) as response:
                result = json.load(response)
        except HTTPError as error:
            # Do not log exception URLs: they contain the bot token.
            try:
                details = json.load(error)
                delay = int(details.get('parameters', {}).get('retry_after', 0))
            except (ValueError, TypeError):
                delay = 0
            raise TelegramError(error.code, delay) from None
        if not result.get('ok'):
            raise TelegramError(int(result.get('error_code', 500)),
                                int(result.get('parameters', {}).get('retry_after', 0)))
        return result['result']


def chunks(text, limit=3500):
    while text:
        cut = len(text) if len(text) <= limit else text.rfind('\n', 0, limit)
        if cut <= 0:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip('\n')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ.get('BOT_TOKEN')
    if not token:
        raise SystemExit('Задайте BOT_TOKEN через защищённые переменные окружения.')
    try:
        owner_id = int(os.environ.get('OWNER_ID', ''))
        if owner_id <= 0:
            raise ValueError
    except ValueError:
        raise SystemExit('Задайте OWNER_ID: положительный числовой Telegram ID владельца.') from None
    path = Path(os.environ.get('DB_PATH', 'data/game.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    game = Game(path, os.environ.get('GAME_TIMEZONE', 'Europe/Moscow'))
    administration = Administration(game, owner_id)
    api = Telegram(token)
    try:
        me = api.call('getMe')
        if api.call('getWebhookInfo').get('url'):
            raise SystemExit('У бота активен webhook. Используйте отдельного бота или сначала отключите webhook.')
    except Exception as error:
        raise SystemExit(f'Не удалось подключиться к Telegram ({type(error).__name__}). Проверьте токен и сеть.') from None
    try:
        api.call('deleteMyCommands')
    except Exception as error:
        LOG.warning('Не вдалося видалити меню команд (%s)', type(error).__name__)
    LOG.info('Бот @%s запущен', me['username'])
    delivery = Delivery(game.db, api)
    while True:
        try:
            delivery.flush()
            updates = api.call('getUpdates', offset=delivery.offset, timeout=1,
                               allowed_updates=['message', 'my_chat_member', 'callback_query'])
            for update in updates:
                delivery.process(update, administration, me['username'])
            delivery.flush()
        except KeyboardInterrupt:
            break
        except TelegramError as error:
            LOG.warning('Ошибка Telegram HTTP %s', error.code)
            time.sleep(max(5, error.retry_after))
        except Exception as error:
            LOG.warning('Ошибка обработки (%s); повтор через 5 секунд', type(error).__name__)
            time.sleep(5)


if __name__ == '__main__':
    main()
