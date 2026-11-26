"""In-memory fake of the Spotify Web API for tests (supports 2026 and legacy shapes)."""

from __future__ import annotations

import json

import httpx2 as httpx

from spotify_mcp.client import SpotifyClient


class StaticTokens:
    def __init__(self):
        self.refreshes = 0

    def access_token(self, force_refresh: bool = False) -> str:
        if force_refresh:
            self.refreshes += 1
        return f"tok{self.refreshes}"


def artist(i, name):
    return {"id": f"ar{i:020d}", "name": name}


def track(i, name, art, year, *, explicit=False, popularity=None, duration=210000, album=None):
    t = {
        "id": f"tr{i:020d}", "uri": f"spotify:track:tr{i:020d}", "type": "track", "name": name,
        "artists": [art], "explicit": explicit, "duration_ms": duration,
        "album": {"name": album or f"{name} LP", "release_date": f"{year}-01-01"},
        "external_urls": {"spotify": f"https://open.spotify.com/track/tr{i:020d}"},
    }
    if popularity is not None:
        t["popularity"] = popularity
    return t


A = {
    "daft": artist(1, "Daft Punk"), "justice": artist(2, "Justice"), "air": artist(3, "Air"),
    "phoenix": artist(4, "Phoenix"), "dolly": artist(5, "Dolly Parton"), "cash": artist(6, "Johnny Cash"),
}
GENRES = {
    A["daft"]["id"]: ["french house", "electro"], A["justice"]["id"]: ["french house", "electro", "nu disco"],
    A["air"]["id"]: ["french electronic", "downtempo"], A["phoenix"]["id"]: ["french indie pop", "electro"],
    A["dolly"]["id"]: ["country", "classic country"], A["cash"]["id"]: ["country", "outlaw country"],
}
PLAYLIST_TRACKS = [
    track(1, "One More Time", A["daft"], 2000),
    track(2, "Digital Love", A["daft"], 2001),
    track(3, "D.A.N.C.E.", A["justice"], 2007),
    track(4, "Genesis", A["justice"], 2007),
    track(5, "La Femme d'Argent", A["air"], 1998),
    track(6, "1901", A["phoenix"], 2009),
    track(7, "Lisztomania", A["phoenix"], 2009),
    track(8, "One More Time - Radio Edit", A["daft"], 2001, album="Singles"),
]
CATALOG = {t["id"]: t for t in PLAYLIST_TRACKS}
EXTRA = [
    track(20, "Around the World", A["daft"], 1997),
    track(21, "Jolene", A["dolly"], 1973),
    track(22, "Hurt", A["cash"], 2002, explicit=True),
    track(23, "Sexy Boy", A["air"], 1998),
]
CATALOG.update({t["id"]: t for t in EXTRA})
PID = "pl" + "0" * 20


class FakeSpotify:
    def __init__(self, *, modern=True, batch=False, audio=False, page_size=3):
        self.modern = modern
        self.batch = batch
        self.audio = audio
        self.page_size = page_size
        self.playlist = [dict(t) for t in PLAYLIST_TRACKS]
        self.calls: list[tuple[str, str]] = []
        self.fail_once: dict[str, int] = {}

    def client(self) -> SpotifyClient:
        return SpotifyClient(tokens=StaticTokens(), http=httpx.Client(transport=httpx.MockTransport(self.handle)),
                             base_url="https://api.test/v1")

    def _json(self, data, status=200, headers=None):
        return httpx.Response(status, json=data, headers=headers)

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/v1/")
        self.calls.append((req.method, path))
        q = dict(req.url.params)
        if path in self.fail_once and self.fail_once[path] > 0:
            self.fail_once[path] -= 1
            return self._json({"error": {"status": 429}}, 429, {"Retry-After": "0"})
        if path == "me":
            if req.headers["Authorization"] == "Bearer tok0" and "expire_me" in self.fail_once:
                return self._json({"error": {"status": 401, "message": "expired"}}, 401)
            return self._json({"id": "me", "display_name": "Me"})
        if path == "me/playlists" and req.method == "GET":
            pl = {"id": PID, "name": "French Touch", "owner": {"id": "me"}, "public": False,
                  "collaborative": False, ("items" if self.modern else "tracks"): {"total": len(self.playlist)}}
            return self._json({"items": [pl], "next": None})
        if path == f"playlists/{PID}":
            return self._json({"id": PID, "name": "French Touch", "owner": {"id": "me"}})
        sub = f"playlists/{PID}/items" if self.modern else f"playlists/{PID}/tracks"
        if path.startswith(f"playlists/{PID}/") and path != sub:
            return self._json({"error": {"status": 404, "message": "Not found"}}, 404)
        if path == sub:
            key = "item" if self.modern else "track"
            if req.method == "GET":
                off = int(q.get("offset", 0))
                chunk = self.playlist[off: off + self.page_size]
                nxt = (f"https://api.test/v1/{sub}?offset={off + self.page_size}"
                       if off + self.page_size < len(self.playlist) else None)
                return self._json({"items": [{key: t, "added_at": "2024-01-01T00:00:00Z"} for t in chunk],
                                   "next": nxt, "total": len(self.playlist)})
            body = json.loads(req.content or b"{}")
            if req.method == "POST":
                new = [CATALOG[u.split(":")[-1]] for u in body["uris"]]
                pos = body.get("position", len(self.playlist))
                self.playlist[pos:pos] = new
                return self._json({"snapshot_id": "snap"}, 201)
            if req.method == "DELETE":
                objs = body.get("items" if self.modern else "tracks")
                if objs is None:
                    return self._json({"error": {"status": 400, "message": "missing"}}, 400)
                drop = {o["uri"] for o in objs}
                self.playlist = [t for t in self.playlist if t["uri"] not in drop]
                return self._json({"snapshot_id": "snap2"})
        if path == "tracks":
            if not self.batch:
                return self._json({"error": {"status": 403, "message": "Forbidden"}}, 403)
            return self._json({"tracks": [CATALOG.get(i) for i in q["ids"].split(",")]})
        if path.startswith("tracks/"):
            t = CATALOG.get(path.split("/")[1])
            return self._json(t) if t else self._json({"error": {"status": 404}}, 404)
        if path == "artists":
            return self._json({"error": {"status": 403, "message": "Forbidden"}}, 403)
        if path.startswith("artists/"):
            aid = path.split("/")[1]
            name = next(a["name"] for a in A.values() if a["id"] == aid)
            return self._json({"id": aid, "name": name, "genres": GENRES.get(aid, [])})
        if path == "audio-features":
            if not self.audio:
                return self._json({"error": {"status": 403, "message": "Forbidden"}}, 403)
            return self._json({"audio_features": [{"id": i, "energy": 0.8} for i in q["ids"].split(",")]})
        if path == "search":
            needle = q["q"].lower()
            hits = [t for t in CATALOG.values()
                    if t["name"].lower() in needle or needle in t["name"].lower()]
            return self._json({"tracks": {"items": hits[: int(q.get("limit", 10))]}})
        return self._json({"error": {"status": 404, "message": f"unhandled {path}"}}, 404)
