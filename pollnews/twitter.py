"""Post to @polpollsnews: X API v2 with an OAuth 2.0 user token (xauth.py).

A thread that breaks half-way is never retried from the top (that would
duplicate the root tweet); the ids that did go out come back with the error.
"""

from __future__ import annotations

import requests

from .xauth import AuthError, Tokens

URL = "https://api.x.com/2/tweets"
MEDIA_URL = "https://api.x.com/2/media/upload"


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

    def url(self, tweet_id: str) -> str:
        return f"https://x.com/{self.handle}/status/{tweet_id}"
