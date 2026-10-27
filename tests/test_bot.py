import random
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from bot import Game, chunks


class GameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'game.db'
        self.game = Game(self.path, rng=random.Random(42))
        self.addCleanup(self.game.db.close)
        self.now = datetime(2026, 10, 27, 20, tzinfo=timezone.utc)

    def run_command(self, command, user=1, chat=-1, now=None):
        return self.game.handle({'text': command, 'chat': {'id': chat, 'type': 'supergroup'},
                                 'from': {'id': user, 'first_name': f'Игрок {user}'}}, 'testbot', now or self.now)

    def test_daily_limit_and_moscow_midnight(self):
        self.run_command('/писюн')
        first = self.run_command('/мой_писюн')
        self.assertIn('вже грав', self.run_command('/писюн'))
        self.assertEqual(first, self.run_command('/мой_писюн'))
        self.assertNotIn('вже грав', self.run_command('/писюн', now=datetime(2026, 10, 27, 21, tzinfo=timezone.utc)))

    def test_chat_isolation(self):
        self.run_command('/писюн')
        self.assertIn('ще не грав', self.run_command('/мой_писюн', chat=-2))
        self.assertEqual('Довжина писюна вашого чату 0 см.', self.run_command('/чат', chat=-2))
        self.assertNotIn('вже грав', self.run_command('/писюн', chat=-2))

    def test_ranking_and_sum(self):
        for user in range(1, 13):
            self.run_command('/писюн', user=user)
        self.assertEqual(len(self.run_command('/топ').splitlines()), 10)
        self.assertEqual(len(self.run_command('/топ_все').splitlines()), 12)
        total = self.game.db.execute('SELECT sum(length) FROM players').fetchone()[0]
        self.assertIn(f'{total} см', self.run_command('/чат'))
        values = [int(line.rsplit(' - ', 1)[1].split()[0]) for line in self.run_command('/топ_все').splitlines()]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_persistence(self):
        self.run_command('/писюн')
        other = Game(self.path)
        try:
            self.assertEqual(other.db.execute('SELECT count(*) FROM players').fetchone()[0], 1)
        finally:
            other.db.close()

    def test_routing_and_roll(self):
        self.assertIsNone(self.run_command('/писюн@otherbot'))
        self.assertIn('см', self.run_command('/писюн@testbot'))
        for _ in range(100):
            value = int(self.run_command('/ролл').split()[-1])
            self.assertTrue(1 <= value <= 100)

    def test_missing_timezone_does_not_open_database(self):
        with patch('bot.ZoneInfo', side_effect=ZoneInfoNotFoundError('missing')):
            with patch('bot.sqlite3.connect') as connect:
                with self.assertRaises(ZoneInfoNotFoundError):
                    Game(self.path)
                connect.assert_not_called()

    def test_menu_aliases_share_results_and_daily_limit(self):
        self.assertIn('см.', self.run_command('/grow@testbot'))
        self.assertIn('вже грав', self.run_command('/писюн'))
        for alias, original in (('/mine', '/мой_писюн'), ('/top', '/топ'),
                                ('/top_all', '/топ_все'), ('/chat', '/чат')):
            self.assertEqual(self.run_command(alias), self.run_command(original))
        self.assertTrue(1 <= int(self.run_command('/roll').split()[-1]) <= 100)
        self.assertIsNone(self.run_command('/grow@otherbot'))

    def test_growth_probability_boundary_and_magnitude(self):
        for user, roll, positive in ((101, 0.0, True), (102, 0.699999, True),
                                     (103, 0.7, False), (104, 0.999999, False)):
            with self.subTest(roll=roll):
                with patch.object(self.game.rng, 'random', return_value=roll):
                    with patch.object(self.game.rng, 'randint', return_value=6) as magnitude:
                        answer = self.run_command('/писюн', user=user)
                magnitude.assert_called_once_with(1, 10)
                length = self.game.db.execute(
                    'SELECT length FROM players WHERE chat=? AND user=?', (-1, user)).fetchone()[0]
                self.assertEqual(length, 6 if positive else -6)
                self.assertIn('виріс' if positive else 'зменшився', answer)

    def test_chunking(self):
        parts = list(chunks('\n'.join(['А' * 100] * 100)))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 3500 for part in parts))


if __name__ == '__main__':
    unittest.main()
