"""Spotify OAuth (Authorization Code + PKCE) and token storage.

PKCE means no client secret is needed. Tokens are cached on disk and refreshed
automatically. For headless use, set SPOTIFY_REFRESH_TOKEN instead of running
the interactive login.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx2 as httpx

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# Spotify no longer accepts "localhost"; loopback redirects must use 127.0.0.1.
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"

SCOPES = [
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
    "user-library-read",
    "user-top-read",
    "user-read-recently-played",
    "user-read-currently-playing",
    "user-read-private",
]


class AuthError(RuntimeError):
    pass


def token_path() -> Path:
    override = os.environ.get("SPOTIFY_TOKEN_PATH")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "spotify-mcp" / "token.json"


def client_id() -> str:
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    if not cid:
        raise AuthError(
            "SPOTIFY_CLIENT_ID is not set. Create an app at "
            "https://developer.spotify.com/dashboard and export its Client ID."
        )
    return cid


def redirect_uri() -> str:
    return os.environ.get("SPOTIFY_REDIRECT_URI", DEFAULT_REDIRECT_URI)


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at - 60

    def to_json(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_response(cls, data: dict, previous_refresh: str | None = None) -> "Token":
        return cls(
            access_token=data["access_token"],
            # Spotify may omit refresh_token on refresh; keep the old one then.
            refresh_token=data.get("refresh_token") or previous_refresh or "",
            expires_at=time.time() + int(data.get("expires_in", 3600)),
            scope=data.get("scope", ""),
        )


def save_token(token: Token) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token.to_json(), indent=2))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def load_token() -> Token | None:
    path = token_path()
    if path.exists():
        return Token(**json.loads(path.read_text()))
    env_refresh = os.environ.get("SPOTIFY_REFRESH_TOKEN")
    if env_refresh:
        return Token(access_token="", refresh_token=env_refresh, expires_at=0)
    return None


def refresh(token: Token, http: httpx.Client | None = None) -> Token:
    if not token.refresh_token:
        raise AuthError("No refresh token available. Run `spotify-mcp auth`.")
    http = http or httpx.Client(timeout=20)
    resp = http.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": client_id(),
        },
    )
    if resp.status_code != 200:
        raise AuthError(
            f"Token refresh failed ({resp.status_code}): {resp.text}. "
            "Run `spotify-mcp auth` again."
        )
    new = Token.from_response(resp.json(), previous_refresh=token.refresh_token)
    save_token(new)
    return new


class TokenManager:
    """Hands out a valid access token, refreshing as needed (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: Token | None = None

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if self._token is None:
                self._token = load_token()
            if self._token is None:
                raise AuthError(
                    "Not logged in to Spotify. Run `spotify-mcp auth` once in a terminal."
                )
            if force_refresh or self._token.expired or not self._token.access_token:
                self._token = refresh(self._token)
            return self._token.access_token


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def interactive_login(open_browser: bool = True) -> Token:
    """Run the PKCE flow with a one-shot local callback server."""
    cid = client_id()
    redirect = redirect_uri()
    parsed = urllib.parse.urlparse(redirect)
    if parsed.hostname not in ("127.0.0.1", "[::1]", "::1"):
        raise AuthError("SPOTIFY_REDIRECT_URI must be a loopback address like http://127.0.0.1:8888/callback")

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": redirect,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
            "scope": " ".join(SCOPES),
        }
    )

    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path != parsed.path:
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in q.items()})
            ok = "code" in q and q.get("state", [""])[0] == state
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = "Spotify connected. You can close this tab." if ok else "Spotify login failed."
            self.wfile.write(f"<h2>{msg}</h2>".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer((parsed.hostname.strip("[]"), parsed.port or 80), Handler)
    print("Open this URL to authorize Spotify:\n\n  " + url + "\n")
    if open_browser:
        webbrowser.open(url)
    deadline = time.time() + 300
    server.timeout = 1
    while "code" not in result and "error" not in result and time.time() < deadline:
        server.handle_request()
    server.server_close()

    if "error" in result:
        raise AuthError(f"Authorization denied: {result['error']}")
    if result.get("state") != state or "code" not in result:
        raise AuthError("Did not receive a valid authorization code (timed out or state mismatch).")

    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect,
            "client_id": cid,
            "code_verifier": verifier,
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise AuthError(f"Token exchange failed ({resp.status_code}): {resp.text}")
    token = Token.from_response(resp.json())
    save_token(token)
    return token
