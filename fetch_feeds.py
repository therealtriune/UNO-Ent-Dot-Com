#!/usr/bin/env python3
"""
UNO Entertainment — feed aggregation script.

Pulls the latest posts from each source's RSS feed, normalizes them into a
common shape (source, title, excerpt, summary, thumbnail, link, date), and
merges anything new into articles.json. Run this on a schedule (cron /
GitHub Actions / scheduled task) to keep the site fresh, then re-run
build_site.py to regenerate the homepage and article pages.

RETENTION POLICY: articles.json is a permanent archive, not a rolling
snapshot. Every run loads whatever's already on disk, skips any article
whose link it's already seen (existing fields — including any manually
patched thumbnail — are left untouched), and only fetches/appends articles
it hasn't recorded before. Nothing is ever dropped here just because it
aged out of a source's RSS feed. The one and only removal rule lives in
check_links.py: an article is deleted from the site if and only if its
outbound link is confirmed dead (404/410/451). Run that script separately
(the "Check For Dead Links" workflow already does this on a schedule).

WHERE "SUMMARY" COMES FROM (read this if you're building toward in-house):
  Every article page shows a short UNO Ent-voiced summary before linking out
  to the original story. Right now generate_summary() below does a simple
  extractive summary — it pulls the opening sentences of the full article
  body. That's a reasonable default, but it's still close to the source's
  own words.

  The natural upgrade path, in order:
    1. Swap generate_summary() for a call to an LLM (e.g. the Claude API)
       that reads full_text and writes 2-3 original sentences in UNO Ent's
       voice. This is a small change — see the commented example in
       generate_summary() below.
    2. Once UNO Ent has writers, replace generate_summary()'s output with
       actual staff-written summaries or full original articles, stored
       against the same slug. Nothing else in the site needs to change —
       build_site.py just renders whatever's in the "summary" field.

Requires: pip install feedparser
(feedparser handles gzip/compressed feeds automatically — some of the sources
below serve compressed RSS that simple HTTP fetchers can choke on, so don't
swap this out for a bare requests.get() without decompression handling.)
"""

import json
import re
from datetime import datetime, timezone
from html import unescape

import feedparser
import requests

# Each entry: (Display name, feed URL)
SOURCES = [
    ("XXL", "https://www.xxlmag.com/feed"),
    ("HotNewHipHop", "https://www.hotnewhiphop.com/feed"),
    ("The Source", "https://thesource.com/feed"),
    ("AllHipHop", "https://allhiphop.com/feed"),
    ("Hollywood Unlocked", "https://hollywoodunlocked.com/feed"),
    ("The Shade Room", "https://theshaderoom.com/feed"),
    ("Underground Hip Hop Blog", "https://undergroundhiphopblog.com/feed"),
    # Sports desk: ESPN doesn't run a taggable RSS feed the way the hip-hop
    # blogs do (no rumor/video/album tags to hook categorize() into), and
    # everything from these four feeds belongs in one place regardless of
    # what it's about -- so it's routed straight to "sports" via
    # SOURCE_CATEGORY_OVERRIDE below instead of going through categorize().
    ("ESPN NBA", "https://www.espn.com/espn/rss/nba/news"),
    ("ESPN NFL", "https://www.espn.com/espn/rss/nfl/news"),
    ("ESPN Boxing", "https://www.espn.com/espn/rss/boxing/news"),
    ("ESPN MMA", "https://www.espn.com/espn/rss/mma/news"),
]

# Sources listed here always get this category, bypassing categorize()
# entirely -- for feeds where every story belongs in the same bucket rather
# than needing per-article classification.
SOURCE_CATEGORY_OVERRIDE = {
    "ESPN NBA": "sports",
    "ESPN NFL": "sports",
    "ESPN Boxing": "sports",
    "ESPN MMA": "sports",
}

MAX_PER_SOURCE = 12
EXCERPT_LENGTH = 160
SUMMARY_SENTENCES = 3

TAG_RE = re.compile(r"<[^>]+>")
IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"')
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
SLUG_RE = re.compile(r"[^a-z0-9]+")
OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)
OG_IMAGE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.IGNORECASE
)
PINTEREST_MEDIA_RE = re.compile(
    r'pinterest\.com/pin/create/button[^"\']*[?&]media=([^"\'&]+)', re.IGNORECASE
)
CONTENT_IMG_RE = re.compile(
    r'<img[^>]+src=["\']([^"\']+wp-content/uploads/[^"\']+)["\']', re.IGNORECASE
)
THUMBNAIL_TIMEOUT = 8.0

# A number of sources (ESPN in particular, and occasionally HotNewHipHop)
# sit behind bot/WAF protection that's picky about what's fetching them.
# feedparser's own default User-Agent ("python-feedparser/...") and the old
# self-identifying "UNOEntBot/1.0" UA below both read as obvious bots and
# get silently dropped (0 entries / connection refused) by some of these
# WAFs -- especially from a datacenter IP range like GitHub Actions runners
# use. Presenting a realistic desktop-Chrome UA plus the headers a real
# browser sends alongside it (Accept, Accept-Language, Referer) is the
# standard mitigation and is used for every outbound fetch in this file:
# both the RSS pull itself (feedparser's request_headers) and the article
# page fetch in fetch_real_thumbnail().
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}

# Same UA/browser-fingerprint as REQUEST_HEADERS above, but with an Accept
# header that prioritizes XML/RSS mime types over text/html. This matters:
# ESPN's servers do content negotiation on the Accept header, and
# REQUEST_HEADERS' html-first Accept (needed for the article-page thumbnail
# scrape) was causing them to serve back an HTML page instead of the RSS
# feed for that same URL -- feedparser would then choke trying to parse
# that HTML as XML ("mismatched tag"). Used only for the feedparser.parse()
# call in fetch_source(), never for the thumbnail page fetch.
FEED_REQUEST_HEADERS = {
    **REQUEST_HEADERS,
    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
}

# Some sources (The Shade Room and Hollywood Unlocked in particular) serve a
# generic site logo as og:image on a meaningful chunk of their pages, even
# though the page itself displays a real photo in the body. A thumbnail URL
# matching any of these substrings is treated as "not a real photo" and
# rejected, so we fall through to the body-image / RSS fallbacks instead of
# ever putting a logo on the homepage.
LOGO_URL_PATTERNS = [
    "logo-for-white-backgrounds",
    "logo-for-dark-backgrounds",
    "/og.jpg",
    "/og-image",
    "site-logo",
    "sharing-default",
    "default-social",
    "hu-logo",
    "shaderoom-logo",
    "-logo.png",
    "-logo.jpg",
    "/logo.png",
    "/logo.jpg",
    "placeholder",
]


def looks_like_logo(url: str | None) -> bool:
    """True if a thumbnail URL matches a known site-logo/placeholder pattern
    rather than a real article photo."""
    if not url:
        return False
    lowered = url.lower()
    return any(pattern in lowered for pattern in LOGO_URL_PATTERNS)


def clean_text(raw_html: str) -> str:
    """Strip HTML tags and collapse whitespace from a feed description."""
    if not raw_html:
        return ""
    text = unescape(TAG_RE.sub(" ", raw_html))
    return re.sub(r"\s+", " ", text).strip()


def slugify(title: str) -> str:
    return SLUG_RE.sub("-", title.lower()).strip("-")[:70]


def generate_summary(title: str, full_text: str, fallback_excerpt: str) -> str:
    """
    Produce the 2-3 sentence summary shown on the article page, before the
    outbound link to the source.

    Default behavior: extractive — take the first few sentences of the full
    article body. Good enough to ship with, but it stays close to the
    source's own phrasing, which is worth improving on.

    To upgrade to a real rewritten summary, swap this function body for an
    LLM call, e.g.:

        import anthropic
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"Write a 2-3 sentence news summary in a punchy, "
                    f"editorial voice for a hip-hop culture site, based on "
                    f"this article. Do not copy phrasing directly.\n\n"
                    f"Title: {title}\n\nArticle:\n{full_text[:3000]}"
                ),
            }],
        )
        return response.content[0].text.strip()

    That call costs a fraction of a cent per article and gives you an
    original summary in UNO Ent's own voice — which is the version worth
    keeping once you're ready to make this feel less like syndication and
    more like your own editorial desk.
    """
    if not full_text:
        return fallback_excerpt

    sentences = SENTENCE_SPLIT_RE.split(full_text)
    summary = " ".join(sentences[:SUMMARY_SENTENCES]).strip()
    return summary or fallback_excerpt


# The 6 categories UNO Ent actually filters by. This is deliberately not the
# same list as what source RSS feeds tag things with — feeds throw in labels
# like "Exclusive," "Feature," "Source Sports," etc. that are specific to how
# that publisher organizes their own site. Those tags get read below (as a
# hint) but never turn into their own filter; anything that isn't clearly
# rumors/videos/music/opinion just falls through to "news." "sports" is the
# one exception: it's never reached via categorize() below -- it's assigned
# directly via SOURCE_CATEGORY_OVERRIDE for the ESPN feeds.
VALID_CATEGORIES = {"news", "rumors", "videos", "music", "opinion", "sports"}

# RSS <category> tags → our taxonomy. Left side is lowercased substring match
# against the tags a source puts on the entry.
CATEGORY_TAG_MAP = {
    "rumor": "rumors", "gossip": "rumors", "dating": "rumors", "beef": "rumors",
    "video": "videos", "interview": "videos", "podcast": "videos",
    "music": "music", "album": "music", "single": "music", "mixtape": "music",
    "review": "opinion", "opinion": "opinion", "column": "opinion",
}

# Fallback keyword match against the title, used when the source didn't tag
# the entry with anything we recognize.
TITLE_KEYWORD_MAP = [
    (("rumor", "dating", "spark", "split", "boo'd", "romance"), "rumors"),
    (("video", "cypher", "watch", "footage", "freestyle"), "videos"),
    (("album", "single", "song", "track", "mixtape", "releases", "drops"), "music"),
    (("review", "reacts", "reaction", "tears into", "praises", "thoughts on"), "opinion"),
]


def categorize(title: str, source_tags: list[str]) -> str:
    """
    Assigns one of VALID_CATEGORIES. Order of preference:
      1. A source RSS <category> tag that maps cleanly onto our taxonomy.
      2. A keyword match against the title.
      3. Default to "news" — the safe fallback for straight reporting,
         legal/business news, and anything else that doesn't clearly fit
         rumors/videos/music/opinion.

    To upgrade this to something smarter than keyword-matching, swap it for
    an LLM call the same way generate_summary() suggests — a single prompt
    that returns one of the 5 category keys works well and costs about the
    same as the summary call.
    """
    for tag in source_tags:
        tag_lower = tag.lower()
        for needle, category in CATEGORY_TAG_MAP.items():
            if needle in tag_lower:
                return category

    title_lower = title.lower()
    for keywords, category in TITLE_KEYWORD_MAP:
        if any(k in title_lower for k in keywords):
            return category

    return "news"


def fetch_real_thumbnail(article_url: str, debug: bool = False) -> str | None:
    """
    Fetch the article page once and try, in order, to find a real photo
    (never a site logo/placeholder) to use as the thumbnail:

      1. og:image meta tag -- the same high-resolution hero image the source
         site uses when the story is shared on social media. Consistently
         better (and more consistently present) than whatever thumbnail
         happens to be embedded in the RSS feed itself: some feeds (e.g.
         HotNewHipHop's) don't carry a thumbnail at all, and others carry a
         small WordPress-generated crop (e.g. "-300x300.jpg") instead of the
         real hero image. Rejected if it matches a known logo pattern (see
         looks_like_logo) -- The Shade Room and Hollywood Unlocked both serve
         a generic site logo as og:image on some pages even though the page
         itself shows a real photo elsewhere.
      2. The `media=` query param on a Pinterest share-button link embedded
         in the page -- a reliable real-photo source on pages where og:image
         is a logo.
      3. The first wp-content/uploads image referenced anywhere in the page
         HTML (a decent proxy for "the actual article photo" on WordPress
         sites, which is what every source here runs on).

    Returns None if the page can't be fetched or none of the above find a
    non-logo image -- the caller falls back to extract_thumbnail() (RSS
    fields) after this, and if that also comes up empty the article simply
    gets no thumbnail rather than a logo.
    """
    if not article_url:
        return None
    try:
        resp = requests.get(
            article_url,
            timeout=THUMBNAIL_TIMEOUT,
            headers=REQUEST_HEADERS,
        )
        if resp.status_code != 200:
            if debug:
                print(f"    [thumbnail] {article_url}: HTTP {resp.status_code}")
            return None
        html = resp.text
        match = OG_IMAGE_RE.search(html) or OG_IMAGE_RE_ALT.search(html)
        if match:
            candidate = unescape(match.group(1))
            if not looks_like_logo(candidate):
                return candidate
            if debug:
                print(f"    [thumbnail] {article_url}: og:image rejected as logo ({candidate})")

        match = PINTEREST_MEDIA_RE.search(html)
        if match:
            candidate = unescape(requests.utils.unquote(match.group(1)))
            if not looks_like_logo(candidate):
                return candidate

        match = CONTENT_IMG_RE.search(html)
        if match:
            candidate = unescape(match.group(1))
            if not looks_like_logo(candidate):
                return candidate

        if debug:
            print(f"    [thumbnail] {article_url}: fetched {len(html)} bytes, no usable image found")
    except requests.RequestException as e:
        if debug:
            print(f"    [thumbnail] {article_url}: {type(e).__name__}: {e}")
    return None


def extract_thumbnail(entry) -> str | None:
    """Fallback: try the common places a thumbnail shows up in RSS/Media RSS,
    used only if fetch_real_thumbnail() above couldn't get a real photo.
    Same logo rejection applies here -- an RSS-embedded thumbnail can be a
    logo just as easily as an og:image can."""
    candidates = []
    if "media_thumbnail" in entry and entry.media_thumbnail:
        candidates.append(entry.media_thumbnail[0].get("url"))
    if "media_content" in entry and entry.media_content:
        candidates.append(entry.media_content[0].get("url"))
    for key in ("summary", "description"):
        html = entry.get(key, "")
        match = IMG_SRC_RE.search(html)
        if match:
            candidates.append(match.group(1))
    for candidate in candidates:
        if candidate and not looks_like_logo(candidate):
            return candidate
    return None


def fetch_source(name: str, url: str, known_links: set[str]) -> list[dict]:
    """Pull this source's recent entries and build full article records for
    anything not already in known_links. Entries whose link is already
    known are skipped entirely (no thumbnail fetch, no summary generation)
    -- they're already archived in articles.json and this function never
    touches or re-derives their existing fields."""
    parsed = feedparser.parse(url, request_headers=FEED_REQUEST_HEADERS)
    if parsed.bozo and not parsed.entries:
        print(f"  [!] {name}: could not parse feed ({parsed.bozo_exception})")
        return []
    if not parsed.entries:
        status = getattr(parsed, "status", "?")
        print(f"  [!] {name}: 0 entries returned (HTTP {status}) -- likely blocked")
        return []

    articles = []
    skipped = 0
    for entry in parsed.entries[:MAX_PER_SOURCE]:
        link = entry.get("link")
        if link and link in known_links:
            skipped += 1
            continue

        title = clean_text(entry.get("title", ""))
        excerpt = clean_text(entry.get("summary", ""))[:EXCERPT_LENGTH].rstrip()

        # content:encoded (full article body) if the feed provides it,
        # otherwise fall back to the RSS description.
        full_text = ""
        if entry.get("content"):
            full_text = clean_text(entry["content"][0].get("value", ""))
        if not full_text:
            full_text = clean_text(entry.get("summary", ""))

        summary = generate_summary(title, full_text, excerpt)

        if name in SOURCE_CATEGORY_OVERRIDE:
            category = SOURCE_CATEGORY_OVERRIDE[name]
        else:
            source_tags = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")]
            category = categorize(title, source_tags)

        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        date_iso = (
            datetime(*pub[:6], tzinfo=timezone.utc).isoformat()
            if pub
            else datetime.now(timezone.utc).isoformat()
        )

        thumbnail = fetch_real_thumbnail(link) or extract_thumbnail(entry)

        articles.append(
            {
                "source": name,
                "title": title,
                "slug": slugify(title),
                "excerpt": excerpt,
                "summary": summary,
                "category": category,
                "link": link,
                "thumbnail": thumbnail,
                "date": date_iso,
            }
        )
        if link:
            known_links.add(link)  # guard against dupes within this same run

    print(f"  [+] {name}: {len(articles)} new, {skipped} already archived")
    return articles


def main():
    print("Pulling feeds for UNO Entertainment...")

    try:
        with open("articles.json") as f:
            existing = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        existing = []

    known_links = {a["link"] for a in existing if a.get("link")}
    starting_count = len(existing)

    new_articles = []
    for name, url in SOURCES:
        new_articles.extend(fetch_source(name, url, known_links))

    all_articles = existing + new_articles
    all_articles.sort(key=lambda a: a["date"], reverse=True)

    with open("articles.json", "w") as f:
        json.dump(all_articles, f, indent=2)

    missing = [a["link"] for a in new_articles if not a.get("thumbnail")]
    print(
        f"\n{starting_count} archived + {len(new_articles)} new = "
        f"{len(all_articles)} articles written to articles.json"
    )
    if missing:
        print(f"[!] {len(missing)} newly-fetched article(s) have no thumbnail (needs a manual fix):")
        for link in missing:
            print(f"    {link}")


if __name__ == "__main__":
    main()
