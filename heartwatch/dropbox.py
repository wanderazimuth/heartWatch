"""Fetch new .FIT files from the Dropbox folder the Wahoo app shares into.

Uses Dropbox's PKCE login, so only the app key is needed (no app secret on
the Pi), and its change cursor, so each poll only lists what's new.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import store

log = logging.getLogger("heartwatch")

AUTHORIZE_URL = "https://www.dropbox.com/oauth2/authorize"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"


class DropboxError(Exception):
    pass


def login_url(app_key: str) -> tuple[str, str]:
    """The URL to open in a browser, and the verifier to finish the login with."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urllib.parse.urlencode({
        "client_id": app_key, "response_type": "code", "token_access_type": "offline",
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return f"{AUTHORIZE_URL}?{query}", verifier


def save_token(path: str | Path, token: str) -> None:
    """Write the refresh token readable only by its owner."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token.strip() + "\n")


@dataclass
class Client:
    app_key: str
    refresh_token: str = ""
    access_token: str = field(default="", repr=False)

    # One place for every HTTP call, so tests can stand in for Dropbox.
    def _request(self, url: str, body: bytes | None, headers: dict[str, str]) -> bytes:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise DropboxError(f"{url}: HTTP {e.code} {e.read()[:300].decode(errors='replace')}") from e

    def _token(self, form: dict[str, str]) -> dict:
        body = urllib.parse.urlencode({"client_id": self.app_key, **form}).encode()
        return json.loads(self._request(TOKEN_URL, body, {"Content-Type": "application/x-www-form-urlencoded"}))

    def finish_login(self, code: str, verifier: str) -> str:
        """Trade the code shown after login for a long-lived refresh token."""
        got = self._token({"grant_type": "authorization_code", "code": code.strip(), "code_verifier": verifier})
        self.refresh_token, self.access_token = got["refresh_token"], got["access_token"]
        return self.refresh_token

    def _auth(self) -> dict[str, str]:
        if not self.access_token:
            self.access_token = self._token({"grant_type": "refresh_token", "refresh_token": self.refresh_token})["access_token"]
        return {"Authorization": f"Bearer {self.access_token}"}

    def rpc(self, endpoint: str, args: dict) -> dict:
        headers = {**self._auth(), "Content-Type": "application/json"}
        return json.loads(self._request(f"{API}/{endpoint}", json.dumps(args).encode(), headers))

    def download(self, path: str) -> bytes:
        # json.dumps escapes non-ASCII, which this header requires.
        headers = {**self._auth(), "Dropbox-API-Arg": json.dumps({"path": path})}
        return self._request(f"{CONTENT}/files/download", b"", headers)

    def changes(self, folder: str, cursor: str | None) -> tuple[list[dict], str]:
        """Files added or changed since cursor (or all of them), and the new cursor."""
        if cursor:
            page = self.rpc("files/list_folder/continue", {"cursor": cursor})
        else:
            page = self.rpc("files/list_folder", {"path": folder, "recursive": True})
        entries = list(page["entries"])
        while page["has_more"]:
            page = self.rpc("files/list_folder/continue", {"cursor": page["cursor"]})
            entries += page["entries"]
        return [e for e in entries if e[".tag"] == "file"], page["cursor"]


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (key, value))


@dataclass
class PollResult:
    seen: int = 0
    added: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def poll(conn: sqlite3.Connection, cfg, client: Client) -> PollResult:
    """Import every new .fit file in the Wahoo folder. The cursor only moves on once all of them are stored."""
    files, cursor = client.changes(cfg.dropbox_folder, get_meta(conn, "dropbox_cursor"))
    result = PollResult()
    for entry in files:
        if not entry["name"].lower().endswith(".fit"):
            continue
        result.seen += 1
        got = store.import_fit(conn, cfg, entry["name"], client.download(entry["path_lower"]))
        if got.status == "added":
            result.added.append(entry["name"])
        elif got.status.startswith("failed"):
            result.failed.append(f"{entry['name']}: {got.status}")
    set_meta(conn, "dropbox_cursor", cursor)
    set_meta(conn, "last_poll", str(int(time.time())))
    if result.added:
        set_meta(conn, "last_new_workout", str(int(time.time())))
        if cfg.heartbeat_url:
            # Tells an outside monitor workouts are still arriving; it alerts when they stop.
            try:
                urllib.request.urlopen(cfg.heartbeat_url, timeout=10).close()
            except Exception as e:
                log.warning("heartbeat ping failed: %s", e)
    return result
