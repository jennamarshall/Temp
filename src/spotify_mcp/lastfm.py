"""Optional Last.fm enrichment for genre/mood tags.

Spotify's artist genres are sparse (and restricted for some apps), so when
LASTFM_API_KEY is set we pull community tags per artist as a genre signal.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import httpx2 as httpx

API = "https://ws.audioscrobbler.com/2.0/"
_JUNK = {"seen live", "favorites", "favourite", "favorite", "my favorite", "love", "awesome",
         "beautiful", "albums i own", "under 2000 listeners", "spotify", "check out"}


class LastFM:
    def __init__(self, api_key: str | None = None, http: httpx.Client | None = None):
        self.api_key = api_key or os.environ.get("LASTFM_API_KEY")
        self.http = http or httpx.Client(timeout=15)
        self._cache: dict[str, list[str]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def artist_tags(self, name: str, top: int = 6) -> list[str]:
        key = name.lower()
        if key in self._cache:
            return self._cache[key]
        tags: list[str] = []
        try:
            r = self.http.get(API, params={"method": "artist.gettoptags", "artist": name,
                                           "api_key": self.api_key, "format": "json", "autocorrect": 1})
            if r.status_code == 200:
                for t in (r.json().get("toptags") or {}).get("tag", []):
                    tag = t.get("name", "").strip().lower()
                    if tag and tag not in _JUNK and int(t.get("count", 0)) >= 10 and not tag.isdigit():
                        tags.append(tag)
                    if len(tags) >= top:
                        break
        except (httpx.HTTPError, ValueError):
            pass
        self._cache[key] = tags
        return tags

    def many(self, names: list[str]) -> dict[str, list[str]]:
        if not self.enabled:
            return {}
        names = list(dict.fromkeys(names))
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(zip(names, pool.map(self.artist_tags, names)))
