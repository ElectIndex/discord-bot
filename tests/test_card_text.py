import unittest

from card_text import clusters


class ClusterTests(unittest.TestCase):
    def test_emoji_sequences_stay_whole(self):
        self.assertEqual(clusters("👨‍👩‍👧"), ["👨‍👩‍👧"])  # ZWJ family
        self.assertEqual(clusters("👍🏽"), ["👍🏽"])  # skin tone
        self.assertEqual(clusters("🇺🇸🇬🇧"), ["🇺🇸", "🇬🇧"])  # two flags, not one blob
        self.assertEqual(clusters("1️⃣"), ["1️⃣"])  # keycap
        self.assertEqual(clusters("🗳️"), ["🗳️"])  # variation selector

    def test_combining_marks_attach(self):
        self.assertEqual(clusters("éa"), ["é", "a"])

    def test_plain_text(self):
        self.assertEqual(clusters("Hi 选"), ["H", "i", " ", "选"])


if __name__ == "__main__":
    unittest.main()
