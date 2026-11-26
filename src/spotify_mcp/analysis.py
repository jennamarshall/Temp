"""Song and playlist evaluation. Pure functions: no network calls here.

Every signal is optional. Spotify removed or restricted several fields for
Development Mode apps (audio features in 2024; popularity and some batch
endpoints in 2026), so each score uses whatever data is present and says which
signals it used.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .client import entry_track

FEATURE_KEYS = ("energy", "valence", "danceability", "acousticness", "instrumentalness", "speechiness")


# --------------------------------------------------------------- normalizing
def compact_track(t: dict) -> dict:
    album = t.get("album") or {}
    release = album.get("release_date") or ""
    year = int(release[:4]) if release[:4].isdigit() else None
    return {
        "id": t.get("id"),
        "uri": t.get("uri"),
        "name": t.get("name"),
        "artists": [{"id": a.get("id"), "name": a.get("name")} for a in t.get("artists", [])],
        "album": album.get("name"),
        "release_date": release or None,
        "year": year,
        "duration_ms": t.get("duration_ms"),
        "explicit": t.get("explicit"),
        "popularity": t.get("popularity"),
        "is_local": t.get("is_local", False),
        "is_playable": t.get("is_playable"),
        "url": (t.get("external_urls") or {}).get("spotify"),
    }


def compact_playlist(p: dict) -> dict:
    owner = p.get("owner") or {}
    count_src = p.get("items") or p.get("tracks") or {}
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "owner": owner.get("display_name") or owner.get("id"),
        "owner_id": owner.get("id"),
        "public": p.get("public"),
        "collaborative": p.get("collaborative"),
        "track_count": count_src.get("total") if isinstance(count_src, dict) else None,
        "description": p.get("description") or "",
        "url": (p.get("external_urls") or {}).get("spotify"),
        "snapshot_id": p.get("snapshot_id"),
    }


def playlist_entries_to_tracks(entries: list[dict]) -> list[dict]:
    """Return compact tracks, keeping added_at; skips podcast episodes and empty slots."""
    out = []
    for e in entries:
        t = entry_track(e)
        if not t or t.get("type", "track") != "track":
            continue
        c = compact_track(t)
        c["added_at"] = e.get("added_at")
        out.append(c)
    return out


_PAREN = re.compile(r"\s*[\(\[][^)\]]*(remaster|version|edit|mix|mono|stereo|live|feat|ft\.|deluxe|bonus)[^)\]]*[\)\]]", re.I)
_DASH = re.compile(r"\s+-\s+.*(remaster|version|edit|mix|mono|stereo|live|single|radio).*$", re.I)


def normalize_title(name: str) -> str:
    s = _PAREN.sub("", name or "")
    s = _DASH.sub("", s)
    s = re.sub(r"[^\w\s]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def song_key(t: dict) -> tuple[str, str]:
    primary = (t["artists"][0]["name"] if t.get("artists") else "").lower()
    return normalize_title(t.get("name") or ""), primary


# ------------------------------------------------------------------ profile
def _q(values: list[float], q: float) -> float:
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


@dataclass
class Profile:
    n: int
    artist_counts: Counter
    genre_share: dict[str, float]
    years: list[int]
    durations: list[int]
    popularity: list[int]
    explicit_share: float | None
    features: dict[str, tuple[float, float]] = field(default_factory=dict)  # key -> (mean, stdev)

    @property
    def year_median(self) -> float | None:
        return statistics.median(self.years) if self.years else None

    @property
    def year_spread(self) -> float:
        if len(self.years) < 4:
            return 5.0
        return max(3.0, (_q(self.years, 0.75) - _q(self.years, 0.25)))


def track_genres(t: dict, artist_genres: dict[str, list[str]]) -> set[str]:
    g: set[str] = set()
    for a in t.get("artists", []):
        g.update(x.lower() for x in artist_genres.get(a.get("id") or a.get("name") or "", []))
    return g


def build_profile(tracks: list[dict], artist_genres: dict[str, list[str]] | None = None,
                  features: dict[str, dict] | None = None) -> Profile:
    artist_genres = artist_genres or {}
    features = features or {}
    artist_counts: Counter = Counter()
    genre_counts: Counter = Counter()
    for t in tracks:
        for a in t.get("artists", []):
            artist_counts[a.get("name")] += 1
        for g in track_genres(t, artist_genres):
            genre_counts[g] += 1
    n = len(tracks)
    explicit = [t["explicit"] for t in tracks if t.get("explicit") is not None]
    feat_stats = {}
    for k in FEATURE_KEYS + ("tempo",):
        vals = [features[t["id"]][k] for t in tracks if t.get("id") in features and features[t["id"]].get(k) is not None]
        if len(vals) >= 3:
            feat_stats[k] = (statistics.fmean(vals), statistics.pstdev(vals))
    return Profile(
        n=n,
        artist_counts=artist_counts,
        genre_share={g: c / n for g, c in genre_counts.items()} if n else {},
        years=[t["year"] for t in tracks if t.get("year")],
        durations=[t["duration_ms"] for t in tracks if t.get("duration_ms")],
        popularity=[t["popularity"] for t in tracks if t.get("popularity") is not None],
        explicit_share=(sum(explicit) / len(explicit)) if explicit else None,
        features=feat_stats,
    )


# ---------------------------------------------------------------- fit score
WEIGHTS = {"artist": 0.20, "genre": 0.35, "era": 0.20, "audio": 0.30,
           "popularity": 0.08, "explicit": 0.10, "duration": 0.05}


def fit_score(t: dict, profile: Profile, artist_genres: dict[str, list[str]] | None = None,
              features: dict[str, dict] | None = None, exclude_self: bool = False) -> dict:
    """Score 0-100 for how well a track fits a playlist profile, with reasons.

    exclude_self: the track is itself in the playlist, so discount its own
    contribution to artist/genre counts (avoids every track "fitting" itself).
    """
    artist_genres = artist_genres or {}
    features = features or {}
    parts: dict[str, float] = {}
    reasons: list[str] = []
    n_other = max(profile.n - (1 if exclude_self else 0), 1)

    # Artist familiarity
    shared = []
    for a in t.get("artists", []):
        c = profile.artist_counts.get(a.get("name"), 0) - (1 if exclude_self else 0)
        if c > 0:
            shared.append((a.get("name"), c))
    if profile.n:
        if shared:
            parts["artist"] = min(1.0, 0.6 + 0.4 * max(c for _, c in shared) / n_other * 5)
            reasons.append("artist already on playlist: " + ", ".join(f"{a} ({c}x)" for a, c in shared))
        else:
            parts["artist"] = 0.3  # new artists are fine; this is a mild signal

    # Genre overlap
    g = track_genres(t, artist_genres)
    if g and profile.genre_share:
        share = {k: v - (1 / profile.n if exclude_self and k in g else 0) for k, v in profile.genre_share.items()}
        hits = sorted(((share.get(x, 0), x) for x in g), reverse=True)
        best = hits[0][0]
        matched = [x for s, x in hits if s > 0]
        parts["genre"] = min(1.0, best * 2.5) * 0.7 + 0.3 * (len(matched) / len(g))
        if matched:
            reasons.append("genre match: " + ", ".join(matched[:4]))
        else:
            reasons.append("no genre overlap (" + ", ".join(sorted(g)[:3]) + ")")

    # Era
    if t.get("year") and profile.year_median:
        gap = abs(t["year"] - profile.year_median)
        parts["era"] = math.exp(-gap / (profile.year_spread * 1.5))
        if gap > profile.year_spread * 2:
            reasons.append(f"era outlier: {t['year']} vs playlist median {int(profile.year_median)}")

    # Audio features (only if Spotify still serves them to this app)
    f = features.get(t.get("id") or "")
    if f and profile.features:
        dists = []
        for k, (mean, sd) in profile.features.items():
            if f.get(k) is None:
                continue
            scale = (sd or 0.0) + (15.0 if k == "tempo" else 0.1)
            dists.append(abs(f[k] - mean) / scale)
        if dists:
            d = statistics.fmean(dists)
            parts["audio"] = math.exp(-max(0.0, d - 0.5))
            if d > 1.5:
                reasons.append("sound differs (energy/mood/tempo) from playlist average")

    # Popularity (often missing for Development Mode apps since Feb 2026)
    if t.get("popularity") is not None and len(profile.popularity) >= 3:
        parts["popularity"] = max(0.0, 1 - abs(t["popularity"] - statistics.fmean(profile.popularity)) / 50)

    # Explicit content consistency
    if t.get("explicit") is not None and profile.explicit_share is not None:
        if t["explicit"] and profile.explicit_share < 0.05:
            parts["explicit"] = 0.0
            reasons.append("explicit, but playlist is clean")
        else:
            parts["explicit"] = 1.0

    # Duration
    if t.get("duration_ms") and len(profile.durations) >= 3:
        med = statistics.median(profile.durations)
        parts["duration"] = math.exp(-abs(t["duration_ms"] - med) / max(med, 1))

    if not parts:
        return {"score": None, "signals": {}, "reasons": ["not enough data to score"]}
    total_w = sum(WEIGHTS[k] for k in parts)
    score = sum(WEIGHTS[k] * v for k, v in parts.items()) / total_w
    return {
        "score": round(score * 100),
        "signals": {k: round(v, 2) for k, v in parts.items()},
        "reasons": reasons,
    }


# --------------------------------------------------------- playlist report
def evaluate_playlist(tracks: list[dict], artist_genres: dict[str, list[str]] | None = None,
                      features: dict[str, dict] | None = None, weakest: int = 10) -> dict[str, Any]:
    artist_genres = artist_genres or {}
    features = features or {}
    profile = build_profile(tracks, artist_genres, features)
    n = len(tracks)
    report: dict[str, Any] = {"track_count": n}
    if not n:
        return report

    durations = profile.durations
    report["total_duration_min"] = round(sum(durations) / 60000, 1)
    report["avg_track_min"] = round(statistics.fmean(durations) / 60000, 2) if durations else None

    if profile.years:
        decades = Counter(f"{y // 10 * 10}s" for y in profile.years)
        report["era"] = {
            "oldest": min(profile.years), "newest": max(profile.years),
            "median_year": int(profile.year_median),
            "decades": dict(sorted(decades.items())),
        }

    total_credits = sum(profile.artist_counts.values())
    hhi = sum((c / total_credits) ** 2 for c in profile.artist_counts.values()) if total_credits else 0
    report["artists"] = {
        "unique": len(profile.artist_counts),
        "top": profile.artist_counts.most_common(10),
        # 0 = every track a different artist, 1 = one artist only
        "concentration": round(hhi, 3),
    }
    if profile.genre_share:
        report["genres"] = [(g, round(s, 2)) for g, s in
                            sorted(profile.genre_share.items(), key=lambda x: -x[1])[:15]]
    if profile.popularity:
        report["popularity"] = {
            "mean": round(statistics.fmean(profile.popularity), 1),
            "min": min(profile.popularity), "max": max(profile.popularity),
        }
    if profile.explicit_share is not None:
        report["explicit_share"] = round(profile.explicit_share, 2)
    if profile.features:
        report["audio_profile"] = {k: {"mean": round(m, 2), "spread": round(s, 2)}
                                   for k, (m, s) in profile.features.items()}

    # Issues
    issues: dict[str, Any] = {}
    ids = Counter(t["id"] for t in tracks if t.get("id"))
    exact = [t for t in tracks if t.get("id") and ids[t["id"]] > 1]
    if exact:
        seen = {}
        for t in exact:
            seen.setdefault(t["id"], {"name": t["name"], "artist": t["artists"][0]["name"] if t["artists"] else None,
                                      "copies": ids[t["id"]], "uri": t["uri"]})
        issues["exact_duplicates"] = list(seen.values())
    groups: dict[tuple, list[dict]] = {}
    for t in tracks:
        groups.setdefault(song_key(t), []).append(t)
    near = [[{"name": t["name"], "album": t["album"], "uri": t["uri"]} for t in g]
            for g in groups.values() if len({t["id"] for t in g}) > 1]
    if near:
        issues["same_song_different_versions"] = near
    local = [t["name"] for t in tracks if t.get("is_local")]
    if local:
        issues["local_files"] = local
    unplayable = [t["name"] for t in tracks if t.get("is_playable") is False]
    if unplayable:
        issues["unplayable_in_market"] = unplayable
    if issues:
        report["issues"] = issues

    # Per-track fit to the rest of the playlist
    scored = []
    for t in tracks:
        if t.get("is_local"):
            continue
        s = fit_score(t, profile, artist_genres, features, exclude_self=True)
        if s["score"] is not None:
            scored.append({"name": t["name"], "artist": ", ".join(a["name"] for a in t["artists"]),
                           "uri": t["uri"], **s})
    scored.sort(key=lambda x: x["score"])
    if scored:
        report["cohesion_score"] = round(statistics.fmean(s["score"] for s in scored))
        report["weakest_fits"] = scored[:weakest]

    report["signals_available"] = {
        "genres": bool(profile.genre_share),
        "popularity": bool(profile.popularity),
        "audio_features": bool(profile.features),
        "release_year": bool(profile.years),
    }
    return report
