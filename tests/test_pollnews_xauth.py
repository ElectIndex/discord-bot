import base64
import hashlib
import os
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from pollnews.twitter import PostError, XClient
from pollnews.xauth import REDIRECT_URI, AuthError, Tokens, login_url, parse_callback


def resp(status, body):
    r = mock.Mock(status_code=status, text=str(body))
    r.json.return_value = body
    return r


GRANT = {"access_token": "A1", "refresh_token": "R1", "expires_in": 7200,
         "scope": "tweet.read tweet.write users.read offline.access"}
ME = {"data": {"username": "PolPollsNews"}}


class XAuthTests(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.tokens = Tokens(self.db, "cid", "secret", "@polpollsnews")

    def tearDown(self):
        os.remove(self.db)

    def callback(self, login, code="CODE"):
        return f"{REDIRECT_URI}?state={login.state}&code={code}"

    def test_login_url_is_pkce_s256(self):
        login = login_url("cid")
        q = {k: v[0] for k, v in parse_qs(urlparse(login.url).query).items()}
        want = base64.urlsafe_b64encode(hashlib.sha256(login.verifier.encode()).digest())
        self.assertEqual(q["code_challenge"], want.rstrip(b"=").decode())
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertIn("tweet.write", q["scope"])
        self.assertIn("offline.access", q["scope"])
        self.assertEqual(q["redirect_uri"], REDIRECT_URI)
        self.assertEqual(q["state"], login.state)

    def test_parse_callback(self):
        self.assertEqual(parse_callback(f" {REDIRECT_URI}?state=s&code=c "), ("c", "s"))
        with self.assertRaisesRegex(AuthError, "cancelled"):
            parse_callback(f"{REDIRECT_URI}?error=access_denied&state=s")
        with self.assertRaisesRegex(AuthError, "whole address"):
            parse_callback("CODE")

    def test_finish_saves_and_checks_account(self):
        login = login_url("cid")
        with mock.patch("pollnews.xauth.requests.post", return_value=resp(200, GRANT)) as post, \
                mock.patch("pollnews.xauth.requests.get", return_value=resp(200, ME)):
            self.assertEqual(self.tokens.finish(login, self.callback(login)), "PolPollsNews")
        sent = post.call_args.kwargs
        self.assertEqual(sent["data"]["code_verifier"], login.verifier)
        self.assertEqual(sent["auth"], ("cid", "secret"))
        self.assertEqual(self.tokens.access(), "A1")
        self.assertEqual(self.tokens.status()["username"], "PolPollsNews")

    def test_finish_refuses_the_wrong_account(self):
        login = login_url("cid")
        with mock.patch("pollnews.xauth.requests.post", return_value=resp(200, GRANT)), \
                mock.patch("pollnews.xauth.requests.get",
                           return_value=resp(200, {"data": {"username": "someoneelse"}})):
            with self.assertRaisesRegex(AuthError, "not @polpollsnews"):
                self.tokens.finish(login, self.callback(login))
        self.assertIsNone(self.tokens.status())

    def test_finish_refuses_state_mismatch_expiry_and_read_only(self):
        login = login_url("cid")
        with self.assertRaisesRegex(AuthError, "different login"):
            self.tokens.finish(login, f"{REDIRECT_URI}?state=other&code=c")
        login.started -= 3600
        with self.assertRaisesRegex(AuthError, "expired"):
            self.tokens.finish(login, self.callback(login))
        login = login_url("cid")
        read_only = {**GRANT, "scope": "tweet.read users.read offline.access"}
        with mock.patch("pollnews.xauth.requests.post", return_value=resp(200, read_only)):
            with self.assertRaisesRegex(AuthError, "Read and Write"):
                self.tokens.finish(login, self.callback(login))

    def test_refresh_rotates_and_saves(self):
        login = login_url("cid")
        with mock.patch("pollnews.xauth.requests.post", return_value=resp(200, GRANT)), \
                mock.patch("pollnews.xauth.requests.get", return_value=resp(200, ME)):
            self.tokens.finish(login, self.callback(login))
        rotated = {"access_token": "A2", "refresh_token": "R2", "expires_in": 7200}
        with mock.patch("pollnews.xauth.time.time", return_value=time.time() + 7200), \
                mock.patch("pollnews.xauth.requests.post", return_value=resp(200, rotated)) as post:
            self.assertEqual(self.tokens.access(), "A2")
        self.assertEqual(post.call_args.kwargs["data"]["refresh_token"], "R1")
        self.assertEqual(self.tokens._load()["refresh_token"], "R2")
        self.assertEqual(self.tokens.status()["username"], "PolPollsNews")  # kept

    def test_not_logged_in(self):
        with self.assertRaisesRegex(AuthError, "xlogin"):
            self.tokens.access()


class XClientTests(unittest.TestCase):
    def test_401_refreshes_once_then_posts(self):
        tokens = mock.Mock()
        tokens.access.side_effect = lambda force_refresh=False: "new" if force_refresh else "old"
        x = XClient(tokens)
        with mock.patch("pollnews.twitter.requests.post",
                        side_effect=[resp(401, {}), resp(201, {"data": {"id": "1"}}),
                                     resp(201, {"data": {"id": "2"}})]) as post:
            self.assertEqual(x.post_thread(["a", "b"]), ["1", "2"])
        self.assertEqual(post.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer new")
        self.assertEqual(post.call_args_list[2].kwargs["json"]["reply"],
                         {"in_reply_to_tweet_id": "1"})

    def test_thread_break_reports_what_went_out(self):
        tokens = mock.Mock()
        tokens.access.return_value = "t"
        with mock.patch("pollnews.twitter.requests.post",
                        side_effect=[resp(201, {"data": {"id": "1"}}), resp(403, {})]):
            with self.assertRaises(PostError) as e:
                XClient(tokens).post_thread(["a", "b"])
        self.assertEqual(e.exception.posted, ["1"])

    def test_not_logged_in_is_a_post_error(self):
        from pollnews.xauth import AuthError as AE
        tokens = mock.Mock()
        tokens.access.side_effect = AE("@polpollsnews isn't logged in; run /xlogin")
        with self.assertRaisesRegex(PostError, "xlogin"):
            XClient(tokens).post_thread(["a"])


if __name__ == "__main__":
    unittest.main()
