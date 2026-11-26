# spotify-mcp

An MCP server that gives Claude (or any MCP client) real access to your Spotify:
**read playlists, add songs to existing playlists, and evaluate songs** against a playlist.

## Tools

| Tool | What it does |
|---|---|
| `list_playlists` | Your playlists (owned + followed), filter by name; shows which you can edit |
| `get_playlist` | Every track in a playlist (auto-paginated). Accepts name, ID, URI or URL |
| `search` | Catalog search with Spotify filters (`artist:`, `year:`, `genre:`) |
| `add_to_playlist` | Add songs to an existing playlist. Accepts URIs/URLs or plain text like `"Daft Punk - Digital Love"`. Skips songs already there, including remasters and other versions. Has `dry_run` and `position` |
| `remove_from_playlist` | Remove songs |
| `create_playlist` | New playlist, optionally seeded with songs |
| `evaluate_playlist` | Era/decade makeup, top genres, artist variety, explicit share, duplicates and alternate versions, unplayable or local files, a **cohesion score**, and the **weakest-fitting songs** with reasons |
| `evaluate_songs` | Per-song details. Given a playlist, it adds a **0–100 fit score**, a verdict (strong/possible/poor fit), reasons, and whether the song is already on the playlist |
| `listening_history` | Top tracks/artists, recently played, Liked Songs |

A typical prompt: *"Look at my Sunday Morning playlist, find 10 songs from my recent listening
that would fit, check them, and add the strong fits."*

## How songs are evaluated

The fit score is a weighted blend of whichever signals are available. The server reports which ones it used:

- **Genre overlap** with the playlist (Spotify artist genres, plus Last.fm tags if configured)
- **Artist familiarity** (artist already on the playlist)
- **Era**: release year compared with the playlist's median and spread
- **Explicit consistency**: penalizes explicit songs on a clean playlist
- **Duration**, and **popularity** when Spotify returns it
- **Audio features** (energy, mood, tempo…) only if your Spotify app still has access

Spotify has cut back what Development Mode apps can see: audio features in Nov 2024, then popularity,
some batch endpoints and the old playlist `/tracks` paths in Feb 2026. **Set `LASTFM_API_KEY`
(free) for much better genre matching.** Without it, scores depend on Spotify's sparse genre data.

## Setup

1. Create an app at <https://developer.spotify.com/dashboard>. Select **Web API** and add the redirect URI
   `http://127.0.0.1:8888/callback`. Spotify rejects `localhost`. Copy the **Client ID**. No secret
   is needed because the server uses PKCE. Development Mode apps need the owner to have Premium.
2. Install and log in once:
   ```bash
   pip install -e .            # or: uv pip install -e .
   export SPOTIFY_CLIENT_ID=your_client_id
   spotify-mcp auth            # opens browser; token saved to ~/.config/spotify-mcp/token.json
   ```
3. Optional: get a Last.fm API key at <https://www.last.fm/api/account/create>.
4. Add it to your MCP client. For Claude Desktop/Claude Code:
   ```json
   {
     "mcpServers": {
       "spotify": {
         "command": "spotify-mcp",
         "env": { "SPOTIFY_CLIENT_ID": "your_client_id", "LASTFM_API_KEY": "optional" }
       }
     }
   }
   ```
   Or with Claude Code: `claude mcp add spotify -e SPOTIFY_CLIENT_ID=... -- spotify-mcp`

Environment variables: `SPOTIFY_CLIENT_ID` (required), `SPOTIFY_REDIRECT_URI`,
`SPOTIFY_TOKEN_PATH`, `SPOTIFY_REFRESH_TOKEN` (headless alternative to `auth`), `LASTFM_API_KEY`.

## Compatibility notes

- Tries the 2026 endpoints first (`/playlists/{id}/items`, `POST /me/playlists`). If those return 404, it falls back to the
  legacy ones and remembers the choice. It reads entries keyed either `item` or `track`.
- If batch lookups (`/tracks?ids=`, `/artists?ids=`) are refused, it fetches items one at a time.
- Retries on 429 (honoring `Retry-After`) and 5xx, and refreshes the token on 401.
- Development Mode apps can only read and modify items of playlists you own or collaborate on.

## Development

```bash
pip install -e '.[dev]' && pytest
```
Tests run against an in-memory fake of the Spotify API that covers both the 2026 and legacy shapes.
