"""Start the dashboard empty, for a demo: archive outputs/state, launch Streamlit, open the Upload page.

    python scripts/fresh_start.py [--port 8501]

The old state is moved to outputs/archive/<UTC timestamp>/, never deleted.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard import reset  # noqa: E402
from pipeline import review  # noqa: E402


def wait_until_up(url: str, proc: subprocess.Popen, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            urllib.request.urlopen(url + "/_stcore/health", timeout=1)
            return True
        except OSError:
            time.sleep(0.3)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8501)
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()

    moved = reset.archive_state(review.Config.default())
    print(f"Previous state moved to {moved}" if moved else "No saved state; starting empty.", flush=True)

    url = f"http://localhost:{args.port}"
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"),
                             "--server.headless", "true", "--server.port", str(args.port)], cwd=ROOT)
    try:
        if wait_until_up(url, proc):
            print(f"Dashboard ready at {url}/Upload", flush=True)
            if not args.no_browser:
                webbrowser.open(f"{url}/Upload")
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
