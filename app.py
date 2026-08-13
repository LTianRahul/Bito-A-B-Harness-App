#!/usr/bin/env python3
"""Launcher for the A/B Testing Benchmark Harness UI.

Starts the local web server and opens the dashboard in your browser. Once it's
running you drive everything from the UI — no terminal needed.

    python app.py                 # start on http://127.0.0.1:8765
    python app.py --port 9000     # custom port
    python app.py --no-browser    # don't auto-open a browser
    python app.py --reload        # dev: auto-reload backend on change
"""

from __future__ import annotations

import argparse
import threading
import time
import webbrowser


def _open_browser_when_ready(url: str) -> None:
    # Give uvicorn a moment to bind before opening the tab.
    time.sleep(1.2)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="A/B Testing Benchmark Harness UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open a browser")
    parser.add_argument("--reload", action="store_true", help="Auto-reload backend (dev)")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"\n  A/B Testing Benchmark Harness")
    print(f"  → Open {url} in your browser\n")

    if not args.no_browser and not args.reload:
        threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()

    # Build skills cache before starting the server so the first request is fast.
    try:
        from backend.services.skills import build_all_caches
        build_all_caches()
    except Exception:
        pass

    import uvicorn

    try:
        uvicorn.run(
            "backend.main:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level="info",
            # Don't hang on Ctrl+C waiting for a long-lived SSE stream to close —
            # force shutdown after a few seconds so the port is released promptly.
            timeout_graceful_shutdown=5,
        )
    except SystemExit:
        raise
    except OSError as e:
        # Most commonly "address already in use" — a previous instance is still bound.
        print(f"\n  Could not start on {url}: {e}")
        print(f"  Another instance may be running. Free the port and retry, e.g.:")
        print(f"    macOS/Linux:  lsof -ti tcp:{args.port} | xargs kill")
        print(f"    or start on another port:  python app.py --port {args.port + 1}\n")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
