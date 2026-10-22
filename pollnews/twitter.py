"""Post to @polpollsnews: X API v2 with an OAuth 2.0 user token (xauth.py).

A thread that breaks half-way is never retried from the top (that would
duplicate the root tweet); the ids that did go out come back with the error.
"""

from __future__ import annotations

import requests

from .xauth import AuthError, Tokens

URL = "https://api.x.com/2/tweets"


class PostError(RuntimeError):
    def __init__(self, msg: str, posted: list[str] | None = None):
        super().__init__(msg)
        self.posted = posted or []


class XClient:
    def __init__(self, tokens: Tokens, handle: str = "polpollsnews"):
        self.tokens = tokens
        self.handle = handle.lstrip("@")

    def _send(self, body: dict, token: str) -> requests.Response:
        return requests.post(URL, json=body, headers={"Authorization": f"Bearer {token}"},
                             timeout=30)

    def _post(self, text: str, reply_to: str | None) -> str:
        body: dict = {"text": text}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        try:
            r = self._send(body, self.tokens.access())
            if r.status_code == 401:   # revoked or expired early: refresh once and retry
                r = self._send(body, self.tokens.access(force_refresh=True))
        except AuthError as e:
            raise PostError(str(e)) from e
        if r.status_code != 201:
            raise PostError(f"X HTTP {r.status_code}: {r.text[:300]}")
        return r.json()["data"]["id"]

    def post_thread(self, texts: list[str]) -> list[str]:
        ids: list[str] = []
        for t in texts:
            try:
                ids.append(self._post(t, ids[-1] if ids else None))
            except (PostError, requests.RequestException) as e:
                raise PostError(str(e), ids) from e
        return ids

    def url(self, tweet_id: str) -> str:
        return f"https://x.com/{self.handle}/status/{tweet_id}"
