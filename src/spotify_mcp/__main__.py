"""CLI: `spotify-mcp auth` to log in once, `spotify-mcp` (or `serve`) to run the MCP server."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="spotify-mcp")
    sub = parser.add_subparsers(dest="cmd")
    a = sub.add_parser("auth", help="Log in to Spotify (opens a browser) and cache a token")
    a.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening it")
    s = sub.add_parser("serve", help="Run the MCP server (default)")
    s.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"])
    args = parser.parse_args()

    if args.cmd == "auth":
        from .auth import AuthError, interactive_login, token_path
        try:
            interactive_login(open_browser=not args.no_browser)
        except AuthError as e:
            sys.exit(f"Error: {e}")
        print(f"Logged in. Token saved to {token_path()}")
        return

    from .server import mcp
    mcp.run(transport=getattr(args, "transport", "stdio"))


if __name__ == "__main__":
    main()
