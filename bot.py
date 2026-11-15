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
            with self.db:
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
        with game.db:
            game.db.execute("CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, "
                            "title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' "
                            "CHECK(status IN ('pending','approved','denied')))")

    def register(self, chat):
        with self.game.db:
            cursor = self.game.db.execute(
                'INSERT OR IGNORE INTO chats(id,title) VALUES(?,?)',
                (chat['id'], chat.get('title', str(chat['id']))[:200]))
            created = cursor.rowcount == 1
            self.game.db.execute('UPDATE chats SET title=? WHERE id=?',
                                 (chat.get('title', str(chat['id']))[:200], chat['id']))
        return created

    def notify_owner(self, api, chat, repeated=False):
        title = ' '.join(chat.get('title', str(chat['id'])).split())[:200]
        heading = 'Повторная заявка на авторизацию' if repeated else 'Новая заявка на авторизацию'
        try:
            self.send(api, self.owner_id,
                      f"{heading}\nБеседа: {title}\nID: {chat['id']}\n"
                      f"Разрешить: /approve {chat['id']}\nЗапретить: /deny {chat['id']}")
        except Exception as error:
            # The persisted request remains available even if Telegram blocks the DM.
            LOG.warning('Уведомление о заявке чата %s не доставлено владельцу (%s); проверьте /pending',
                        chat['id'], type(error).__name__)

    def buttons(self, chat_id):
        return {'inline_keyboard': [[
            {'text': 'Разрешить', 'callback_data': f'approve:{chat_id}'},
            {'text': 'Запретить', 'callback_data': f'deny:{chat_id}'}]]}

    def decide(self, action, chat_id):
        status = 'approved' if action == 'approve' else 'denied'
        with self.game.db:
            changed = self.game.db.execute('UPDATE chats SET status=? WHERE id=?',
                                           (status, chat_id)).rowcount
        return f'Чат {chat_id}: {status}.' if changed else 'Чат не найден.'

    def send(self, api, chat_id, text, markup=None):
        for part in chunks(text):
            api.call('sendMessage', chat_id=chat_id, text=part,
                     reply_markup=markup or {'remove_keyboard': True})

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
            action, _, value = callback.get('data', '').partition(':')
            try:
                chat_id = int(value)
            except ValueError:
                return
            if action not in {'approve', 'deny'}:
                return
            result = self.decide(action, chat_id)
            api.call('answerCallbackQuery', callback_query_id=callback['id'], text=result)
            self.send(api, self.owner_id, result)
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
                with self.game.db:
                    reopened = self.game.db.execute(
                        "UPDATE chats SET status='pending' WHERE id=? AND status='denied'",
                        (chat['id'],)).rowcount
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
            if command in {'/admin', '/pending', '/chats', '/approve', '/deny'}:
                if sender.get('id') != self.owner_id or chat['id'] != self.owner_id:
                    self.send(api, chat['id'], 'Доступ запрещён.')
                    return
                if command == '/admin':
                    self.send(api, chat['id'], 'Панель владельца\n/pending — заявки\n/chats — все чаты\n'
                              '/approve ID — разрешить\n/deny ID — запретить (в том числе ранее разрешённый чат)')
                elif command in {'/pending', '/chats'}:
                    query = 'SELECT id,title,status FROM chats'
                    if command == '/pending':
                        query += " WHERE status='pending'"
                    rows = self.game.db.execute(query + ' ORDER BY id').fetchall()
                    self.send(api, chat['id'], '\n'.join(f'{title} | {cid} | {status}'
                              for cid, title, status in rows) or 'Чатов нет.')
                else:
                    try:
                        cid = int(words[1]) if len(words) == 2 else None
                    except ValueError:
                        cid = None
                    self.send(api, chat['id'], self.decide(command[1:], cid) if cid is not None
                              else f'Использование: {command} ID')
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
            with urlopen(request, timeout=40) as response:
                result = json.load(response)
        except HTTPError as error:
            # Do not log exception URLs: they contain the bot token.
            raise RuntimeError(f'Telegram HTTP {error.code}') from None
        if not result.get('ok'):
            raise RuntimeError('Telegram rejected request')
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
    offset = 0
    while True:
        try:
            updates = api.call('getUpdates', offset=offset, timeout=25,
                               allowed_updates=['message', 'my_chat_member', 'callback_query'])
            for update in updates:
                try:
                    administration.handle(update, api, me['username'])
                except Exception as error:
                    LOG.warning('Ошибка обновления %s (%s)', update['update_id'], type(error).__name__)
                # A failed reply must not block approval requests and other chats.
                offset = update['update_id'] + 1
        except KeyboardInterrupt:
            break
        except Exception as error:
            LOG.warning('Ошибка обработки (%s); повтор через 5 секунд', type(error).__name__)
            time.sleep(5)


if __name__ == '__main__':
    main()
