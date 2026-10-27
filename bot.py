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
COMMANDS = {'/писюн', '/топ', '/топ_все', '/чат', '/ролл', '/мой_писюн', '/help', '/start'}
HELP = ('Команды игры:\n/писюн — изменить длину раз в день на ±1–10 см\n'
        '/мой_писюн — ваша длина\n/топ — топ 10\n/топ_все — все игроки\n'
        '/чат — суммарная длина\n/ролл — число от 1 до 100\n'
        'Новый день — в 00:00 по часовому поясу игры (по умолчанию Москва). '
        'Начальная длина — 0 см; длина может быть отрицательной. Рейтинги включают игроков, использовавших /писюн.')


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
        if command not in COMMANDS:
            return None
        if command in {'/help', '/start'}:
            return HELP
        if message['chat']['type'] not in {'group', 'supergroup'}:
            return 'Добавьте меня в беседу: игра и рейтинги работают в группах. /help — команды.'
        sender = message.get('from', {})
        if not sender or sender.get('is_bot') or message.get('sender_chat'):
            return 'Для игры отправьте команду от своего личного аккаунта.'
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
                    return f'{name}, сегодня вы уже играли. Длина: {length} см. Приходите после полуночи ({self.zone.key}).'
                delta = self.rng.choice((-1, 1)) * self.rng.randint(1, 10)
                self.db.execute('UPDATE players SET length=length+?,day=? WHERE chat=? AND user=?', (delta, day, chat, user))
            return f'{name}: {delta:+d} см. Теперь {length + delta} см!'
        if command == '/мой_писюн':
            row = self.db.execute('SELECT length FROM players WHERE chat=? AND user=?', (chat, user)).fetchone()
            return f'{name}: {row[0]} см.' if row else 'Вы ещё не играли в этой беседе. Начните с /писюн.'
        if command == '/чат':
            total, count = self.db.execute('SELECT coalesce(sum(length),0),count(*) FROM players WHERE chat=?', (chat,)).fetchone()
            return f'Суммарная длина: {total} см. Игроков: {count}.'
        limit = ' LIMIT 10' if command == '/топ' else ''
        rows = self.db.execute('SELECT name,length FROM players WHERE chat=? ORDER BY length DESC,user ASC' + limit, (chat,)).fetchall()
        return '\n'.join(f'{i}. {n}: {length} см' for i, (n, length) in enumerate(rows, 1)) or 'Пока нет игроков. Начните с /писюн.'


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
    path = Path(os.environ.get('DB_PATH', 'data/game.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    game = Game(path, os.environ.get('GAME_TIMEZONE', 'Europe/Moscow'))
    api = Telegram(token)
    try:
        me = api.call('getMe')
        if api.call('getWebhookInfo').get('url'):
            raise SystemExit('У бота активен webhook. Используйте отдельного бота или сначала отключите webhook.')
    except Exception as error:
        raise SystemExit(f'Не удалось подключиться к Telegram ({type(error).__name__}). Проверьте токен и сеть.') from None
    LOG.info('Бот @%s запущен', me['username'])
    offset = 0
    while True:
        try:
            updates = api.call('getUpdates', offset=offset, timeout=25, allowed_updates=['message'])
            for update in updates:
                message = update.get('message', {})
                answer = game.handle(message, me['username'])
                if answer:
                    for part in chunks(answer):
                        api.call('sendMessage', chat_id=message['chat']['id'], text=part)
                offset = update['update_id'] + 1
        except KeyboardInterrupt:
            break
        except Exception as error:
            LOG.warning('Ошибка обработки (%s); повтор через 5 секунд', type(error).__name__)
            time.sleep(5)


if __name__ == '__main__':
    main()
