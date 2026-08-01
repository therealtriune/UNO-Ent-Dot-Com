#!/usr/bin/env python3
"""
UNO Entertainment — missing thumbnail auto-fixer.

This is the "automatic checker" that runs every 30 minutes as part of the
Update UNO Entertainment Feed workflow, right after fetch_feeds.py and
before build_site.py. It scans articles.json for any article with no
thumbnail (fetch_feeds.py couldn't find one at ingest time -- usually a
source page that was slow/blocked/malformed at that exact moment) and
simply retries the same og:image/Pinterest/wp-content extraction logic
against the live page.

Why this exists: a source page failing once doesn't mean it'll fail again.
A HotNewHipHop article, for example, almost always HAS a valid og:image --
fetch_feeds.py's request to it just occasionally times out or hits a
transient hiccup during the RSS pull. Retrying a few minutes later on its
own page (rather than requiring someone to notice and patch it by hand)
fixes the vast majority of these automatically. Whatever's left after a
retry gets left null and flagged by build_site.py's check_thumbnails() in
thumbnail_flags.json for manual attention, same as before.

Nothing here ever removes or overwrites an article -- it only ever fills in
a thumbnail field that was previously null. Existing (including manually
patched) thumbnails are never touched.

Requires: pip install requests
"""

import json

from fetch_feeds import fetch_real_thumbnail


def main():
    with open("articles.json") as f:
        articles = json.load(f)

    missing = [a for a in articles if not a.get("thumbnail")]
    print(f"Checking {len(missing)} article(s) with no thumbnail...")

    fixed = 0
    for a in missing:
        thumb = fetch_real_thumbnail(a.get("link"), debug=True)
        if thumb:
            a["thumbnail"] = thumb
            fixed += 1
            print(f"  [fixed] {a['title'][:60]}")
        else:
            print(f"  [still missing] {a['title'][:60]}")

    if fixed:
        with open("articles.json", "w") as f:
            json.dump(articles, f, indent=2)
        print(f"\nFixed {fixed} of {len(missing)} missing thumbnail(s). articles.json updated.")
    else:
        print(f"\nNo missing thumbnails could be fixed this run ({len(missing)} still missing).")


if __name__ == "__main__":
    main()
