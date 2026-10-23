import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pollnews.store import Store
from pollnews.twitter import PostError, XClient


def resp(status, body):
    r = mock.Mock(status_code=status, text=str(body))
    r.json.return_value = body
    return r


def tokens(scopes="tweet.read tweet.write users.read media.write like.write"):
    t = mock.Mock()
    t.access.return_value = "tok"
    t.has_scope.side_effect = lambda s: s in scopes.split()
    return t


class EngageTests(unittest.TestCase):
    def test_like_and_repost_use_our_user_id(self):
        x = XClient(tokens())
        with mock.patch("pollnews.twitter.requests.request",
                        side_effect=[resp(200, {"data": {"id": "999"}}),
                                     resp(200, {"data": {"liked": True}}),
                                     resp(200, {"data": {"retweeted": True}})]) as req:
            self.assertEqual(x.like_and_repost("123"), ({"like", "repost"}, []))
        calls = [(c.args[0], c.args[1], c.kwargs.get("json")) for c in req.call_args_list]
        self.assertEqual(calls, [("GET", "https://api.x.com/2/users/me", None),
                                 ("POST", "https://api.x.com/2/users/999/likes", {"tweet_id": "123"}),
                                 ("POST", "https://api.x.com/2/users/999/retweets",
                                  {"tweet_id": "123"})])

    def test_no_like_scope_still_reposts(self):
        x = XClient(tokens("tweet.read tweet.write users.read"))
        x._me = "999"
        with mock.patch("pollnews.twitter.requests.request",
                        return_value=resp(200, {"data": {"retweeted": True}})) as req:
            did, problems = x.like_and_repost("123")
        self.assertEqual(did, {"repost"})
        self.assertEqual(len(problems), 1)
        self.assertIn("/xlogin", problems[0])
        self.assertEqual(req.call_count, 1)   # only the repost went out

    def test_retry_skips_what_already_worked(self):
        x = XClient(tokens(), me="999")
        with mock.patch("pollnews.twitter.requests.request",
                        return_value=resp(200, {"data": {"liked": True}})) as req:
            self.assertEqual(x.like_and_repost("123", {"repost"}), ({"like", "repost"}, []))
        self.assertEqual(req.call_count, 1)
        self.assertTrue(req.call_args.args[1].endswith("/likes"))

    def test_quotes_by_reads_both_field_names(self):
        x = XClient(tokens())
        body = {"data": [{"id": "5", "referenced_tweets": [{"id": "100", "type": "quoted"}]},
                         {"id": "4", "referenced_posts": [{"id": "101", "type": "quoted"}]},
                         {"id": "3", "referenced_tweets": [{"id": "102", "type": "replied_to"}]},
                         {"id": "2"}],
                "meta": {"newest_id": "5"}}
        with mock.patch("pollnews.twitter.requests.request", return_value=resp(200, body)) as req:
            quotes, newest = x.quotes_by("42", "1")
        self.assertEqual(quotes, [("5", "100"), ("4", "101")])
        self.assertEqual(newest, "5")
        self.assertEqual(req.call_args.kwargs["params"]["since_id"], "1")

    def test_no_new_posts_keeps_since_id(self):
        x = XClient(tokens())
        with mock.patch("pollnews.twitter.requests.request",
                        return_value=resp(200, {"meta": {"result_count": 0}})):
            self.assertEqual(x.quotes_by("42", "77"), ([], "77"))

    def test_http_error_is_post_error(self):
        x = XClient(tokens())
        with mock.patch("pollnews.twitter.requests.request", return_value=resp(429, {})):
            with self.assertRaises(PostError):
                x.user_id("davidslosttt")


class RecentTweetsTests(unittest.TestCase):
    def test_recent_ids_include_thread_replies(self):
        s = Store(":memory:")
        s.add_card(1, 9, "t", "nyt")
        s.claim(1, 5, ("pending",), "posting")
        s.finish(1, "posted", ["10", "11"])
        s.add_card(2, 9, "t", "nyt")
        s.finish(2, "rejected")
        self.assertEqual(s.recent_tweet_ids(7), {"10", "11"})


class QuotePassTests(unittest.TestCase):
    """The cog's quote watcher, with X mocked."""

    def make_cog(self, d):
        import cogs.polpollsnews as M
        bot = mock.Mock()
        with mock.patch.object(M, "DB_PATH", Path(d) / "p.db"), \
                mock.patch.object(M, "DATA", Path(d)):
            cog = M.PolPollsNews(bot)
        cog.x = mock.Mock()
        cog.tweets_enabled = True
        cog.store.add_card(1, 9, "t", "nyt")
        cog.store.claim(1, 5, ("pending",), "posting")
        cog.store.finish(1, "posted", ["100"])
        return cog

    def test_likes_and_reposts_quotes_of_our_posts_once(self):
        with tempfile.TemporaryDirectory() as d:
            cog = self.make_cog(d)
            cog.x.user_id.return_value = "42"
            cog.x.quotes_by.return_value = ([("500", "100"), ("501", "999")], "501")
            cog.x.like_and_repost.return_value = ({"like", "repost"}, [])
            cog.x._me = "999"
            asyncio.run(cog.quote_pass())
            cog.x.like_and_repost.assert_called_once_with("500", set())   # not someone else's quote
            self.assertEqual(cog.store.get_meta("x_user_id"), "999")       # saved for restarts
            self.assertEqual(cog.store.get_meta("quote_since:davidslosttt"), "501")
            cog.x.quotes_by.return_value = ([("500", "100")], "501")   # seen again
            asyncio.run(cog.quote_pass())
            cog.x.like_and_repost.assert_called_once()
            cog.x.user_id.assert_called_once()                       # id cached

    def test_failures_retry_then_give_up_but_login_waits(self):
        with tempfile.TemporaryDirectory() as d:
            cog = self.make_cog(d)
            cog.x.user_id.return_value = "42"
            seen = []
            cog.x.quotes_by.side_effect = lambda uid, since: (
                ([("500", "100")], "500") if not seen.append(1) and len(seen) == 1 else ([], "500"))
            cog.x._me = None
            cog.x.like_and_repost.side_effect = lambda q, did: (
                did | {"repost"}, ["like: the X login predates liking: run /xlogin"])
            for _ in range(7):
                asyncio.run(cog.quote_pass())
            st = json.loads(cog.store.get_meta("quotes_retry"))["500"]
            self.assertEqual(st, {"tries": 0, "done": ["repost"]})       # still waiting, repost kept
            self.assertEqual(cog.x.like_and_repost.call_args.args[1], {"repost"})
            cog.x.like_and_repost.side_effect = lambda q, did: (did, ["like: X HTTP 500"])
            for _ in range(5):
                asyncio.run(cog.quote_pass())
            self.assertNotIn("500", json.loads(cog.store.get_meta("quotes_retry")))

    def test_no_recent_posts_no_api_calls(self):
        with tempfile.TemporaryDirectory() as d:
            import cogs.polpollsnews as M
            with mock.patch.object(M, "DB_PATH", Path(d) / "p.db"), \
                    mock.patch.object(M, "DATA", Path(d)):
                cog = M.PolPollsNews(mock.Mock())
            cog.x = mock.Mock()
            asyncio.run(cog.quote_pass())
            cog.x.quotes_by.assert_not_called()


if __name__ == "__main__":
    unittest.main()
