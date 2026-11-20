"""Start the app: `python -m cadraw`."""

import argparse
import os
import threading
import webbrowser

import uvicorn


def main():
    ap = argparse.ArgumentParser(description="Exploded line drawings from STEP files")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"Running at {url}  (Ctrl+C to stop)")
    # picks up code updates by itself, no restart needed
    here = os.path.dirname(os.path.abspath(__file__))
    uvicorn.run("cadraw.server:app", host="127.0.0.1", port=args.port, log_level="warning", reload=True, reload_dirs=[here])


if __name__ == "__main__":
    main()
