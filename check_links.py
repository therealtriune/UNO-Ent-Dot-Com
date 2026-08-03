#!/usr/bin/env python3
"""
UNO Entertainment — dead link checker.

Checks every article's outbound "link" (the source URL) and removes any
article whose link is confirmed dead (404 / 410 / 451 — "this page is
genuinely gone") from articles.json. Run build_site.py afterward to drop
the removed article's page and card from the live site.

Deliberately conservative: only CONFIRMED-dead responses cause a removal.
Timeouts, connection errors, and 403s are logged as warnings but the
article is kept — those are usually a source site's bot-blocking or a
transient outage, not proof the story is actually gone. A story that
fails intermittently will just get re-checked next run.

Requires: pip install requests

Usage:
    python3 check_links.py                 # check + update articles.json
    python3 check_links.py --dry-run        # check only, report, no changes
"""

import argparse
import json
import sys
import time

import requests

TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (compatible; UNOEntLinkChecker/1.0)"
CONFIRMED_DEAD_CODES = {404, 410, 451}
DELAY_BETWEEN_REQUESTS = 0.5  # be polite to source sites


def check_url(url: str) -> tuple[str, int | None]:
    """
    Returns (status, code):
      status is one of "ok", "dead", "warn"
      code is the HTTP status code if we got one, else None
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.head(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        # Some servers don't support HEAD properly (405, or a suspicious 403) — confirm with GET.
        if resp.status_code in (405, 403, 501):
            resp = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True, stream=True)
            resp.close()
    except requests.RequestException as e:
        return "warn", None

    code = resp.status_code
    if code in CONFIRMED_DEAD_CODES:
        return "dead", code
    if code >= 400:
        return "warn", code
    return "ok", code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="report only, don't modify articles.json")
    args = parser.parse_args()

    with open("articles.json") as f:
        articles = json.load(f)

    kept = []
    removed = []
    warned = []

    print(f"Checking {len(articles)} links...\n")
    for a in articles:
        url = a.get("link")
        if not url:
            kept.append(a)
            continue

        status, code = check_url(url)
        label = a["title"][:60]

        if status == "dead":
            print(f"  [DEAD  {code}] {label}")
            removed.append(a)
        elif status == "warn":
            code_str = code if code else "no response / timeout"
            print(f"  [WARN  {code_str}] {label} — kept, will recheck next run")
            warned.append(a)
            kept.append(a)
        else:
            kept.append(a)

        time.sleep(DELAY_BETWEEN_REQUESTS)

    print(f"\n{len(kept)} kept, {len(removed)} confirmed dead, {len(warned)} warned (kept, will recheck).")

    if removed:
        print("\nRemoved (confirmed dead):")
        for a in removed:
            print(f"  - {a['title']}  ({a['link']})")

    if args.dry_run:
        print("\nDry run — articles.json not modified.")
        sys.exit(0)

    if removed:
        with open("articles.json", "w") as f:
            json.dump(kept, f, indent=2)
        print(f"\nUpdated articles.json — removed {len(removed)} dead link(s).")
        print("Run build_site.py to regenerate the site without them.")

        # Record these links permanently so fetch_feeds.py never re-adds them.
        # Without this, a source's RSS feed can keep serving a stale entry for
        # a page that's confirmed 404/410/451 -- removing it here alone
        # doesn't stop the very next feed pull from pulling it right back in.
        try:
            with open("dead_links.json") as f:
                dead_links = set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            dead_links = set()
        dead_links.update(a["link"] for a in removed if a.get("link"))
        with open("dead_links.json", "w") as f:
            json.dump(sorted(dead_links), f, indent=2)
        print(f"Recorded {len(removed)} link(s) in dead_links.json so they won't come back.")
    else:
        print("\nNo dead links found — articles.json unchanged.")


if __name__ == "__main__":
    main()
