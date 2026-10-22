import io
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from PIL import Image

from pollnews.card import Block, Card, Line, render, reparse
from pollnews.graphic import context, photo_scope, title
from pollnews.graphic import render as draw
from pollnews.photos import Photos, crop_face
from pollnews.store import Store
from pollnews.twitter import PostError, XClient


def card(**kw):
    c = Card(tag="MEGov", pollster="Beacon Research/Shaw & Company Research", sponsor="Fox News",
             blocks=[Block([Line("Pingree", "DEM", 49, full="Hannah Pingree"),
                            Line("Charles", "REP", 39, full="Bobby Charles"),
                            Line("Bennett", "IND", 9, full="Rick Bennett"),
                            Line("Undecided", "UND", 2)])],
             start=date(2026, 10, 16), end=date(2026, 10, 20), n=1514, population="lv",
             url="https://example.com", office="GOV", state="ME", race_code="ME-GOV",
             stage="general")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def jpeg(w=400, h=500):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 150, 120)).save(buf, "JPEG")
    return buf.getvalue()


class CardEditTests(unittest.TestCase):
    def test_json_round_trip(self):
        c = card()
        self.assertEqual(Card.from_json(c.to_json()), c)

    def test_reparse_reads_edited_numbers_and_keeps_full_names(self):
        c = card()
        text = render(c).replace("Pingree (D) 🔵: 49%", "Pingree (D) 🔵: 51%") \
                        .replace("Charles (R) 🔴: 39%", "Charles (R, incumbent) 🔴: 37.5%")
        new = reparse(text, c)
        lines = new.blocks[0].lines
        self.assertEqual([(l.name, l.party, l.pct) for l in lines[:2]],
                         [("Pingree", "DEM", 51.0), ("Charles", "REP", 37.5)])
        self.assertTrue(lines[1].incumbent)
        self.assertEqual(lines[0].full, "Hannah Pingree")
        self.assertEqual(lines[3].party, "UND")
        self.assertEqual(new.race_code, "ME-GOV")

    def test_reparse_new_candidate_and_tag(self):
        c = card()
        text = render(c).replace("#MEGov", "#MEGovernor").replace(
            "Undecided ⚪: 2%", "Smith (L) 🟡: 2%")
        new = reparse(text, c)
        self.assertEqual(new.tag, "MEGovernor")
        smith = new.blocks[0].lines[-1]
        self.assertEqual((smith.party, smith.full), ("LIB", ""))

    def test_reparse_needs_candidate_lines(self):
        with self.assertRaises(ValueError):
            reparse("#MEGov POLL\n\nSomething else entirely", card())


class GraphicTests(unittest.TestCase):
    def test_renders_with_and_without_photos(self):
        for photo in (lambda c, l: None, lambda c, l: jpeg()):
            png = draw(card(), photo)
            im = Image.open(io.BytesIO(png))
            self.assertEqual(im.size, (1200, 675))

    def test_approval_gcb_primary_render(self):
        approval = card(office="APPROVAL", state="US", subject="Donald Trump", race_code="",
                        blocks=[Block([Line("Trump approve", "YES", 41, full="Donald Trump"),
                                       Line("Trump disapprove", "NO", 59, full="Donald Trump")])])
        gcb = card(office="GCB", state="US", race_code="",
                   blocks=[Block([Line("Democrats", "DEM", 50, full="Generic Democrat"),
                                  Line("Republicans", "REP", 43, full="Generic Republican")])])
        asked = []
        for c in (approval, gcb):
            draw(c, lambda c, l: asked.append(l.full))
        self.assertEqual(asked, ["Donald Trump"])          # never a photo search for the GCB
        self.assertEqual(title(approval), "NATIONAL · TRUMP APPROVAL")
        self.assertEqual(title(card(office="PRES", state="NH", stage="primary",
                                    primary_party="REP")), "NEW HAMPSHIRE · 2028 GOP PRIMARY")
        self.assertEqual(title(card(office="HOUSE", state="NJ", race_code="NJ-02")),
                         "NEW JERSEY · 2ND DISTRICT")

    def test_photo_scope_and_context(self):
        self.assertEqual(photo_scope(card()), "ME")
        self.assertEqual(photo_scope(card(office="PRES", state="NH")), "")
        self.assertEqual(context(card(), card().blocks[0].lines[1]),
                         "Republican candidate, Maine Governor, 2026 election")

    def test_crop_face_is_square(self):
        im = Image.new("RGB", (1200, 600))
        out = Image.open(io.BytesIO(crop_face(im, {"x0": .7, "y0": .2, "x1": .8, "y1": .45})))
        self.assertEqual(out.size, (600, 600))


class PhotoCacheTests(unittest.TestCase):
    def test_found_once_then_cached_and_misses_cached(self):
        with tempfile.TemporaryDirectory() as d:
            p = Photos(Path(d), client=mock.Mock())
            with mock.patch.object(p, "_find", return_value=(jpeg(), "https://x")) as find:
                self.assertTrue(p.headshot("Bobby Charles", "ME", "ctx"))
                self.assertTrue(p.headshot("Bobby  charles", "ME", "ctx"))
            find.assert_called_once()
            again = Photos(Path(d), client=mock.Mock())      # survives a restart
            self.assertTrue(again.cached("Bobby Charles", "ME"))
            with mock.patch.object(p, "_find", return_value=None) as find:
                self.assertIsNone(p.headshot("Nobody Atall", "ME", "ctx"))
                self.assertIsNone(p.headshot("Nobody Atall", "ME", "ctx"))
            find.assert_called_once()

    def test_transient_failure_is_not_cached(self):
        with tempfile.TemporaryDirectory() as d:
            p = Photos(Path(d), client=mock.Mock())
            with mock.patch.object(p, "_find", side_effect=RuntimeError("API down")):
                self.assertIsNone(p.headshot("Bobby Charles", "ME", "ctx"))
            self.assertNotIn(p.key("Bobby Charles", "ME"), p.index)

    def test_no_client_no_search(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(Photos(Path(d), client=None).headshot("A B", "ME", "ctx"))


class StoreMigrationTests(unittest.TestCase):
    def test_old_database_gets_image_columns(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE cards (message_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,"
                       " text TEXT NOT NULL, source TEXT NOT NULL, poll_id TEXT, ident TEXT,"
                       " status TEXT NOT NULL DEFAULT 'pending', tweet_ids TEXT, decided_by INTEGER,"
                       " error TEXT, created_at TEXT NOT NULL, decided_at TEXT)")
            db.execute("INSERT INTO cards (message_id, channel_id, text, source, created_at)"
                       " VALUES (1, 2, 't', 'nyt', 'now')")
            db.commit()
            db.close()
            s = Store(path)
            row = s.card(1)
            self.assertEqual((row["image_on"], row["image_custom"], row["image"]), (1, 0, None))
            s.set_image(1, b"png", custom=True)
            self.assertEqual((s.card(1)["image"], s.card(1)["image_custom"]), (b"png", 1))
            s.db.close()
        finally:
            os.remove(path)


class MediaTests(unittest.TestCase):
    def resp(self, status, body):
        r = mock.Mock(status_code=status, text=str(body))
        r.json.return_value = body
        return r

    def test_image_goes_on_the_first_tweet_only(self):
        tokens = mock.Mock()
        tokens.access.return_value = "t"
        tokens.can_upload_media.return_value = True
        calls = [self.resp(200, {"data": {"id": "M1"}}), self.resp(201, {"data": {"id": "1"}}),
                 self.resp(201, {"data": {"id": "2"}})]
        with mock.patch("pollnews.twitter.requests.post", side_effect=calls) as post:
            self.assertEqual(XClient(tokens).post_thread(["a", "b"], jpeg()), ["1", "2"])
        upload = post.call_args_list[0].kwargs
        self.assertEqual(upload["files"]["media"][2], "image/jpeg")
        self.assertEqual(upload["data"], {"media_category": "tweet_image"})
        self.assertEqual(post.call_args_list[1].kwargs["json"]["media"], {"media_ids": ["M1"]})
        self.assertNotIn("media", post.call_args_list[2].kwargs["json"])

    def test_old_login_without_media_scope_posts_nothing(self):
        tokens = mock.Mock()
        tokens.can_upload_media.return_value = False
        with mock.patch("pollnews.twitter.requests.post") as post:
            with self.assertRaisesRegex(PostError, "xlogin"):
                XClient(tokens).post_thread(["a"], b"\x89PNG...")
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
