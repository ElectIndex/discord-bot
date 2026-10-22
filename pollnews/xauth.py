"""@polpollsnews login: X OAuth 2.0 authorization code + PKCE.

1. login_url() gives the X consent screen ("Authorize polpollsnews to use your
   account?"). Log in there as @polpollsnews.
2. X redirects to REDIRECT_URI (http://127.0.0.1:8765/callback). Nothing needs
   to be listening there: the page fails to load, but its address carries the
   one-time code, so it is pasted back to the bot (/xlogin url:...).
3. finish() trades the code for an access token (2 hours) and a refresh token,
   checks they belong to the expected account, and saves them.

After that Tokens.access() refreshes on its own. X rotates the refresh token
on every refresh, so the new pair is saved before the old one is forgotten.

The app must be a confidential client ("Web App, Automated App or Bot") with
REDIRECT_URI listed as a callback URI and Read and Write permissions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

import requests

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
ME_URL = "https://api.x.com/2/users/me"
REDIRECT_URI = "http://127.0.0.1:8765/callback"
SCOPES = "tweet.read tweet.write users.read offline.access"
LOGIN_TTL = 15 * 60          # an unused login link stops working after this
REFRESH_EARLY = 120          # refresh this many seconds before expiry
META_KEY = "x_oauth2"


class AuthError(RuntimeError):
    """Shown to staff as-is."""


@dataclass
class Login:
    state: str
    verifier: str
    url: str
    started: float


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def login_url(client_id: str) -> Login:
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI,
         "scope": SCOPES, "state": state, "code_challenge": _challenge(verifier),
         "code_challenge_method": "S256"}
    return Login(state, verifier, f"{AUTHORIZE_URL}?{urlencode(q)}", time.time())


def parse_callback(pasted: str) -> tuple[str, str]:
    """(code, state) from the pasted address of the page X redirected to."""
    q = parse_qs(urlparse(pasted.strip()).query)
    if "error" in q:
        raise AuthError(f"X said: {q['error'][0]} (login was cancelled or refused)")
    code, state = q.get("code", [""])[0], q.get("state", [""])[0]
    if not code or not state:
        raise AuthError("Paste the whole address of the page X sent you to "
                        "(it starts with http://127.0.0.1:8765/callback?state=...)")
    return code, state


class Tokens:
    """The @polpollsnews token pair, kept in the cog's SQLite meta table.

    Opens its own connection per call: it is used from worker threads
    (tweets post off the event loop) and sqlite connections can't cross
    threads."""

    def __init__(self, db_path: str, client_id: str, client_secret: str, handle: str):
        self.db_path, self.client_id, self.client_secret = db_path, client_id, client_secret
        self.handle = handle.lstrip("@").lower()
        self.lock = threading.Lock()

    # ---- storage
    def _load(self) -> dict | None:
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            r = db.execute("SELECT value FROM meta WHERE key=?", (META_KEY,)).fetchone()
        return json.loads(r[0]) if r else None

    def _save(self, tok: dict | None) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            if tok is None:
                db.execute("DELETE FROM meta WHERE key=?", (META_KEY,))
            else:
                db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (META_KEY, json.dumps(tok)))

    def status(self) -> dict | None:
        t = self._load()
        return None if t is None else {"username": t.get("username"),
                                       "since": t.get("since"), "scope": t.get("scope")}

    def logout(self) -> None:
        self._save(None)

    # ---- token endpoint
    def _token_request(self, data: dict) -> dict:
        try:
            r = requests.post(TOKEN_URL, data={**data, "client_id": self.client_id},
                              auth=(self.client_id, self.client_secret), timeout=30)
        except requests.RequestException as e:
            raise AuthError(f"Couldn't reach X: {e}") from e
        if r.status_code != 200:
            raise AuthError(f"X token endpoint HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        if "access_token" not in body:
            raise AuthError("X returned no access token")
        return body

    @staticmethod
    def _stamp(body: dict, base: dict | None = None) -> dict:
        tok = dict(base or {})
        tok.update(access_token=body["access_token"],
                   refresh_token=body.get("refresh_token") or tok.get("refresh_token"),
                   expires_at=time.time() + float(body.get("expires_in") or 7200),
                   scope=body.get("scope") or tok.get("scope"))
        return tok

    def finish(self, login: Login, pasted: str) -> str:
        """Complete a login. Returns the @username that authorized."""
        code, state = parse_callback(pasted)
        if not secrets.compare_digest(state, login.state):
            raise AuthError("That link is from a different login attempt; run /xlogin again")
        if time.time() - login.started > LOGIN_TTL:
            raise AuthError("That login expired; run /xlogin again")
        body = self._token_request({"grant_type": "authorization_code", "code": code,
                                    "redirect_uri": REDIRECT_URI,
                                    "code_verifier": login.verifier})
        if "tweet.write" not in (body.get("scope") or ""):
            raise AuthError("X didn't grant tweet.write: set the app's permissions to "
                            "Read and Write, then log in again")
        try:
            me = requests.get(ME_URL, headers={"Authorization": f"Bearer {body['access_token']}"},
                              timeout=30)
            username = me.json()["data"]["username"]
        except (requests.RequestException, ValueError, KeyError) as e:
            raise AuthError("Logged in, but couldn't check which account it was") from e
        if self.handle and username.lower() != self.handle:
            raise AuthError(f"You logged in as @{username}, not @{self.handle}. Log out of X "
                            f"in that browser (or use a private window) and try again")
        tok = self._stamp(body, {"username": username,
                                 "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        with self.lock:
            self._save(tok)
        return username

    def access(self, force_refresh: bool = False) -> str:
        """A valid access token, refreshing (and saving the rotated pair) if needed."""
        with self.lock:
            tok = self._load()
            if tok is None:
                raise AuthError("@polpollsnews isn't logged in; run /xlogin")
            if force_refresh or time.time() > tok["expires_at"] - REFRESH_EARLY:
                if not tok.get("refresh_token"):
                    raise AuthError("No refresh token (offline.access wasn't granted); run /xlogin")
                try:
                    body = self._token_request({"grant_type": "refresh_token",
                                                "refresh_token": tok["refresh_token"]})
                except AuthError as e:
                    raise AuthError(f"Refreshing the X login failed; run /xlogin. ({e})") from e
                tok = self._stamp(body, tok)
                self._save(tok)
            return tok["access_token"]
