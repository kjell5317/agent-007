"""Answers webclicker polls by clicking one of the first two answer buttons at random."""

import logging
import os
import random
import sys
import time
from urllib.parse import urlsplit

from playwright.sync_api import ConsoleMessage, Page, TimeoutError, sync_playwright

URL = os.environ.get("WEBCLICKER_URL", "https://webclicker.web.app/student/n73QmzX1YjDucKR3T6Zr")
COURSE_PATH = urlsplit(URL).path
STATE = os.environ.get("WEBCLICKER_STATE", "/state.json")
# react-router's history listens for popstate, so this routes without a reload.
ROUTE_TO = (
    "path => { history.pushState(null, '', path); dispatchEvent(new PopStateEvent('popstate')); }"
)
# The course page reads courses[courseID] unguarded, so routing there before
# the course list has loaded throws during render and blanks the page for good.
COURSE_LIST_ITEM = ".MuiDrawer-paper .MuiListItem-container"
# The A-E group is the page's only ButtonGroup. Below 960px wide it moves into
# a list item of its own, so a positional selector only fits one layout.
ANSWER_GROUP = ".MuiButtonGroup-root"
ENABLED_ANSWERS = f"{ANSWER_GROUP} > button:nth-child(-n+2):enabled"
SELECTED_ANSWER = f"{ANSWER_GROUP} > button.MuiButton-contained"
POLL_SECONDS = 2
HEARTBEAT_SECONDS = 3600

log = logging.getLogger("webclicker")


def open_course(page: Page) -> None:
    log.info("opening %s", URL)
    page.goto(URL)
    # The app keeps its state in memory only: every full load starts at
    # /login/ and, once Firebase restores the session, lands on the bare
    # /student/ list with the course ID from the URL dropped.
    try:
        page.wait_for_url("**/student/", timeout=60_000)
    except TimeoutError:
        sys.exit(f"stuck on {page.url}: no valid session in state.json, rerun login.py")
    try:
        page.locator(COURSE_LIST_ITEM).first.wait_for(timeout=60_000)
    except TimeoutError:
        sys.exit(f"course list never loaded on {page.url}")
    log.info("routing %s -> %s", page.url, COURSE_PATH)
    page.evaluate(ROUTE_TO, COURSE_PATH)


def snapshot(page: Page) -> dict[str, object]:
    return {
        "path": urlsplit(page.url).path,
        "rendered": page.locator("#root > *").count() > 0,
        "header": " ".join(" ".join(page.locator("header").all_inner_texts()).split()),
        "buttons": "".join(page.locator(f"{ANSWER_GROUP} > button").all_inner_texts()),
        "enabled": "".join(page.locator(f"{ANSWER_GROUP} > button:enabled").all_inner_texts()),
        "selected": "".join(page.locator(SELECTED_ANSWER).all_inner_texts()),
        "dialogs": [" ".join(t.split()) for t in page.locator("[role=dialog]").all_inner_texts()],
    }


def forward_console(message: ConsoleMessage) -> None:
    # The app console.logs every Firestore read, so only forward what's
    # flagged as a problem: Firestore connection trouble, React errors.
    # Stack frames point into minified bundles, so the first line is enough.
    if message.type in ("error", "warning"):
        log.warning("console.%s: %s", message.type, message.text.partition("\n")[0])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-dev-shm-usage"])
        page = browser.new_context(storage_state=STATE).new_page()
        page.on("console", forward_console)
        page.on("pageerror", lambda error: log.error("page error: %s", error))
        page.on("crash", lambda _: log.error("page crashed"))
        open_course(page)
        answers = page.locator(ENABLED_ANSWERS)
        selected = page.locator(SELECTED_ANSWER)
        answered_url = None
        last_state, last_logged = None, 0.0
        while True:
            state = snapshot(page)
            if state != last_state or time.monotonic() - last_logged >= HEARTBEAT_SECONDS:
                log.info(" ".join(f"{key}={value!r}" for key, value in state.items()))
                last_state, last_logged = state, time.monotonic()
            url = page.url
            if not state["rendered"] or not urlsplit(url).path.startswith(COURSE_PATH):
                log.warning("lost the course page, reopening")
                open_course(page)
            # The URL ends in the live poll's ID, so a new question means a new
            # URL. A geofence-rejected vote never renders as selected, so the
            # selection check alone would re-vote every few seconds.
            elif url != answered_url and (n := answers.count()) and not selected.count():
                button = answers.nth(random.randrange(n))
                log.info("clicking %r on %s", button.inner_text(), url)
                button.click()
                answered_url = url
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
