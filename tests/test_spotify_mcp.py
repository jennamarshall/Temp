import pytest

from fake_spotify import CATALOG, EXTRA, GENRES, PID, PLAYLIST_TRACKS, FakeSpotify
from spotify_mcp import analysis, server
from spotify_mcp.client import entry_track, parse_id, to_uri

TID = "4uLU6hMCjMI75M1A2tKUQC"


@pytest.mark.parametrize("value", [
    TID, f"spotify:track:{TID}", f"https://open.spotify.com/track/{TID}?si=abc",
    f"https://open.spotify.com/intl-de/track/{TID}",
])
def test_parse_id(value):
    assert parse_id(value) == TID
    assert to_uri(value) == f"spotify:track:{TID}"


def test_parse_id_rejects_free_text():
    with pytest.raises(ValueError):
        parse_id("Daft Punk - Digital Love")


@pytest.mark.parametrize("modern", [True, False])
def test_reads_all_playlist_items_both_api_shapes(modern):
    fake = FakeSpotify(modern=modern)
    items = fake.client().playlist_items(PID)
    assert [entry_track(e)["id"] for e in items] == [t["id"] for t in PLAYLIST_TRACKS]
    if not modern:
        assert ("GET", f"playlists/{PID}/items") in fake.calls  # tried new path first


def test_legacy_fallback_is_remembered():
    fake = FakeSpotify(modern=False)
    c = fake.client()
    c.playlist_items(PID)
    fake.calls.clear()
    c.playlist_items(PID)
    assert ("GET", f"playlists/{PID}/items") not in fake.calls


def test_batch_endpoint_removed_falls_back_to_single_gets():
    fake = FakeSpotify(batch=False)
    ids = [t["id"] for t in PLAYLIST_TRACKS[:3]]
    got = fake.client().tracks(ids)
    assert set(got) == set(ids)


def test_audio_features_unavailable_returns_empty():
    c = FakeSpotify(audio=False).client()
    assert c.audio_features([PLAYLIST_TRACKS[0]["id"]]) == {}
    assert c.audio_features_available is False


def test_retries_on_429(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake = FakeSpotify()
    fake.fail_once["me"] = 2
    assert fake.client().me()["id"] == "me"


def test_refreshes_token_on_401():
    fake = FakeSpotify()
    fake.fail_once["expire_me"] = 0
    c = fake.client()
    assert c.me()["id"] == "me"
    assert c.tokens.refreshes == 1


def test_remove_items_uses_body_matching_endpoint():
    for modern in (True, False):
        fake = FakeSpotify(modern=modern)
        fake.client().remove_items(PID, [PLAYLIST_TRACKS[0]["uri"]])
        assert PLAYLIST_TRACKS[0]["uri"] not in {t["uri"] for t in fake.playlist}


# ------------------------------------------------------------------ analysis
def compact(ts):
    return [analysis.compact_track(t) for t in ts]


def test_normalize_title_strips_versions():
    assert analysis.normalize_title("One More Time - Radio Edit") == "one more time"
    assert analysis.normalize_title("Hey Jude (Remastered 2015)") == "hey jude"
    assert analysis.normalize_title("Song (feat. Someone)") == "song"


def test_evaluate_playlist_report():
    report = analysis.evaluate_playlist(compact(PLAYLIST_TRACKS), GENRES)
    assert report["track_count"] == 8
    assert report["era"]["oldest"] == 1998
    assert report["artists"]["top"][0] == ("Daft Punk", 3)
    assert report["issues"]["same_song_different_versions"]
    assert report["genres"][0][0] in ("electro", "french house")
    assert report["signals_available"]["popularity"] is False  # handled without popularity
    assert 0 <= report["cohesion_score"] <= 100


def test_fit_score_ranks_in_genre_song_above_outlier():
    profile = analysis.build_profile(compact(PLAYLIST_TRACKS), GENRES)
    good, jolene, hurt, _ = compact(EXTRA)
    s_good = analysis.fit_score(good, profile, GENRES)
    s_jolene = analysis.fit_score(jolene, profile, GENRES)
    s_hurt = analysis.fit_score(hurt, profile, GENRES)
    assert s_good["score"] >= 70
    assert s_jolene["score"] < 50
    assert any("era outlier" in r for r in s_jolene["reasons"])
    assert any("explicit" in r for r in s_hurt["reasons"])


def test_fit_score_without_any_data():
    assert analysis.fit_score({"artists": []}, analysis.build_profile([]))["score"] is None


# -------------------------------------------------------------------- tools
@pytest.fixture
def fake(monkeypatch):
    f = FakeSpotify()
    monkeypatch.setattr(server, "_client", f.client())
    monkeypatch.setattr(server, "_lastfm", server.LastFM(api_key=""))
    return f


def test_get_playlist_by_name(fake):
    p = server.get_playlist("french touch")
    assert p["id"] == PID and p["track_count"] == 8


def test_add_to_playlist_resolves_text_and_skips_duplicates(fake):
    res = server.add_to_playlist("French Touch", [
        "Around the World", EXTRA[1]["uri"], PLAYLIST_TRACKS[0]["uri"], "nonexistent zzz"])
    assert [a["name"] for a in res["added"]] == ["Around the World", "Jolene"]
    assert res["skipped"][0]["reason"] == "already on playlist"
    assert res["unresolved"][0]["input"] == "nonexistent zzz"
    assert len(fake.playlist) == 10


def test_add_to_playlist_dry_run_does_not_write(fake):
    res = server.add_to_playlist(PID, [EXTRA[0]["uri"]], dry_run=True)
    assert res["would_add"] and len(fake.playlist) == 8


def test_evaluate_songs_against_playlist(fake):
    res = server.evaluate_songs([EXTRA[1]["uri"], EXTRA[0]["uri"], PLAYLIST_TRACKS[1]["uri"]], "French Touch")
    names = [s["name"] for s in res["songs"]]
    assert names[-1] == "Jolene" and res["songs"][-1]["verdict"] == "poor fit"
    assert next(s for s in res["songs"] if s["name"] == "Digital Love")["already_on_playlist"]
    assert res["data_notes"]["audio_features"] == "not available to this Spotify app"


def test_evaluate_playlist_tool(fake):
    res = server.evaluate_playlist("French Touch")
    assert res["playlist"] == "French Touch" and "weakest_fits" in res
