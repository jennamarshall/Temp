"""Thin, defensive Spotify Web API client.

Spotify changed the Web API for Development Mode apps in February 2026:
playlist endpoints moved from /tracks to /items (entries keyed "item" instead
of "track"), some batch endpoints were removed, and fields like popularity can
be missing. This client tries the current endpoint first, falls back to the old
one, and never assumes optional fields exist.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Iterator

import httpx2 as httpx

from .auth import TokenManager

API_BASE = "https://api.spotify.com/v1"
_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_URL_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z-]+/)?(track|album|artist|playlist|episode)/([A-Za-z0-9]{22})")
_URI_RE = re.compile(r"^spotify:(track|album|artist|playlist|episode|local):(.+)$")


class SpotifyAPIError(RuntimeError):
    def __init__(self, status: int, message: str, path: str = ""):
        super().__init__(f"Spotify API {status} on {path}: {message}")
        self.status = status
        self.message = message
        self.path = path


def parse_id(value: str, kind: str = "track") -> str:
    """Accept a raw ID, a spotify: URI, or an open.spotify.com URL."""
    value = value.strip()
    if m := _URI_RE.match(value):
        return m.group(2)
    if m := _URL_RE.search(value):
        return m.group(2)
    if _ID_RE.match(value):
        return value
    raise ValueError(f"Not a Spotify {kind} ID, URI or URL: {value!r}")


def to_uri(value: str, kind: str = "track") -> str:
    value = value.strip()
    if _URI_RE.match(value):
        return value
    if m := _URL_RE.search(value):
        return f"spotify:{m.group(1)}:{m.group(2)}"
    return f"spotify:{kind}:{parse_id(value, kind)}"


def _chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def entry_track(entry: dict) -> dict | None:
    """Playlist entries carry the track under "item" (2026+) or "track" (legacy)."""
    return entry.get("item") or entry.get("track")


class SpotifyClient:
    def __init__(
        self,
        tokens: TokenManager | None = None,
        http: httpx.Client | None = None,
        base_url: str = API_BASE,
        max_retries: int = 4,
    ):
        self.tokens = tokens or TokenManager()
        self.http = http or httpx.Client(timeout=30)
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._legacy_playlist_path = False  # flips to True if /items is unavailable
        self._batch_ok = {"tracks": True, "artists": True}
        self.audio_features_available: bool | None = None
        self._me: dict | None = None

    # ------------------------------------------------------------------ core
    def request(self, method: str, path: str, *, params=None, json=None) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        refreshed = False
        for attempt in range(self.max_retries + 1):
            headers = {"Authorization": f"Bearer {self.tokens.access_token()}"}
            resp = self.http.request(method, url, params=params, json=json, headers=headers)
            if resp.status_code == 401 and not refreshed:
                self.tokens.access_token(force_refresh=True)
                refreshed = True
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < self.max_retries:
                    wait = float(resp.headers.get("Retry-After", 2**attempt))
                    time.sleep(min(wait, 30))
                    continue
            if resp.status_code >= 400:
                try:
                    msg = resp.json().get("error", {})
                    msg = msg.get("message", msg) if isinstance(msg, dict) else msg
                except Exception:
                    msg = resp.text
                raise SpotifyAPIError(resp.status_code, str(msg), path)
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()
        raise SpotifyAPIError(429, "rate limited, retries exhausted", path)

    def paginate(self, path: str, params: dict | None = None, max_items: int | None = None,
                 key: str | None = None) -> list[dict]:
        out: list[dict] = []
        data = self.request("GET", path, params=params)
        while True:
            page = data[key] if key else data
            out.extend(i for i in page.get("items", []) if i is not None)
            if max_items is not None and len(out) >= max_items:
                return out[:max_items]
            nxt = page.get("next")
            if not nxt:
                return out
            data = self.request("GET", nxt)

    # ------------------------------------------------------------------ user
    def me(self) -> dict:
        if self._me is None:
            self._me = self.request("GET", "me")
        return self._me

    def my_playlists(self, max_items: int | None = None) -> list[dict]:
        return self.paginate("me/playlists", {"limit": 50}, max_items)

    def saved_tracks(self, max_items: int | None = 200) -> list[dict]:
        return self.paginate("me/tracks", {"limit": 50}, max_items)

    def top_items(self, kind: str, time_range: str, limit: int) -> list[dict]:
        return self.paginate(f"me/top/{kind}", {"limit": min(limit, 50), "time_range": time_range}, limit)

    def recently_played(self, limit: int = 50) -> list[dict]:
        return self.request("GET", "me/player/recently-played", params={"limit": min(limit, 50)}).get("items", [])

    # ------------------------------------------------------------- playlists
    def playlist(self, playlist_id: str) -> dict:
        return self.request("GET", f"playlists/{parse_id(playlist_id, 'playlist')}")

    def _playlist_call(self, method: str, playlist_id: str, *, params=None, json_new=None, json_old=None):
        pid = parse_id(playlist_id, "playlist")
        if not self._legacy_playlist_path:
            try:
                return self.request(method, f"playlists/{pid}/items", params=params, json=json_new)
            except SpotifyAPIError as e:
                if e.status not in (404, 410):
                    raise
                self._legacy_playlist_path = True
        return self.request(method, f"playlists/{pid}/tracks", params=params,
                            json=json_old if json_old is not None else json_new)

    def playlist_items(self, playlist_id: str, max_items: int | None = None) -> list[dict]:
        first = self._playlist_call("GET", playlist_id, params={"limit": 50, "additional_types": "track"})
        items = [i for i in first.get("items", []) if i]
        nxt = first.get("next")
        while nxt and (max_items is None or len(items) < max_items):
            page = self.request("GET", nxt)
            items.extend(i for i in page.get("items", []) if i)
            nxt = page.get("next")
        return items[:max_items] if max_items else items

    def add_items(self, playlist_id: str, uris: list[str], position: int | None = None) -> list[str]:
        snapshots = []
        for i, chunk in enumerate(_chunks(uris, 100)):
            body: dict[str, Any] = {"uris": chunk}
            if position is not None:
                body["position"] = position + i * 100
            res = self._playlist_call("POST", playlist_id, json_new=body)
            snapshots.append(res.get("snapshot_id", ""))
        return snapshots

    def remove_items(self, playlist_id: str, uris: list[str]) -> list[str]:
        snapshots = []
        for chunk in _chunks(uris, 100):
            objs = [{"uri": u} for u in chunk]
            try:
                res = self._playlist_call("DELETE", playlist_id,
                                          json_new={"items": objs}, json_old={"tracks": objs})
            except SpotifyAPIError as e:
                if e.status != 400 or self._legacy_playlist_path:
                    raise
                # Some deployments of /items still expect the legacy body key.
                res = self._playlist_call("DELETE", playlist_id, json_new={"tracks": objs})
            snapshots.append(res.get("snapshot_id", ""))
        return snapshots

    def create_playlist(self, name: str, description: str = "", public: bool = False) -> dict:
        body = {"name": name, "description": description, "public": public}
        try:
            return self.request("POST", "me/playlists", json=body)
        except SpotifyAPIError as e:
            if e.status not in (404, 405):
                raise
            return self.request("POST", f"users/{self.me()['id']}/playlists", json=body)

    # ---------------------------------------------------------------- catalog
    def search(self, query: str, types: str = "track", limit: int = 10, market: str | None = None) -> dict:
        params: dict[str, Any] = {"q": query, "type": types, "limit": max(1, min(limit, 10))}
        if market:
            params["market"] = market
        return self.request("GET", "search", params=params)

    def _batch_get(self, kind: str, ids: list[str], size: int) -> dict[str, dict]:
        ids = list(dict.fromkeys(ids))
        out: dict[str, dict] = {}
        if self._batch_ok[kind]:
            try:
                for chunk in _chunks(ids, size):
                    data = self.request("GET", kind, params={"ids": ",".join(chunk)})
                    for obj in data.get(kind, []):
                        if obj:
                            out[obj["id"]] = obj
                return out
            except SpotifyAPIError as e:
                if e.status not in (403, 404, 410):
                    raise
                self._batch_ok[kind] = False  # batch endpoint removed for this app

        def one(i: str) -> dict | None:
            try:
                return self.request("GET", f"{kind}/{i}")
            except SpotifyAPIError as e:
                if e.status == 404:
                    return None
                raise

        with ThreadPoolExecutor(max_workers=4) as pool:
            for obj in pool.map(one, [i for i in ids if i not in out]):
                if obj:
                    out[obj["id"]] = obj
        return out

    def tracks(self, ids: Iterable[str]) -> dict[str, dict]:
        return self._batch_get("tracks", [parse_id(i) for i in ids], 50)

    def artists(self, ids: Iterable[str]) -> dict[str, dict]:
        return self._batch_get("artists", [parse_id(i, "artist") for i in ids], 50)

    def audio_features(self, ids: Iterable[str]) -> dict[str, dict]:
        """Deprecated for most apps since Nov 2024; returns {} when unavailable."""
        if self.audio_features_available is False:
            return {}
        out: dict[str, dict] = {}
        try:
            for chunk in _chunks(list(dict.fromkeys(ids)), 100):
                data = self.request("GET", "audio-features", params={"ids": ",".join(chunk)})
                for f in data.get("audio_features", []):
                    if f:
                        out[f["id"]] = f
            self.audio_features_available = True
        except SpotifyAPIError as e:
            if e.status not in (403, 404, 410):
                raise
            self.audio_features_available = False
        return out
