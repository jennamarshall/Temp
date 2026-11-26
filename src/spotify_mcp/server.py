"""MCP server exposing Spotify playlist reading, editing and song evaluation."""

from __future__ import annotations

import functools
from collections import Counter
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import analysis
from .auth import AuthError
from .client import SpotifyAPIError, SpotifyClient, parse_id, to_uri
from .lastfm import LastFM

mcp = MCPServer(
    name="spotify",
    instructions=(
        "Spotify tools. Playlists can be referenced by name, ID, URI or URL. Tracks can be "
        "referenced by URI/URL/ID or by a free-text query like 'Artist - Title' (resolved via "
        "search; check the 'resolved' field). Before adding songs to a playlist, use "
        "evaluate_songs with that playlist to check fit, then add_to_playlist. Use dry_run to preview."
    ),
)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)



def tool(annotations: ToolAnnotations):
    """Register a tool; expected failures reach the model with their real message."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (AuthError, SpotifyAPIError, ValueError) as e:
                raise ToolError(str(e)) from e
        return mcp.tool(annotations=annotations)(wrapper)
    return deco


_client: SpotifyClient | None = None
_lastfm: LastFM | None = None


def sp() -> SpotifyClient:
    global _client
    if _client is None:
        _client = SpotifyClient()
    return _client


def lfm() -> LastFM:
    global _lastfm
    if _lastfm is None:
        _lastfm = LastFM()
    return _lastfm


# ------------------------------------------------------------------ helpers
def resolve_playlist(ref: str) -> dict:
    """Find a playlist by ID/URI/URL, or by name among the user's playlists."""
    try:
        return sp().playlist(parse_id(ref, "playlist"))
    except ValueError:
        pass
    mine = sp().my_playlists()
    low = ref.strip().lower()
    exact = [p for p in mine if (p.get("name") or "").lower() == low]
    partial = [p for p in mine if low in (p.get("name") or "").lower()]
    matches = exact or partial
    if not matches:
        raise ValueError(f"No playlist named {ref!r} in your library.")
    if len(matches) > 1 and not exact:
        names = ", ".join(f"{p['name']} ({p['id']})" for p in matches[:8])
        raise ValueError(f"{ref!r} matches several playlists: {names}. Pass the ID.")
    return matches[0]


def resolve_tracks(refs: list[str]) -> tuple[list[dict], list[dict]]:
    """Turn IDs/URIs/URLs/free-text into compact tracks. Returns (tracks, unresolved)."""
    ids, order, queries, unresolved = [], [], {}, []
    for r in refs:
        try:
            tid = parse_id(r)
            ids.append(tid)
            order.append(("id", tid, r))
        except ValueError:
            order.append(("q", r, r))
    fetched = sp().tracks(ids) if ids else {}
    for kind, val, raw in order:
        if kind == "q" and val not in queries:
            res = sp().search(val, "track", limit=1)
            items = (res.get("tracks") or {}).get("items") or []
            queries[val] = items[0] if items else None
    out = []
    for kind, val, raw in order:
        t = fetched.get(val) if kind == "id" else queries.get(val)
        if not t:
            unresolved.append({"input": raw, "reason": "not found"})
            continue
        c = analysis.compact_track(t)
        if kind == "q":
            c["resolved"] = f"{raw!r} -> {c['name']} by {', '.join(a['name'] for a in c['artists'])}"
        out.append(c)
    return out, unresolved


def enrich(tracks: list[dict], max_artists: int = 120) -> tuple[dict[str, list[str]], dict[str, dict], dict]:
    """Collect genre tags per artist and audio features per track, best effort."""
    counts = Counter(a["id"] for t in tracks for a in t["artists"] if a.get("id"))
    artist_ids = [a for a, _ in counts.most_common(max_artists)]
    genres: dict[str, list[str]] = {}
    notes: dict[str, Any] = {}
    try:
        for aid, a in sp().artists(artist_ids).items():
            if a.get("genres"):
                genres[aid] = a["genres"]
    except SpotifyAPIError as e:
        notes["spotify_genres"] = f"unavailable ({e.status})"
    if lfm().enabled:
        names = {a["id"]: a["name"] for t in tracks for a in t["artists"] if a.get("id") in artist_ids}
        need = {aid: n for aid, n in names.items() if aid not in genres}
        tags = lfm().many(list(need.values()))
        for aid, n in need.items():
            if tags.get(n):
                genres[aid] = tags[n]
        # Blend Last.fm tags into artists Spotify already tagged, for richer overlap.
        for aid, n in names.items():
            if aid in genres and aid not in need:
                extra = lfm().artist_tags(n)
                genres[aid] = list(dict.fromkeys(genres[aid] + extra))
        notes["lastfm_tags"] = "used"
    else:
        notes["lastfm_tags"] = "off (set LASTFM_API_KEY for better genre matching)"
    features = sp().audio_features([t["id"] for t in tracks if t.get("id")])
    notes["audio_features"] = "used" if features else "not available to this Spotify app"
    if len(counts) > max_artists:
        notes["artist_sampling"] = f"genres looked up for the {max_artists} most frequent of {len(counts)} artists"
    return genres, features, notes


def _verdict(score: int | None) -> str:
    if score is None:
        return "unknown"
    return "strong fit" if score >= 70 else "possible fit" if score >= 50 else "poor fit"


# -------------------------------------------------------------------- tools
@tool(READ)
def whoami() -> dict:
    """Show the connected Spotify account."""
    me = sp().me()
    return {"id": me.get("id"), "name": me.get("display_name"), "product": me.get("product"),
            "country": me.get("country")}


@tool(READ)
def list_playlists(name_contains: str | None = None, owned_only: bool = False, limit: int = 200) -> list[dict]:
    """List the user's playlists (owned and followed), optionally filtered by name or ownership."""
    me_id = sp().me().get("id")
    out = []
    for p in sp().my_playlists():
        c = analysis.compact_playlist(p)
        c["editable"] = c["owner_id"] == me_id or bool(c["collaborative"])
        if owned_only and c["owner_id"] != me_id:
            continue
        if name_contains and name_contains.lower() not in (c["name"] or "").lower():
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


@tool(READ)
def get_playlist(playlist: str, include_tracks: bool = True, max_tracks: int = 1000) -> dict:
    """Read a playlist's details and every track in it (paginated automatically).

    playlist: name, ID, URI or URL.
    """
    p = resolve_playlist(playlist)
    out = analysis.compact_playlist(p)
    if include_tracks:
        try:
            tracks = analysis.playlist_entries_to_tracks(sp().playlist_items(p["id"], max_tracks))
        except SpotifyAPIError as e:
            if e.status == 403:
                raise ValueError(
                    "Spotify refused to list this playlist's tracks (403). Since Feb 2026, "
                    "Development Mode apps can only read items of playlists you own or collaborate on."
                ) from e
            raise
        out["tracks"] = tracks
        out["track_count"] = len(tracks)
    return out


@tool(READ)
def search(query: str, type: Literal["track", "artist", "album", "playlist"] = "track",
           limit: int = 10) -> list[dict]:
    """Search Spotify's catalog. Supports filters like 'artist:Radiohead year:1997 genre:rock'."""
    res = sp().search(query, type, limit=limit)
    items = (res.get(f"{type}s") or {}).get("items") or []
    items = [i for i in items if i]
    if type == "track":
        return [analysis.compact_track(i) for i in items]
    if type == "playlist":
        return [analysis.compact_playlist(i) for i in items]
    return [{"id": i.get("id"), "uri": i.get("uri"), "name": i.get("name"),
             "genres": i.get("genres"), "release_date": i.get("release_date"),
             "artists": [a.get("name") for a in i.get("artists", [])] or None,
             "url": (i.get("external_urls") or {}).get("spotify")} for i in items]


@tool(WRITE)
def add_to_playlist(playlist: str, tracks: list[str], position: int | None = None,
                    skip_duplicates: bool = True, dry_run: bool = False) -> dict:
    """Add songs to an existing playlist.

    tracks: Spotify URIs/URLs/IDs, or free-text like 'Daft Punk - Digital Love'.
    skip_duplicates: don't add songs already on the playlist (same track, or same song
      in another version/remaster).
    position: 0-based insert index; omitted = append to the end.
    dry_run: resolve and check everything but don't modify the playlist.
    """
    p = resolve_playlist(playlist)
    resolved, unresolved = resolve_tracks(tracks)
    skipped = []
    to_add = resolved
    if skip_duplicates:
        existing = analysis.playlist_entries_to_tracks(sp().playlist_items(p["id"]))
        have_ids = {t["id"] for t in existing}
        have_keys = {analysis.song_key(t) for t in existing}
        to_add, seen = [], set()
        for t in resolved:
            key = analysis.song_key(t)
            if t["id"] in have_ids or key in have_keys:
                skipped.append({"name": t["name"], "reason": "already on playlist"})
            elif t["id"] in seen:
                skipped.append({"name": t["name"], "reason": "duplicate in request"})
            else:
                seen.add(t["id"])
                to_add.append(t)
    result = {
        "playlist": p.get("name"),
        "playlist_id": p.get("id"),
        "added" if not dry_run else "would_add": [
            {"name": t["name"], "artists": [a["name"] for a in t["artists"]], "uri": t["uri"],
             **({"resolved": t["resolved"]} if "resolved" in t else {})} for t in to_add],
        "skipped": skipped,
        "unresolved": unresolved,
    }
    if to_add and not dry_run:
        try:
            result["snapshot_ids"] = sp().add_items(p["id"], [t["uri"] for t in to_add], position)
        except SpotifyAPIError as e:
            if e.status == 403:
                raise ValueError("Spotify refused (403): you can only add to playlists you own or "
                                 "collaborate on.") from e
            raise
    return result


@tool(DESTRUCTIVE)
def remove_from_playlist(playlist: str, tracks: list[str]) -> dict:
    """Remove songs (every occurrence) from a playlist. tracks: URIs/URLs/IDs."""
    p = resolve_playlist(playlist)
    uris = [to_uri(t) for t in tracks]
    snaps = sp().remove_items(p["id"], uris)
    return {"playlist": p.get("name"), "removed": uris, "snapshot_ids": snaps}


@tool(WRITE)
def create_playlist(name: str, description: str = "", public: bool = False,
                    tracks: list[str] | None = None) -> dict:
    """Create a new playlist, optionally seeded with tracks (URIs/URLs/IDs or free text)."""
    p = sp().create_playlist(name, description, public)
    out = analysis.compact_playlist(p)
    if tracks:
        resolved, unresolved = resolve_tracks(tracks)
        if resolved:
            sp().add_items(p["id"], [t["uri"] for t in resolved])
        out["added"] = [t["name"] for t in resolved]
        out["unresolved"] = unresolved
    return out


@tool(READ)
def evaluate_playlist(playlist: str, weakest: int = 10) -> dict:
    """Analyze a playlist: era and genre makeup, artist variety, explicit share, duplicates and
    alternate versions, unplayable tracks, an overall cohesion score, and the songs that fit
    the rest of the playlist least (with reasons).
    """
    p = resolve_playlist(playlist)
    tracks = analysis.playlist_entries_to_tracks(sp().playlist_items(p["id"]))
    genres, features, notes = enrich(tracks)
    report = analysis.evaluate_playlist(tracks, genres, features, weakest=weakest)
    return {"playlist": p.get("name"), "playlist_id": p.get("id"), **report, "data_notes": notes}


@tool(READ)
def evaluate_songs(tracks: list[str], playlist: str | None = None) -> dict:
    """Evaluate songs: details (year, length, explicit, popularity if available, genre tags) and,
    if a playlist is given, a 0-100 fit score against it with reasons, whether it's already on
    the playlist, and a verdict (strong / possible / poor fit).

    tracks: URIs/URLs/IDs or free text like 'Artist - Title'.
    """
    cands, unresolved = resolve_tracks(tracks)
    base: list[dict] = []
    p = None
    if playlist:
        p = resolve_playlist(playlist)
        base = analysis.playlist_entries_to_tracks(sp().playlist_items(p["id"]))
    genres, features, notes = enrich(base + cands)
    profile = analysis.build_profile(base, genres, features) if base else None
    have_ids = {t["id"] for t in base}
    have_keys = {analysis.song_key(t) for t in base}
    results = []
    for t in cands:
        r: dict[str, Any] = {
            "name": t["name"], "artists": [a["name"] for a in t["artists"]], "uri": t["uri"],
            "album": t["album"], "year": t["year"],
            "length": f"{t['duration_ms'] // 60000}:{t['duration_ms'] // 1000 % 60:02d}" if t["duration_ms"] else None,
            "explicit": t["explicit"], "popularity": t["popularity"],
            "genre_tags": sorted(analysis.track_genres(t, genres))[:8],
        }
        if "resolved" in t:
            r["resolved"] = t["resolved"]
        f = features.get(t["id"])
        if f:
            r["audio"] = {k: f.get(k) for k in analysis.FEATURE_KEYS + ("tempo",)}
        if profile:
            r["already_on_playlist"] = t["id"] in have_ids or analysis.song_key(t) in have_keys
            s = analysis.fit_score(t, profile, genres, features)
            r.update(fit_score=s["score"], verdict=_verdict(s["score"]), reasons=s["reasons"],
                     signals=s["signals"])
        results.append(r)
    if profile:
        results.sort(key=lambda r: -(r.get("fit_score") or 0))
    out: dict[str, Any] = {"songs": results, "unresolved": unresolved, "data_notes": notes}
    if p:
        out["playlist"] = p.get("name")
    return out


@tool(READ)
def listening_history(source: Literal["top_tracks", "top_artists", "recent", "saved"] = "top_tracks",
                      time_range: Literal["short_term", "medium_term", "long_term"] = "medium_term",
                      limit: int = 50) -> list[dict]:
    """The user's top tracks/artists (short_term ~4 weeks, medium ~6 months, long ~1 year),
    recently played tracks, or Liked Songs. Useful for finding songs to add to playlists."""
    if source == "top_artists":
        return [{"id": a.get("id"), "name": a.get("name"), "genres": a.get("genres"), "uri": a.get("uri")}
                for a in sp().top_items("artists", time_range, limit)]
    if source == "top_tracks":
        return [analysis.compact_track(t) for t in sp().top_items("tracks", time_range, limit)]
    if source == "recent":
        return [{**analysis.compact_track(i["track"]), "played_at": i.get("played_at")}
                for i in sp().recently_played(limit) if i.get("track")]
    return [{**analysis.compact_track(i["track"]), "added_at": i.get("added_at")}
            for i in sp().saved_tracks(limit) if i.get("track")]
