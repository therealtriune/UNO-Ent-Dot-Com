#!/usr/bin/env python3
"""
UNO Entertainment — feed aggregation script.

Pulls the latest posts from each source's RSS feed, normalizes them into a
common shape (source, title, excerpt, summary, thumbnail, link, date), and
writes the result to articles.json. Run this on a schedule (cron / GitHub
Actions / scheduled task) to keep the site fresh, then re-run build_site.py
to regenerate the homepage and article pages.

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
]

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
THUMBNAIL_TIMEOUT = 5.0

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


# The 5 categories UNO Ent actually filters by. This is deliberately not the
# same list as what source RSS feeds tag things with — feeds throw in labels
# like "Exclusive," "Feature," "Source Sports," etc. that are specific to how
# that publisher organizes their own site. Those tags get read below (as a
# hint) but never turn into their own filter; anything that isn't clearly
# rumors/videos/music/opinion just falls through to "news."
VALID_CATEGORIES = {"news", "rumors", "videos", "music", "opinion"}

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


def fetch_real_thumbnail(article_url: str) -> str | None:
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
            headers={"User-Agent": "Mozilla/5.0 (compatible; UNOEntBot/1.0)"},
        )
        if resp.status_code != 200:
            return None
        html = resp.text
        match = OG_IMAGE_RE.search(html) or OG_IMAGE_RE_ALT.search(html)
        if match:
            candidate = unescape(match.group(1))
            if not looks_like_logo(candidate):
                return candidate

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
    except requests.RequestException:
        pass
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


def fetch_source(name: str, url: str) -> list[dict]:
    parsed = feedparser.parse(url)
    if parsed.bozo and not parsed.entries:
        print(f"  [!] {name}: could not parse feed ({parsed.bozo_exception})")
        return []

    articles = []
    for entry in parsed.entries[:MAX_PER_SOURCE]:
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

        source_tags = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")]
        category = categorize(title, source_tags)

        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        date_iso = (
            datetime(*pub[:6], tzinfo=timezone.utc).isoformat()
            if pub
            else datetime.now(timezone.utc).isoformat()
        )

        link = entry.get("link")
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
    print(f"  [+] {name}: pulled {len(articles)} articles")
    return articles


def main():
    print("Pulling feeds for UNO Entertainment...")
    all_articles = []
    for name, url in SOURCES:
        all_articles.extend(fetch_source(name, url))

    all_articles.sort(key=lambda a: a["date"], reverse=True)

    with open("articles.json", "w") as f:
        json.dump(all_articles, f, indent=2)

    missing = [a["link"] for a in all_articles if not a.get("thumbnail")]
    print(f"\nWrote {len(all_articles)} articles to articles.json")
    if missing:
        print(f"[!] {len(missing)} article(s) have no thumbnail (needs a manual fix):")
        for link in missing:
            print(f"    {link}")


if __name__ == "__main__":
    main()
