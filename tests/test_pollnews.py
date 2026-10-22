import os
import unittest
from datetime import date
from unittest import mock

from pollnews.card import Block, Card, Line, race_tag, render, split_thread, tweet_length
from pollnews.extract import (ExtractError, build_source, cards_from, expand, fetch,
                              sniff_type)
from pollnews.feeds import build_polls
from pollnews.meta import parse_meta
from pollnews.nyt import card_for, new_races, ordered_keys
from pollnews.store import Store

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "pollnews")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def read(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


POLLS = build_polls({k: read(f"{k}.csv") for k in
                     ("senate", "governor", "house", "president", "approval")})
META = parse_meta(read("pollster_ratings.csv"), read("races.csv"))


def find(office, state):
    for p in POLLS.values():
        for k in p.races:
            if k.office == office and k.state == state:
                return p, k
    raise LookupError


class CardTests(unittest.TestCase):
    def test_exact_template(self):
        c = Card(tag="FL25", pollster="Emerson College", sponsor="The Hill", partisan="DEM",
                 blocks=[Block([Line("Moskowitz", "DEM", 100, incumbent=True),
                                Line("Singer", "REP", 0)])],
                 start=date(2026, 10, 1), end=date(2026, 10, 3), n=600, population="lv",
                 url="https://example.com/poll")
        self.assertEqual(render(c), (
            "#FL25 POLL\n\n"
            "Emerson poll, sponsored by The Hill (DEM INTERNAL)\n\n"
            "Moskowitz (D, incumbent) 🔵: 100%\n"
            "Singer (R) 🔴: 0%\n\n"
            "Oct 1-3/n=600/LV\n"
            "Link to poll: https://example.com/poll"))

    def test_approval_third_party_undecided_cross_month(self):
        c = Card(tag="National", pollster="Big Data Poll", partisan="REP",
                 blocks=[Block([Line("Trump approve", "YES", 44.25),
                                Line("Trump disapprove", "NO", 53)]),
                         Block([Line("Smith", "LIB", 3.0), Line("Jones", "America First Party", 2),
                                Line("Undecided", "UND", 7)], "Round 1")],
                 start=date(2026, 9, 28), end=date(2026, 10, 2), population="rv")
        t = render(c)
        self.assertTrue(t.startswith("#National POLL\n\nBig Data Poll (GOP INTERNAL)\n\n"))
        self.assertIn("Trump approve 🟢: 44.2%\nTrump disapprove 🟣: 53%", t)
        self.assertIn("Round 1:\nSmith (L) 🟡: 3%\nJones (AF) 🟤: 2%\nUndecided ⚪: 7%", t)
        self.assertTrue(t.endswith("Sep 28-Oct 2/RV"))

    def test_pollster_short_names_survive_spacing(self):
        c = Card(tag="MESen", pollster="Beacon Research / Shaw & Company Research",
                 sponsor="Fox News")
        self.assertIn("Beacon/Shaw poll, sponsored by Fox News", render(c))

    def test_tags(self):
        for args, tag in [(("HOUSE", "FL", 25), "FL25"), (("HOUSE", "AK", 1), "AKAL"),
                          (("SEN", "FL"), "FLSen"), (("GOV", "FL"), "FLGov"),
                          (("PRES", "FL"), "FLPres"), (("PRES", "US"), "USPres"),
                          (("GCB", "US"), "GenericBallot"), (("APPROVAL", "US"), "National"),
                          (("FAV", "GA"), "GA")]:
            self.assertEqual(race_tag(*args), tag)

    def test_tweet_length(self):
        self.assertEqual(tweet_length("a 🔵 https://" + "x" * 100 + ".com"), 2 + 2 + 1 + 23)
        self.assertEqual(tweet_length("Pérez"), 5)

    def test_split_thread(self):
        text = "\n\n".join(f"Block {i}\n" + "\n".join(f"Name{j} (D) 🔵: 10%" for j in range(6))
                           for i in range(4))
        parts = split_thread(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(tweet_length(p) <= 280 for p in parts))
        self.assertEqual("\n\n".join(parts), text)


class NytTests(unittest.TestCase):
    def test_undecided_and_third_parties_kept(self):
        t = render(card_for(*find("GOV", "NE"), META))
        self.assertIn("#NEGov POLL", t)
        self.assertIn("Pillen (R, incumbent) 🔴: 42%", t)
        self.assertIn("Lindstrom (AF) 🟤: 6%", t)
        self.assertLess(t.index("Beard"), t.index("Undecided"))
        self.assertNotIn("Would not vote", t)

    def test_rcv_round_labels(self):
        t = render(card_for(*find("HOUSE", "AK"), META))
        self.assertTrue(t.startswith("#AKAL POLL"))
        self.assertIn("Round 1:", t)
        self.assertIn("Final round:", t)

    def test_new_races_wait_for_stable_rows(self):
        today, seen = date(2026, 10, 5), set()
        is_seen = lambda pid, i: (pid, i) in seen  # noqa: E731
        d1 = new_races(POLLS, is_seen, {}, META, today)
        self.assertEqual(d1.ready, [])
        d2 = new_races(POLLS, is_seen, d1.pending, META, today)
        self.assertTrue(d2.ready)
        self.assertFalse({pid for pid, _, _ in d2.skipped} & {pid for pid, _ in d2.ready})
        seen |= {(pid, k.ident()) for pid, p in POLLS.items() for k in p.races}
        d3 = new_races(POLLS, is_seen, d2.pending, META, today)
        self.assertEqual((d3.ready, d3.skipped), ([], []))

    def test_senate_first(self):
        for p in POLLS.values():
            offices = [k.office for k in ordered_keys(p.races)]
            if "SEN" in offices:
                self.assertEqual(offices[0], "SEN")


def answer(name, party, pct, inc=False):
    return {"name": name, "party": party, "pct": pct, "incumbent": inc}


def race(office, answers, **kw):
    r = {"office": office, "state": "FL", "district": None, "stage": "general", "subject": None,
         "other_tag": None, "population": "lv", "sample_size": 500,
         "questions": [{"label": None, "answers": answers}]}
    r.update(kw)
    return r


class ExtractTests(unittest.TestCase):
    def test_cards_from_claude_json(self):
        data = {"found": True, "problem": None, "pollster": "Emerson College", "sponsor": "WPEC",
                "partisan": "", "start_date": "2026-10-12", "end_date": "2026-10-14", "url": None,
                "races": [
                    race("HOUSE", [answer("Jared Moskowitz", "DEM", 47, True),
                                   answer("Joe Kaufman", "REP", 44),
                                   answer("Undecided", "UND", 9)], district=23),
                    race("FAV", [answer("Favorable", "YES", 51), answer("Unfavorable", "NO", 45)],
                         subject="Ron DeSantis"),
                    race("OTHER", [answer("Yes", "YES", 55), answer("No", "NO", 40)],
                         other_tag="FL Amendment 4!")]}
        t = [render(c) for c in cards_from(data, META, "https://emerson.example/poll")]
        self.assertTrue(t[0].startswith("#FL23 POLL\n\nEmerson poll, sponsored by WPEC\n\n"))
        self.assertIn("Moskowitz (D, incumbent) 🔵: 47%\nKaufman (R) 🔴: 44%\nUndecided ⚪: 9%", t[0])
        self.assertTrue(t[0].endswith("Oct 12-14/n=500/LV\nLink to poll: https://emerson.example/poll"))
        self.assertIn("DeSantis favorable 🟢: 51%\nDeSantis unfavorable 🟣: 45%", t[1])
        self.assertTrue(t[2].startswith("#FLAmendment4 POLL"))

    def test_not_found_says_why(self):
        with self.assertRaisesRegex(ExtractError, "paywall"):
            cards_from({"found": False, "problem": "The page is behind a paywall", "races": []},
                       META, None)

    def test_fetch_refuses_private_addresses(self):
        for url in ("http://127.0.0.1/x", "http://169.254.169.254/latest/",
                    "http://localhost:8080/", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(ExtractError):
                fetch(url)

    def test_fetch_refuses_parser_tricks(self):
        for url in ("http://evil.example\\@127.0.0.1/", "http://user:pw@example.com/",
                    "http://example.com /x", "ftp://example.com/"):
            with self.subTest(url=url), self.assertRaises(ExtractError):
                fetch(url)

    def test_fetch_connects_to_the_checked_ip(self):
        """The address validated is the address connected to (no second lookup
        to rebind), and a redirect to a private address is refused."""
        seen = []

        def fake_pool(ip, **kw):
            seen.append((ip, kw.get("server_hostname")))
            pool = mock.Mock()
            pool.urlopen.return_value = mock.Mock(
                status=302, headers={"location": "http://10.0.0.5/secret"})
            return pool
        public = [(2, 1, 6, "", ("93.184.215.14", 443))]
        with mock.patch("pollnews.extract.socket.getaddrinfo",
                        side_effect=lambda h, *a, **k: public if h == "pollster.example"
                        else [(2, 1, 6, "", (h, 80))]), \
                mock.patch("pollnews.extract.urllib3.HTTPSConnectionPool", side_effect=fake_pool):
            with self.assertRaisesRegex(ExtractError, "not a public address"):
                fetch("https://pollster.example/poll")
        self.assertEqual(seen, [("93.184.215.14", "pollster.example")])

    def test_counts_image_links_before_downloading(self):
        links = " ".join(f"https://cdn.example/{i}.png" for i in range(6))
        with mock.patch("pollnews.extract.fetch") as f:
            with self.assertRaisesRegex(ExtractError, "at most"):
                build_source(links, [])
        f.assert_not_called()

    def test_expand_follows_tco(self):
        hops = {"https://t.co/abc": (301, {"location": "https://bit.ly/x"}, b""),
                "https://bit.ly/x": (200, {}, b'<META http-equiv="refresh" '
                                     b'content="0;URL=https://pollster.example/release">'),
                "https://t.co/dead": (404, {}, b"")}

        def fake(url):
            status, headers, body = hops[url]
            return mock.Mock(status=status, headers=headers, read=lambda *a, **k: body)
        with mock.patch("pollnews.extract._get_pinned", side_effect=fake):
            self.assertEqual(expand("https://t.co/abc"), "https://pollster.example/release")
            self.assertEqual(expand("https://t.co/dead"), "https://t.co/dead")
        self.assertEqual(expand("https://pollster.example/a"), "https://pollster.example/a")

    def test_pasted_tweet_uses_the_release_not_the_photo(self):
        real = {"https://t.co/pic": "https://twitter.com/Pollster/status/1/photo/1",
                "https://t.co/rel": "https://pollster.example/release"}
        page = ("https://pollster.example/release", b"<html>Smith 48 Jones 44</html>", "text/html")
        with mock.patch("pollnews.extract.expand", side_effect=lambda u: real.get(u, u)), \
                mock.patch("pollnews.extract.fetch", return_value=page) as f:
            s = build_source("New poll! Smith 48 Jones 44 https://t.co/pic https://t.co/rel", [])
        self.assertEqual(s.link, "https://pollster.example/release")
        f.assert_called_once_with("https://pollster.example/release", mock.ANY)

    def test_x_link_alone_asks_for_a_screenshot(self):
        with mock.patch("pollnews.extract.expand",
                        return_value="https://x.com/Pollster/status/1"), \
                mock.patch("pollnews.extract.fetch") as f:
            with self.assertRaisesRegex(ExtractError, "screenshot"):
                build_source("https://t.co/tweet", [])
            s = build_source("https://t.co/tweet", [(PNG, "image/png")])
        f.assert_not_called()
        self.assertEqual(s.link, "https://x.com/Pollster/status/1")

    def test_pasted_text_needs_no_fetch(self):
        s = build_source("Emerson FL-23: Moskowitz 47 Kaufman 44, LV n=500", [])
        self.assertIsNone(s.link)
        self.assertTrue(any("Moskowitz" in b.get("text", "") for b in s.blocks))

    def test_sniff_type(self):
        self.assertEqual(sniff_type(None, "shot.PNG"), "image/png")
        self.assertEqual(sniff_type("application/octet-stream", "x", PNG), "image/png")
        self.assertEqual(sniff_type("image/jpeg; charset=x", ""), "image/jpeg")
        self.assertEqual(sniff_type("", "https://cdn.example/a.webp?ex=1"), "image/webp")

    def test_screenshots_become_image_blocks(self):
        s = build_source("", [(PNG, "image/png"), (PNG, "")])
        self.assertEqual([b["source"]["media_type"] for b in s.blocks if b["type"] == "image"],
                         ["image/png", "image/png"])

    def test_screenshot_plus_link_keeps_link(self):
        page = ("u", b"<html><title>t</title>Poll</html>", "text/html")
        with mock.patch("pollnews.extract.fetch", return_value=page):
            s = build_source("https://pollster.example/release", [(PNG, "image/png")])
        self.assertEqual(s.link, "https://pollster.example/release")
        self.assertEqual([b["type"] for b in s.blocks][:2], ["image", "text"])

    def test_image_link_is_an_image_not_the_poll_link(self):
        with mock.patch("pollnews.extract.fetch", return_value=("u", PNG, "image/png")):
            s = build_source("https://cdn.example/shot.png", [])
        self.assertIsNone(s.link)
        self.assertEqual(s.blocks[0]["type"], "image")

    def test_limits(self):
        with self.assertRaisesRegex(ExtractError, "5 MB"):
            build_source("", [(PNG + b"\x00" * (5 * 1024 * 1024), "image/png")])
        with self.assertRaisesRegex(ExtractError, "at most"):
            build_source("", [(PNG, "image/png")] * 6)
        with self.assertRaises(ExtractError):
            build_source("", [(b"PK..", "application/zip")])


class StoreTests(unittest.TestCase):
    def test_claim_is_single_winner(self):
        s = Store(":memory:")
        s.add_card(1, 9, "text", "nyt", "p", "k")
        self.assertTrue(s.claim(1, 100, ("pending", "failed"), "posting"))
        self.assertFalse(s.claim(1, 200, ("pending", "failed"), "posting"))
        s.finish(1, "failed", None, "boom")
        self.assertTrue(s.claim(1, 200, ("pending", "failed"), "posting"))

    def test_seen(self):
        s = Store(":memory:")
        s.mark_seen([("p", "a"), ("p", "b")])
        s.mark_seen([("p", "a")])
        self.assertTrue(s.is_seen("p", "a") and s.is_seen("p", "b"))
        self.assertFalse(s.is_seen("q", "a"))


if __name__ == "__main__":
    unittest.main()
