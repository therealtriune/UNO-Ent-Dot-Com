#!/usr/bin/env python3
"""
UNO Entertainment Ã¢ÂÂ feed aggregation script.

Pulls the latest posts from each source's RSS feed, normalizes them into a
common shape (source, title, excerpt, summary, thumbnail, link, date), and
merges anything new into articles.json. Run this on a schedule (cron /
GitHub Actions / scheduled task) to keep the site fresh, then re-run
build_site.py to regenerate the homepage and article pages.

RETENTION POLICY: articles.json is a permanent archive, not a rolling
snapshot. Every run loads whatever's already on disk, skips any article
whose link it's already seen (existing fields Ã¢ÂÂ including any manually
patched thumbnail Ã¢ÂÂ are left untouched), and only fetches/appends articles
it hasn't recorded before. Nothing is ever dropped here just because it
aged out of a source's RSS feed. The one and only removal rule lives in
check_links.py: an article is deleted from the site if and only if its
outbound link is confirmed dead (404/410/451). Run that script separately
(the "Check For Dead Links" workflow already does this on a schedule).

WHERE "SUMMARY" COMES FROM (read this if you're building toward in-house):
  Every article page shows a short UNO Ent-voiced summary before linking out
  to the original story. Right now generate_summary() below does a simple
  extractive summary Ã¢ÂÂ it pulls the opening sentences of the full article
  body. That's a reasonable default, but it's still close to the source's
  own words.

  The natural upgrade path, in order:
    1. Swap generate_summary() for a call to an LLM (e.g. the Claude API)
       that reads full_text and writes 2-3 original sentences in UNO Ent's
       voice. This is a small change Ã¢ÂÂ see the commented example in
       generate_summary() below.
    2. Once UNO Ent has writers, replace generate_summary()'s output with
       actual staff-written summaries or full original articles, stored
       against the same slug. Nothing else in the site needs to change Ã¢ÂÂ
       build_site.py just renders whatever's in the "summary" field.

Requires: pip install feedparser
(feedparser handles gzip/compressed feeds automatically Ã¢ÂÂ some of the sources
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
    # Sports desk: doesn't run a taggable RSS feed the way the hip-hop blogs
    # do (no rumor/video/album tags to hook categorize() into), and
    # everything from these four feeds belongs in one place regardless of
    # what it's about -- so it's routed straight to "sports" via
    # SOURCE_CATEGORY_OVERRIDE below instead of going through categorize().
    #
    # These were ESPN's own NBA/NFL/boxing/MMA RSS feeds originally, but
    # ESPN's servers return an HTTP 202 "challenge" response to every fetch
    # from GitHub Actions' runner IPs -- confirmed to be IP-based (not a
    # header/UA problem: a realistic browser UA/Accept/Referer set made no
    # difference), and a public-proxy fallback (allorigins, codetabs,
    # corsproxy, thingproxy) was tried and also failed (timeouts / 521 / 403
    # / connection errors -- free proxies are simply too unreliable for
    # this). Swapped to Yahoo Sports' equivalent feeds instead, which don't
    # block automated fetches and cover the same four verticals.
    ("Yahoo Sports NBA", "https://sports.yahoo.com/nba/rss.xml"),
    ("Yahoo Sports NFL", "https://sports.yahoo.com/nfl/rss.xml"),
    ("Yahoo Sports Boxing", "https://sports.yahoo.com/boxing/rss.xml"),
    ("Yahoo Sports MMA", "https://sports.yahoo.com/mma/rss.xml"),
    ("TMZ", "https://www.tmz.com/rss.xml"),
]

# Sources listed here always get this category, bypassing categorize()
# entirely -- for feeds where every story belongs in the same bucket rather
# than needing per-article classification.
SOURCE_CATEGORY_OVERRIDE = {
    "Yahoo Sports NBA": "sports",
    "Yahoo Sports NFL": "sports",
    "Yahoo Sports Boxing": "sports",
    "Yahoo Sports MMA": "sports",
    # TMZ is a celebrity gossip outlet -- nearly everything it publishes is
    # rumor/gossip content by nature, so route it straight to "rumors"
    # rather than running it through categorize()'s keyword matching, which
    # would default most of it to "news" since TMZ headlines rarely contain
    # our rumor keywords.
    "TMZ": "rumors",
}

MAX_PER_SOURCE = 12
EXCERPT_LENGTH = 160
SUMMARY_SENTENCES = 3

TAG_RE = re.compile(r"<[^>]+>")
IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"')
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
SLUG_RE = re.compile(r"[^a-z0-9]+")
# Matches both <meta property="og:image" content="..."> (the OpenGraph spec)
# and <meta name="og:image" content="..."> -- HotNewHipHop's SEO plugin emits
# the "name" variant instead of "property", which the original property-only
# regex silently missed (confirmed via the debug logging in
# fetch_real_thumbnail(): the tag was present in the HTML on every failing
# HotNewHipHop page, just never matched).
OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)
OG_IMAGE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']', re.IGNORECASE
)
# Fallback for the rare case where a feed entry's own <title>/<summary> come
# back empty -- confirmed on two Yahoo Sports posts (a /videos/ post and a
# /slideshows/ post): their RSS entries carry no title and no description at
# all, even though the page itself has both via og:title/og:description.
# Only used by fetch_real_title_and_excerpt() below, which is only called
# when the normal RSS-derived title is empty, so this doesn't add a request
# to the common path.
OG_TITLE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)
OG_TITLE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:title["\']', re.IGNORECASE
)
OG_DESC_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
OG_DESC_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:description|description)["\']',
    re.IGNORECASE,
)
PINTEREST_MEDIA_RE = re.compile(
    r'pinterest\.com/pin/create/button[^"\']*[?&]media=([^"\'&]+)', re.IGNORECASE
)
CONTENT_IMG_RE = re.compile(
    r'<img[^>]+src=["\']([^"\']+wp-content/uploads/[^"\']+)["\']', re.IGNORECASE
)
# Some Yahoo Sports articles (confirmed on the "UFC Belgrade" video-recap
# posts) are really just a wrapper around an embedded YouTube clip -- no
# og:image, no wp-content image, and no plain <img> anywhere in the page or
# the RSS content:encoded, just an <iframe src="youtube.com/embed/...">
# (or occasionally a bare youtu.be link). YouTube guarantees a default
# thumbnail at this URL for every public video ID, so this is a reliable
# last-resort source for those pages.
YOUTUBE_EMBED_RE = re.compile(
    r'youtube(?:-nocookie)?\.com/embed/([a-zA-Z0-9_-]{6,})|youtu\.be/([a-zA-Z0-9_-]{6,})',
    re.IGNORECASE,
)
THUMBNAIL_TIMEOUT = 8.0


def youtube_thumbnail_from_html(html: str) -> str | None:
    """Find an embedded YouTube video (iframe src or youtu.be link) in a
    blob of HTML and return its default thumbnail URL, or None if no
    YouTube embed is present."""
    if not html:
        return None
    match = YOUTUBE_EMBED_RE.search(html)
    if not match:
        return None
    video_id = match.group(1) or match.group(2)
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

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

    Default behavior: extractive Ã¢ÂÂ take the first few sentences of the full
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
    original summary in UNO Ent's own voice Ã¢ÂÂ which is the version worth
    keeping once you're ready to make this feel less like syndication and
    more like your own editorial desk.
    """
    if not full_text:
        return fallback_excerpt

    sentences = SENTENCE_SPLIT_RE.split(full_text)
    summary = " ".join(sentences[:SUMMARY_SENTENCES]).strip()
    return summary or fallback_excerpt


# The 6 categories UNO Ent actually filters by. This is deliberately not the
# same list as what source RSS feeds tag things with Ã¢ÂÂ feeds throw in labels
# like "Exclusive," "Feature," "Source Sports," etc. that are specific to how
# that publisher organizes their own site. Those tags get read below (as a
# hint) but never turn into their own filter; anything that isn't clearly
# rumors/videos/music/opinion just falls through to "news." "sports" is the
# one exception: it's never reached via categorize() below -- it's assigned
# directly via SOURCE_CATEGORY_OVERRIDE for the sports feeds.
VALID_CATEGORIES = {"news", "rumors", "videos", "music", "opinion", "sports"}

# RSS <category> tags Ã¢ÂÂ our taxonomy. Left side is lowercased substring match
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
      3. Default to "news" Ã¢ÂÂ the safe fallback for straight reporting,
         legal/business news, and anything else that doesn't clearly fit
         rumors/videos/music/opinion.

    To upgrade this to something smarter than keyword-matching, swap it for
    an LLM call the same way generate_summary() suggests Ã¢ÂÂ a single prompt
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
      4. An embedded YouTube video's default thumbnail, for pages that are
         really just a wrapper around a video clip with no still photo
         anywhere in the markup (see youtube_thumbnail_from_html).

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

        candidate = youtube_thumbnail_from_html(html)
        if candidate:
            return candidate

        if debug:
            # Distinguish "the tag isn't there" from "the tag's there but our
            # regex didn't match its exact attribute order/quoting" -- the
            # fix is completely different depending on which one this is.
            idx = html.lower().find("og:image")
            if idx == -1:
                print(f"    [thumbnail] {article_url}: fetched {len(html)} bytes, no 'og:image' substring anywhere in the HTML")
            else:
                snippet = html[max(0, idx - 80):idx + 120].replace("\n", " ")
                print(f"    [thumbnail] {article_url}: fetched {len(html)} bytes, 'og:image' present but regex didn't match -- context: ...{snippet}...")
    except requests.RequestException as e:
        if debug:
            print(f"    [thumbnail] {article_url}: {type(e).__name__}: {e}")
    return None


def fetch_real_title_and_excerpt(article_url: str) -> tuple[str, str]:
    """
    Fallback for feed entries whose <title>/<summary> came back empty from
    feedparser -- confirmed on Yahoo Sports /videos/ and /slideshows/ posts,
    which apparently ship RSS entries with no title or description field at
    all. Fetches the page once and pulls og:title / og:description out of
    it. Only called from fetch_source() when title is already empty, so this
    never adds a request on the normal path where the feed title is present.
    """
    if not article_url:
        return "", ""
    try:
        resp = requests.get(article_url, timeout=THUMBNAIL_TIMEOUT, headers=REQUEST_HEADERS)
        if resp.status_code != 200:
            return "", ""
        html = resp.text
        title_match = OG_TITLE_RE.search(html) or OG_TITLE_RE_ALT.search(html)
        title = unescape(title_match.group(1)).strip() if title_match else ""
        desc_match = OG_DESC_RE.search(html) or OG_DESC_RE_ALT.search(html)
        excerpt = unescape(desc_match.group(1)).strip() if desc_match else ""
        return title, excerpt
    except requests.RequestException:
        return "", ""


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
    if entry.get("content"):
        candidates.append(youtube_thumbnail_from_html(entry["content"][0].get("value", "")))
    for candidate in candidates:
        if candidate and not looks_like_logo(candidate):
            return candidate
    return None


# Public read-through proxies, tried in order, used only as a fallback when
# a direct feed fetch comes back with 0 entries. ESPN's RSS feeds return an
# HTTP 202 "challenge accepted" response to GitHub Actions' runner IPs no
# matter what headers are sent -- confirmed by testing (see fetch_source()
# below) that a realistic browser UA/Accept/Referer set still gets 0
# entries back, while the exact same URL fetched from a different IP (e.g.
# a developer's machine) returns the feed fine. That's IP-based bot
# mitigation, not a UA problem, so the fix has to be fetching from a
# different origin IP rather than a different header. Each of these
# services fetches the target URL server-side (from their own IP) and
# echoes back the raw response body, so feedparser can parse the *content*
# directly instead of re-requesting the original URL.
FEED_PROXY_TEMPLATES = [
    "https://api.allorigins.win/raw?url={url}",
    "https://api.codetabs.com/v1/proxy?quest={url}",
    "https://corsproxy.io/?url={url}",
    "https://thingproxy.freeboard.io/fetch/{url}",
]


def fetch_via_proxy(name: str, url: str):
    """Try each proxy in FEED_PROXY_TEMPLATES in turn; return the first
    feedparser result that actually contains entries, or None if every
    proxy fails or is itself blocked/down. Never raises -- a bad proxy is
    just skipped, same as a bad direct fetch is skipped by the caller."""
    import urllib.parse

    encoded = urllib.parse.quote(url, safe="")
    for template in FEED_PROXY_TEMPLATES:
        proxy_url = template.format(url=encoded)
        proxy_host = proxy_url.split("/")[2]
        try:
            resp = requests.get(proxy_url, timeout=THUMBNAIL_TIMEOUT, headers=REQUEST_HEADERS)
            if resp.status_code != 200 or not resp.text.strip():
                print(f"    [proxy] {name} via {proxy_host}: HTTP {resp.status_code}")
                continue
            parsed = feedparser.parse(resp.text)
            if parsed.entries:
                print(f"    [proxy] {name} via {proxy_host}: {len(parsed.entries)} entries")
                return parsed
            print(f"    [proxy] {name} via {proxy_host}: 0 entries")
        except requests.RequestException as e:
            print(f"    [proxy] {name} via {proxy_host}: {type(e).__name__}")
            continue
    return None


def fetch_source(name: str, url: str, known_links: set[str]) -> list[dict]:
    """Pull this source's recent entries and build full article records for
    anything not already in known_links. Entries whose link is already
    known are skipped entirely (no thumbnail fetch, no summary generation)
    -- they're already archived in articles.json and this function never
    touches or re-derives their existing fields."""
    parsed = feedparser.parse(url, request_headers=FEED_REQUEST_HEADERS)
    if not parsed.entries:
        status = getattr(parsed, "status", "?")
        reason = (
            f"could not parse feed ({parsed.bozo_exception})"
            if parsed.bozo
            else f"0 entries returned (HTTP {status})"
        )
        print(f"  [!] {name}: {reason} -- retrying via proxy")
        proxied = fetch_via_proxy(name, url)
        if proxied and proxied.entries:
            parsed = proxied
        else:
            print(f"  [!] {name}: proxy fallback also failed -- skipping this run")
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

        if not title:
            fallback_title, fallback_excerpt = fetch_real_title_and_excerpt(link)
            title = fallback_title
            if not excerpt:
                excerpt = fallback_excerpt[:EXCERPT_LENGTH].rstrip()
            if not title:
                # Feed gave no title AND the page itself has no og:title --
                # skip rather than archive an untitled/unslugged record that
                # would collide with every other untitled record at the ""
                # slug. Rare; will be picked up on a later run if the page
                # adds a title, since it's still unseen (not in known_links).
                print(f"  [!] {name}: no title for {link} -- skipping")
                continue

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

    # Links confirmed dead by check_links.py get recorded in dead_links.json so
    # they're never re-added -- without this, a source's RSS feed can keep
    # serving a stale entry for a page that 404s (confirmed with the
    # HotNewHipHop Vini Jr/Jay-Z sneaker post: check_links.py removed it, then
    # the very next run re-added it straight from the feed, since removal
    # alone doesn't stop a source from continuing to list a dead link).
    try:
        with open("dead_links.json") as f:
            dead_links = set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        dead_links = set()

    known_links = {a["link"] for a in existing if a.get("link")} | dead_links
    starting_count = len(existing)

    new_articles = []
    for name, url in SOURCES:
        new_articles.extend(fetch_source(name, url, known_links))

    # Sports was drowning out every other category -- 407 of 842 articles
    # (48%) were sports before this went in, driven by four high-volume
    # Yahoo Sports feeds vastly outproducing every hip-hop/culture source.
    # Going forward, each run reshapes its own batch of newly-fetched
    # articles toward a fixed target mix instead of just appending
    # everything a feed happened to publish that hour. Categories that are
    # naturally scarce (opinion, videos) simply keep whatever they
    # produced -- the cap only ever bites on oversupplied categories.
    TARGET_MIX_PCT = {
    "news": 32,
    "music": 18,
    "rumors": 17,
    "videos": 18,
    "sports": 5,
    "opinion": 10,
    }
    total_fetched = len(new_articles)
    if total_fetched:
        by_category = {}
        for a in new_articles:
            by_category.setdefault(a["category"], []).append(a)
        balanced = []
        deferred = 0
        for cat, articles in by_category.items():
            pct = TARGET_MIX_PCT.get(cat, 0)
            target = (total_fetched * pct + 99) // 100
            if len(articles) > target:
                articles.sort(key=lambda a: a["date"], reverse=True)
                deferred += len(articles) - target
                articles = articles[:target]
            balanced.extend(articles)
        if deferred:
            print(f"[balance] deferred {deferred} article(s) this run to keep the category mix on target")
        new_articles = balanced

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
