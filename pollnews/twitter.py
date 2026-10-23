"""Post to @polpollsnews: X API v2 with an OAuth 2.0 user token (xauth.py).

A thread that breaks half-way is never retried from the top (that would
duplicate the root tweet); the ids that did go out come back with the error.
"""

from __future__ import annotations

import requests

from .xauth import AuthError, Tokens

URL = "https://api.x.com/2/tweets"
MEDIA_URL = "https://api.x.com/2/media/upload"
API = "https://api.x.com/2"


def _mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return "image/png"


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

    def upload_image(self, png: bytes) -> str:
        """Upload one image; returns its media id. Needs the media.write scope."""
        if not self.tokens.can_upload_media():
            raise PostError("The X login predates image posting: run /xlogin again to allow "
                            "it (or turn this card's image off)")
        try:
            token = self.tokens.access()
            r = requests.post(MEDIA_URL, headers={"Authorization": f"Bearer {token}"},
                              files={"media": ("poll", png, _mime(png))},
                              data={"media_category": "tweet_image"}, timeout=60)
        except (AuthError, requests.RequestException) as e:
            raise PostError(f"Image upload failed: {e}") from e
        if r.status_code not in (200, 201):
            raise PostError(f"Image upload failed: X HTTP {r.status_code}: {r.text[:300]}")
        return r.json()["data"]["id"]

    def _post(self, text: str, reply_to: str | None, media_id: str | None = None) -> str:
        body: dict = {"text": text}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        if media_id:
            body["media"] = {"media_ids": [media_id]}
        try:
            r = self._send(body, self.tokens.access())
            if r.status_code == 401:   # revoked or expired early: refresh once and retry
                r = self._send(body, self.tokens.access(force_refresh=True))
        except AuthError as e:
            raise PostError(str(e)) from e
        if r.status_code != 201:
            raise PostError(f"X HTTP {r.status_code}: {r.text[:300]}")
        return r.json()["data"]["id"]

    def post_thread(self, texts: list[str], image: bytes | None = None) -> list[str]:
        """The image (if any) goes on the first tweet; it's uploaded before
        anything is posted, so a failed upload posts nothing."""
        media_id = self.upload_image(image) if image else None
        ids: list[str] = []
        for t in texts:
            try:
                ids.append(self._post(t, ids[-1] if ids else None, None if ids else media_id))
            except (PostError, requests.RequestException) as e:
                raise PostError(str(e), ids) from e
        return ids

    # ---- engagement

    def _call(self, method: str, path: str, **kw) -> requests.Response:
        try:
            r = requests.request(method, API + path, timeout=30, headers={
                "Authorization": f"Bearer {self.tokens.access()}"}, **kw)
            if r.status_code == 401:
                r = requests.request(method, API + path, timeout=30, headers={
                    "Authorization": f"Bearer {self.tokens.access(force_refresh=True)}"}, **kw)
        except (AuthError, requests.RequestException) as e:
            raise PostError(str(e)) from e
        if r.status_code >= 300:
            raise PostError(f"X HTTP {r.status_code} on {path}: {r.text[:300]}")
        return r

    def me_id(self) -> str:
        if not getattr(self, "_me", None):
            self._me = self._call("GET", "/users/me").json()["data"]["id"]
        return self._me

    def user_id(self, username: str) -> str:
        return self._call("GET", f"/users/by/username/{username.lstrip('@')}").json()["data"]["id"]

    def like(self, tweet_id: str) -> None:
        if not self.tokens.has_scope("like.write"):
            raise PostError("the X login predates liking: run /xlogin again to allow it")
        self._call("POST", f"/users/{self.me_id()}/likes", json={"tweet_id": tweet_id})

    def repost(self, tweet_id: str) -> None:
        self._call("POST", f"/users/{self.me_id()}/retweets", json={"tweet_id": tweet_id})

    def like_and_repost(self, tweet_id: str) -> list[str]:
        """Both, independently; returns what failed (empty = both done)."""
        problems = []
        for what, fn in (("like", self.like), ("repost", self.repost)):
            try:
                fn(tweet_id)
            except PostError as e:
                problems.append(f"{what}: {e}")
        return problems

    def quotes_by(self, user_id: str, since_id: str | None) -> tuple[list[tuple[str, str]], str | None]:
        """(quote id, quoted id) for the user's posts after since_id, newest
        first, and the newest id seen (the next since_id)."""
        params = {"max_results": 20, "exclude": "retweets,replies",
                  "tweet.fields": "referenced_tweets"}
        if since_id:
            params["since_id"] = since_id
        body = self._call("GET", f"/users/{user_id}/tweets", params=params).json()
        out = []
        for t in body.get("data") or []:
            refs = t.get("referenced_tweets") or t.get("referenced_posts") or []
            out += [(t["id"], r["id"]) for r in refs if r.get("type") == "quoted"]
        return out, (body.get("meta") or {}).get("newest_id") or since_id

    def url(self, tweet_id: str) -> str:
        return f"https://x.com/{self.handle}/status/{tweet_id}"
