# /// script
# dependencies = ["playwright>=1.51,<2"]
# ///
"""One-time interactive login that produces state.json for the headless clicker.

Run on a machine with Chrome: `uv run webclicker/login.py`.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from clicker import URL

CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
STATE = Path(__file__).with_name("state.json")
CDP_PORT = 9222
READ_AUTH_USER = (
    "() => Object.entries(sessionStorage).filter(([k]) => k.startsWith('firebase:authUser:'))"
)


def main() -> None:
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(URL))
    # A plain Chrome launch, attached to only after login: Google refuses to
    # sign in inside a Playwright-launched (automation-flagged) browser.
    # Chrome's crashpad helper can outlive the browser and keep writing here.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        chrome = subprocess.Popen(
            [
                CHROME,
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={CDP_PORT}",
                "--no-first-run",
                URL,
            ]
        )
        try:
            input(
                "Sign in with Google in the Chrome window, wait for the student page, then press Enter… "
            )
            with sync_playwright() as p:
                pages = (
                    p.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}").contexts[0].pages
                )
                auth_user = {
                    key: value
                    for page in pages
                    if page.url.startswith(origin)
                    for key, value in page.evaluate(READ_AUTH_USER)
                }
        finally:
            chrome.terminate()
            chrome.wait()

    if not auth_user:
        sys.exit("no Firebase login found in any webclicker tab — were you signed in?")
    # The app pins Firebase Auth to per-tab sessionStorage, which a Playwright
    # storage state can't carry. On startup Firebase also checks localStorage
    # and moves a user it finds there into the active storage, so seed it there.
    state = {
        "cookies": [],
        "origins": [
            {
                "origin": origin,
                "localStorage": [{"name": k, "value": v} for k, v in auth_user.items()],
            }
        ],
    }
    STATE.write_text(json.dumps(state))
    STATE.chmod(0o600)
    print(f"wrote {STATE}")


if __name__ == "__main__":
    main()
