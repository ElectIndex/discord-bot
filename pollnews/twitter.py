"""Post to @polpollsnews: X API v2, OAuth 1.0a user context.

Adapted from pollposter.publish.XPublisher. A thread that breaks half-way is
never retried from the top (that would duplicate the root tweet); the ids
that did go out come back with the error.
"""

from __future__ import annotations

import requests
from requests_oauthlib import OAuth1

URL = "https://api.x.com/2/tweets"


class PostError(RuntimeError):
    def __init__(self, msg: str, posted: list[str] | None = None):
        super().__init__(msg)
        self.posted = posted or []


class XClient:
    def __init__(self, api_key: str, api_secret: str, access_token: str, access_secret: str,
                 handle: str = "polpollsnews"):
        self.auth = OAuth1(api_key, api_secret, access_token, access_secret)
        self.handle = handle

    def _post(self, text: str, reply_to: str | None) -> str:
        body: dict = {"text": text}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        r = requests.post(URL, json=body, auth=self.auth, timeout=30)
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
