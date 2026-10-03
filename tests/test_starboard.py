import unittest

from starboard_core import StarStore, board_action, star_count


class StarCountTests(unittest.TestCase):
    def test_author_and_bots_do_not_count(self):
        self.assertEqual(star_count([1, 2, 3, 99, 50], author_id=99, bot_ids={50}), 3)

    def test_duplicates_count_once(self):
        self.assertEqual(star_count([1, 1, 2], author_id=9, bot_ids=set()), 2)


class ActionTests(unittest.TestCase):
    def test_threshold_is_three(self):
        self.assertEqual(board_action(2, posted=False), "none")
        self.assertEqual(board_action(3, posted=False), "post")
        self.assertEqual(board_action(5, posted=True), "update")
        self.assertEqual(board_action(2, posted=True), "remove")


class StoreTests(unittest.TestCase):
    def test_round_trip(self):
        s = StarStore(":memory:")
        self.assertIsNone(s.get(1))
        s.put(1, 100)
        self.assertEqual(s.get(1), 100)
        self.assertTrue(s.is_board_message(100))
        s.drop(1)
        self.assertIsNone(s.get(1))
        self.assertFalse(s.is_board_message(100))


if __name__ == "__main__":
    unittest.main()
