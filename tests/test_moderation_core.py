import unittest
from datetime import timedelta

from moderation_core import CaseStore, hierarchy_problem, human_duration, parse_duration


class DurationTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_duration("10m"), timedelta(minutes=10))
        self.assertEqual(parse_duration("1h30m"), timedelta(hours=1, minutes=30))
        self.assertEqual(parse_duration("2d"), timedelta(days=2))
        self.assertEqual(parse_duration("1w"), timedelta(weeks=1))
        self.assertEqual(parse_duration(" 1H 5M "), timedelta(hours=1, minutes=5))

    def test_bare_number_is_minutes(self):
        self.assertEqual(parse_duration("15"), timedelta(minutes=15))

    def test_rejects_garbage(self):
        for bad in ("", "abc", "10x", "m10", "0m", "1h-5m"):
            with self.assertRaises(ValueError, msg=bad):
                parse_duration(bad)

    def test_human(self):
        self.assertEqual(human_duration(timedelta(hours=1, minutes=30)), "1h 30m")
        self.assertEqual(human_duration(timedelta(days=8)), "1w 1d")


class HierarchyTests(unittest.TestCase):
    def check(self, actor_top=10, target_top=5, bot_top=15, actor=1, target=2, owner=99):
        return hierarchy_problem(actor, actor_top, target, target_top, 50, bot_top, owner)

    def test_normal_case_is_allowed(self):
        self.assertIsNone(self.check())

    def test_cannot_target_self_bot_or_owner(self):
        self.assertIsNotNone(self.check(target=1))
        self.assertIsNotNone(self.check(target=50))
        self.assertIsNotNone(self.check(target=99))

    def test_equal_or_higher_role_is_refused(self):
        self.assertIsNotNone(self.check(actor_top=5, target_top=5))
        self.assertIsNotNone(self.check(actor_top=5, target_top=8))

    def test_owner_outranks_roles_but_not_the_bot_limit(self):
        self.assertIsNone(self.check(actor=99, actor_top=1, target_top=5))
        self.assertIsNotNone(self.check(actor=99, actor_top=1, target_top=20, bot_top=15))


class CaseStoreTests(unittest.TestCase):
    def test_round_trip_and_warning_removal(self):
        store = CaseStore(":memory:")
        a = store.add("warn", 7, "someone", 1, "spam")
        b = store.add("timeout", 7, "someone", 1, "spam again", timedelta(hours=1))
        store.add("warn", 8, "other", 1, "x")
        self.assertEqual([c.id for c in store.for_user(7)], [b.id, a.id])
        self.assertEqual(store.get(b.id).duration_seconds, 3600)
        self.assertEqual(len(store.for_user(7, "warn", active_only=True)), 1)
        self.assertTrue(store.deactivate(a.id))
        self.assertFalse(store.deactivate(a.id), "already inactive")
        self.assertEqual(store.for_user(7, "warn", active_only=True), [])
        self.assertIsNone(store.get(999))


if __name__ == "__main__":
    unittest.main()


from moderation_core import MuteStore, split_duration  # noqa: E402


class MuteTests(unittest.TestCase):
    def test_split_duration(self):
        self.assertEqual(split_duration("2h", "spam"), (timedelta(hours=2), "spam"))
        self.assertEqual(split_duration("spamming", "the chat"), (None, "spamming the chat"))
        self.assertEqual(split_duration(None, "spam"), (None, "spam"))

    def test_store(self):
        import sqlite3
        s = MuteStore(sqlite3.connect(":memory:"))
        s.set(1, 100)
        s.set(2, None)
        self.assertTrue(s.is_muted(1) and s.is_muted(2))
        self.assertEqual(s.due(99), [])
        self.assertEqual(s.due(100), [1])
        self.assertTrue(s.clear(1))
        self.assertFalse(s.clear(1))
        self.assertEqual(s.due(10**12), [], "indefinite mutes never come due")
