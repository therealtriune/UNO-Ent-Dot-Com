#!/usr/bin/env python3
"""
UNO Entertainment -- feed aggregation script.

Pulls the latest posts from each source's RSS feed, normalizes them into a
common shape (source, title, excerpt, summary, thumbnail, link, date), and
merges anything new into articles.json. Run this on a schedule (cron /
GitHub Actions / scheduled task) to keep the site fresh, then re-run
build_site.py to regenerate the homepage and article pages.

RETENTION POLICY: articles.json is a permanent archive, not a rolling
snapshot. Every run loads whatever's already on disk, skips any article
whose link it's already seen (existing fields -- including any manually
patched thumbnail -- are left untouched), and only fetches/appends articles
it hasn't recorded before. Nothing is ever dropped here just because it
aged out of a source's RSS feed. The one and only removal rule lives in
check_links.py: an article is deleted from the site if and only if its
outbound link is confirmed dead (404/410/451). Run that script separately
(the "Check For Dead Links" workflow already does this on a schedule).

WHERE "SUMMARY" COMES FROM (read this if you're building toward in-house):
  Every article page shows a short UNO Ent-voiced summary before linking out
  to the original story. Right now generate_summary() below does a simple
  extractive summary -- it pulls the opening sentences of the full article
  body. That's a reasonable default, but it's still close to the source's
  own words.

  The natural upgrade path, in order:
    1. Swap generate_summary() for a call to an LLM (e.g. the Claude API)
       that reads full_text and writes 2-3 original sentences in UNO Ent's
       voice. This is a small change -- see the commented example in
       generate_summary() below.
    2. Once UNO Ent has writers, replace generate_summary()'s output with
       actual staff-written summaries or full original articles, stored
       against the same slug. Nothing else in the site needs to change --
       build_site.py just renders whatever's in the "summary" field.

Requires: pip install feedparser
(feedparser handles gzip/compressed feeds automatically -- some of the sources
below serve compressed RSS that simple HTTP fetchers can choke on, so don't
swap this out for a bare requests.get() without decompression handling.)
"""

import json
import re
from datetime import datetime, timedelta, timezone
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
# Source feeds (TMZ especially) write copy with a spaced-out ellipsis --
# "word ... word" -- as a house style for joining clauses. Collapsed to a
# tight "word...word" by normalize_copy() below. Matched separately from
# PUNCT_SPACE_RE (and always applied first) so its three dots never get
# treated as a single stray punctuation mark by that second pass.
ELLIPSIS_SPACE_RE = re.compile(r"\s*\.\.\.\s*")
# Stray whitespace before a punctuation mark -- "word , word" -> "word, word".
PUNCT_SPACE_RE = re.compile(r"\s+([.,!?;:])")
# Some source CMSes (confirmed on AllHipHop) leak an internal debug/QA
# annotation straight into their own og:description meta tag, e.g. "...the
# Tupac murder trial. (127 characters)". It's their bug, not ours, but since
# fetch_real_title_and_excerpt() copies og:description verbatim, it was
# passing straight through into UNO Ent's own meta descriptions too. Stripped
# here so it can never leak into an excerpt again, regardless of which
# source it comes from.
DEBUG_CHAR_COUNT_RE = re.compile(r"\s*\(\d+\s*characters?\)\s*$", re.IGNORECASE)
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
# Yahoo Sports articles never carry an og:image tag at all, so they always
# fall through the checks above. Most of them are short wire-service blurbs
# (Sherdog, MMA Junkie one-paragraph items) that genuinely have no article
# photo in the page -- just a small 56px author/source avatar icon served
# through Yahoo's "mysterio" image-resize CDN with a "resizefill_h56" (fixed
# small height) transform in the URL. Longer syndicated pieces (SB Nation,
# USA Today, etc.) do sometimes carry a real hero photo through the same
# CDN, but with a "resizefill_w" (large width) transform instead -- that's
# the one reliable signal that distinguishes a real photo from an avatar
# icon on this CDN, so only that pattern is matched here.
YAHOO_HERO_IMG_RE = re.compile(
    r'<img[^>]+src=["\'](https://s\.yimg\.com/lo/mysterio/api/[^"\']*resizefill_w\d{3,}[^"\']*)["\']',
    re.IGNORECASE,
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
    # Broader catch-all: the previous patterns only matched "logo" right at
    # the end of the filename (e.g. "-logo.png") or as part of a specific
    # known site's naming convention (e.g. "hu-logo", "shaderoom-logo").
    # Hollywood Unlocked's actual site-logo file --
    # "hollywood-unlocked-logo-300x63-1.png" -- has "logo" in the middle of
    # the filename, flanked by hyphens on both sides, so none of the above
    # matched it and it slipped through as a "real" thumbnail on 17
    # articles. "-logo-" catches that shape generically, for any source.
    "-logo-",
]


def looks_like_logo(url: str | None) -> bool:
    """True if a thumbnail URL matches a known site-logo/placeholder pattern
    rather than a real article photo."""
    if not url:
        return False
    lowered = url.lower()
    return any(pattern in lowered for pattern in LOGO_URL_PATTERNS)


# Some sources (The Shade Room, TMZ, HotNewHipHop especially) publish
# headlines with asterisk-censored profanity baked right in -- "F*ck",
# "S***", "B*tch", "A**hole". The site is trying to sell ad space to
# general advertisers, labels, PR firms, and artists, and a headline like
# that is exactly what an automated brand-safety scanner (the kind ad
# platforms run before agreeing to place a buy) flags, whether or not the
# word is technically spelled out. soften_title() below replaces just
# those specific censored roots with a milder word, leaving everything
# else in the headline (including legitimate stylized artist names like
# "diamond*", which doesn't match any of these word-internal patterns)
# untouched. Deliberately an explicit list rather than "any word with an
# asterisk in it" -- that broader rule would also catch stage names.
PROFANITY_SOFTEN_PATTERNS = [
    # f*ck / f**k / f*cking / f*ckin / motherf*cker -- no leading \b since
    # this needs to also match inside a compound word like "motherf*cker".
    # Captures whatever letters follow the censored "ck" (e.g. "ing", "in",
    # "er") and reattaches them to the replacement so the grammar still
    # roughly holds together.
    (re.compile(r"f\*+ck(\w*)", re.IGNORECASE), lambda m: "screw" + (m.group(1) or "")),
    # f**k -- only the "k" survives censorship (u and c both starred out),
    # as opposed to f*ck above where the "ck" survives. Confirmed both
    # styles appear in the archive from different sources.
    (re.compile(r"f\*+k(\w*)", re.IGNORECASE), lambda m: "screw" + (m.group(1) or "")),
    # Bare "f***"/"f**" with no visible "ck" (the whole rest of the word is
    # asterisked out) -- runs after the "ck"-specific rule above so that
    # variant gets first crack at anything with a real "ck" in it. The
    # trailing (?!\w) (rather than \b) is deliberate throughout this list:
    # \b only fires at a word/non-word transition, and an asterisk itself is
    # a non-word character, so "\b" immediately after a run of asterisks
    # silently fails to match whenever the *next* character is also
    # non-word -- which is exactly the common case of a censored word
    # sitting right next to a closing quote mark or comma, e.g. 'bulls***,'
    # or a curly quote in ‘A**’. (?!\w) has no such blind spot.
    (re.compile(r"\bf\*{2,4}(?!\w)", re.IGNORECASE), "screw"),
    (re.compile(r"\bbulls\*+\w*(?!\w)", re.IGNORECASE), "nonsense"),
    # sh*t / sh**t / sh*t-talk -- stem "sh" catches this before the more
    # generic bare "s***" rule below gets a chance to.
    (re.compile(r"\bsh\*+\w*(?!\w)", re.IGNORECASE), "stuff"),
    # s*** / s**** -- fully redacted down to just the first letter, no "h"
    # visible (confirmed on a Robert O'Neil headline and a Crip Mac one).
    (re.compile(r"\bs\*{2,4}(?!\w)", re.IGNORECASE), "stuff"),
    (re.compile(r"\bb\*+tch\b", re.IGNORECASE), "her"),
    (re.compile(r"\bb\*+h\b", re.IGNORECASE), "her"),
    (re.compile(r"\ba\*+hole\b", re.IGNORECASE), "jerk"),
    (re.compile(r"\ba\*+(?!\w)", re.IGNORECASE), "rear"),
    (re.compile(r"\bh\*+e\b", re.IGNORECASE), "her"),
    (re.compile(r"\br\*+pe\b", re.IGNORECASE), "assault"),
]
# Deliberately NOT in the list above: "k*ll" (confirmed only in a headline
# quoting an alleged racist statement verbatim in a news report -- softening
# a direct quote would misrepresent what was actually said/reported, which
# is a bigger problem than the asterisk itself).


def soften_title(title: str) -> str:
    """Replace known asterisk-censored profanity roots in a headline with a
    milder word, for brand-safety reasons -- see PROFANITY_SOFTEN_PATTERNS
    above. A no-op on any title without a "*" in it (the overwhelming
    majority), so this is cheap to call unconditionally. Only matches
    specific known curse-word roots, not "any word with an asterisk in it"
    -- that broader rule would also strip legitimate stylized artist names
    like "diamond*", which this correctly leaves untouched."""
    if not title or "*" not in title:
        return title
    for pattern, replacement in PROFANITY_SOFTEN_PATTERNS:
        title = pattern.sub(replacement, title)
    return title


def normalize_copy(text: str) -> str:
    """Tidy up recurring copy artifacts pulled in verbatim from source
    feeds: a spaced-out ellipsis ("word ... word" -> "word...word"), stray
    whitespace before punctuation ("word , word" -> "word, word"), and a
    trailing "(N characters)" debug annotation some source CMSes leak into
    their own meta tags. Order matters -- the ellipsis pass has to run
    first, otherwise PUNCT_SPACE_RE would trip over the first of the three
    dots."""
    if not text:
        return text
    text = ELLIPSIS_SPACE_RE.sub("...", text)
    text = PUNCT_SPACE_RE.sub(r"\1", text)
    text = DEBUG_CHAR_COUNT_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_text(raw_html: str) -> str:
    """Strip HTML tags and collapse whitespace from a feed description."""
    if not raw_html:
        return ""
    text = unescape(TAG_RE.sub(" ", raw_html))
    text = re.sub(r"\s+", " ", text).strip()
    return normalize_copy(text)


def slugify(title: str) -> str:
    return SLUG_RE.sub("-", title.lower()).strip("-")[:70]


def normalize_title_for_dedupe(title: str) -> str:
    """Collapse a title down to just its lowercase letters/digits, so two
    headlines that are the same story but differ only in punctuation --
    curly vs. straight quotes, an extra exclamation point, whitespace --
    still compare equal. This is intentionally coarser than slugify()
    (which keeps hyphens and a length cap for use as a URL segment) --
    here we want a pure identity check, not something URL-safe."""
    return SLUG_RE.sub("", title.lower())


def republish_dedupe_key(source: str, title: str, date_iso: str) -> str:
    """Identity key for catching a source republishing the exact same
    story under a brand-new URL -- confirmed on The Shade Room, which
    republished "Last Laugh! Scotty..." under two different links with the
    *same* published timestamp, one title using a curly-quote 'Bird' and
    the other a straight-quote "Bird". Link-based dedupe alone let both
    through as separate archive entries.

    Deliberately keyed on (source, normalized title, exact date) rather
    than title alone: several sources run recurring daily features that
    reuse the identical title every time (TMZ's "TMZ Streaming Live..."
    and "Stars and Scars -- You Be the Judge" polls, both confirmed in the
    archive) -- those are genuinely different posts on different days that
    happen to share a title, not duplicates, and must not collide here.
    Requiring the *same* timestamp as well is what tells a true same-minute
    republish apart from a legitimately new post with a recycled headline."""
    return f"{source}|{normalize_title_for_dedupe(title)}|{date_iso}"


def looks_like_slug_title(title: str) -> bool:
    """True if a title is suspiciously slug-shaped -- all lowercase,
    hyphen-separated, no spaces -- which happens on sources whose own
    <title>/og:title tag is itself a URL slug rather than real prose
    (confirmed on a Yahoo Sports/MMA Junkie "reader picks" post, where the
    RSS title field was literally "ufc-330-makhachev-vs-machado-..."). A
    normal human-written title almost always has an uppercase letter or a
    space somewhere, so this heuristic rarely misfires."""
    if not title or " " in title or any(c.isupper() for c in title):
        return False
    return title.count("-") >= 2


def generate_summary(title: str, full_text: str, fallback_excerpt: str) -> str:
    """
    Produce the 2-3 sentence summary shown on the article page, before the
    outbound link to the source.

    Default behavior: extractive -- take the first few sentences of the full
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
    original summary in UNO Ent's own voice -- which is the version worth
    keeping once you're ready to make this feel less like syndication and
    more like your own editorial desk.
    """
    if not full_text:
        return fallback_excerpt

    sentences = SENTENCE_SPLIT_RE.split(full_text)
    summary = " ".join(sentences[:SUMMARY_SENTENCES]).strip()
    return summary or fallback_excerpt


# The 6 categories UNO Ent actually filters by. This is deliberately not the
# same list as what source RSS feeds tag things with -- feeds throw in labels
# like "Exclusive," "Feature," "Source Sports," etc. that are specific to how
# that publisher organizes their own site. Those tags get read below (as a
# hint) but never turn into their own filter; anything that isn't clearly
# rumors/videos/music/opinion just falls through to "news." "sports" is the
# one exception: it's never reached via categorize() below -- it's assigned
# directly via SOURCE_CATEGORY_OVERRIDE for the sports feeds.
VALID_CATEGORIES = {"news", "rumors", "videos", "music", "opinion", "sports"}

# RSS <category> tags -- our taxonomy. Left side is lowercased substring match
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
      3. Default to "news" -- the safe fallback for straight reporting,
         legal/business news, and anything else that doesn't clearly fit
         rumors/videos/music/opinion.

    To upgrade this to something smarter than keyword-matching, swap it for
    an LLM call the same way generate_summary() suggests -- a single prompt
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

        match = YAHOO_HERO_IMG_RE.search(html)
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
        title = normalize_copy(unescape(title_match.group(1)).strip()) if title_match else ""
        desc_match = OG_DESC_RE.search(html) or OG_DESC_RE_ALT.search(html)
        excerpt = normalize_copy(unescape(desc_match.group(1)).strip()) if desc_match else ""
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


def fetch_source(name: str, url: str, known_links: set[str], known_dedupe_keys: set[str]) -> list[dict]:
    """Pull this source's recent entries and build full article records for
    anything not already in known_links or known_titles. Entries whose link
    is already known are skipped entirely (no thumbnail fetch, no summary
    generation) -- they're already archived in articles.json and this
    function never touches or re-derives their existing fields. The
    republish check catches the case where a source republishes the exact
    same story under a brand-new URL (see republish_dedupe_key)."""
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

        title = soften_title(clean_text(entry.get("title", "")))
        excerpt = clean_text(entry.get("summary", ""))[:EXCERPT_LENGTH].rstrip()

        if not title:
            fallback_title, fallback_excerpt = fetch_real_title_and_excerpt(link)
            title = soften_title(fallback_title)
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

        dedupe_key = republish_dedupe_key(name, title, date_iso)
        if dedupe_key in known_dedupe_keys:
            skipped += 1
            continue

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
        known_dedupe_keys.add(dedupe_key)

    print(f"  [+] {name}: {len(articles)} new, {skipped} already archived")
    return articles


# ---------------------------------------------------------------------------
# VladTV -- no RSS feed exists for this source (confirmed: /feed, /rss,
# /rss.xml all return empty/no valid XML, and the homepage's <head> carries
# no <link rel="alternate" type="application/rss+xml"> pointing to one
# either). The homepage itself, however, IS server-rendered with a "Latest
# Videos" grid of entries -- fetched with a plain requests.get() (same as
# every other source here), no JS execution needed. Each individual article
# page is also server-rendered and carries real og:title/og:image tags plus
# a body paragraph summarizing the video's content, which is where the real
# excerpt (not the generic "Watch the full interview..." og:description CTA)
# comes from. This is a scrape, not a feed parse, so it's inherently more
# fragile than the RSS sources above -- if VladTV changes its markup, this
# will start returning 0 entries (caught below the same way an empty RSS
# feed is) rather than raising.
VLADTV_HOMEPAGE = "https://www.vladtv.com/"
VLADTV_ENTRY_RE = re.compile(
    r'<div class="entry" id="entry-(\d+)">.*?<a href="(/article/\d+/[^"]+)"',
    re.IGNORECASE | re.DOTALL,
)
# Absolute publish timestamp shown in an article page's byline, e.g.
# "Aug 19, 2026 4:00 PM". No timezone is ever printed on the page, so this
# is parsed assuming US/Eastern (VladTV's newsroom is US-based) -- an
# approximation, but one that only affects sort granularity within a single
# fetch run, not correctness of the site.
VLADTV_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(\d{1,2}),\s*(\d{4})\s+(\d{1,2}):(\d{2})\s*([AP]M)",
)
VLADTV_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
VLADTV_EASTERN = timezone(timedelta(hours=-4))
VLADTV_BODY_RE = re.compile(r'<div class="article-body"[^>]*>(.*?)(?:<div class="article-meta"|<h2)', re.IGNORECASE | re.DOTALL)
VLADTV_PARA_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
# Boilerplate lines that show up inside .article-body alongside the real
# summary paragraph(s) -- the YouTube-membership paywall CTA, "Part N:"
# cross-links to related interviews, a "--------" divider, and raw ad-slot
# JS that leaks in because the div isn't cleanly scoped to just prose.
VLADTV_BODY_JUNK_PREFIXES = ("watch the full interview", "part 1:", "part 2:", "part 3:")


def fetch_vladtv_article(article_url: str) -> dict | None:
    """Fetch one VladTV article page and pull title (og:title), a real
    excerpt (the narrative paragraph(s) in .article-body, not the generic
    og:description CTA), thumbnail (og:image), and an approximate publish
    date out of it. Returns None if the page can't be fetched or has no
    title -- same "skip rather than archive junk" rule fetch_source() uses."""
    try:
        resp = requests.get(article_url, timeout=THUMBNAIL_TIMEOUT, headers=REQUEST_HEADERS)
        if resp.status_code != 200:
            return None
        html = resp.text
    except requests.RequestException:
        return None

    title_match = OG_TITLE_RE.search(html) or OG_TITLE_RE_ALT.search(html)
    title = normalize_copy(unescape(title_match.group(1)).strip()) if title_match else ""
    if not title:
        return None

    excerpt = ""
    body_match = VLADTV_BODY_RE.search(html)
    if body_match:
        paras = []
        for para_html in VLADTV_PARA_RE.findall(body_match.group(1)):
            text = clean_text(para_html)
            if not text or "freestar" in text.lower():
                continue
            if text.lower().startswith(VLADTV_BODY_JUNK_PREFIXES):
                continue
            if set(text) <= {"-"}:
                continue
            paras.append(text)
        excerpt = " ".join(paras)[:EXCERPT_LENGTH].rstrip()

    thumb_match = OG_IMAGE_RE.search(html) or OG_IMAGE_RE_ALT.search(html)
    thumbnail = unescape(thumb_match.group(1)) if thumb_match else None
    if thumbnail and looks_like_logo(thumbnail):
        thumbnail = None

    date_iso = datetime.now(timezone.utc).isoformat()
    date_match = VLADTV_DATE_RE.search(html)
    if date_match:
        mon_str, day, year, hour, minute, ampm = date_match.groups()
        month = VLADTV_MONTHS.get(mon_str[:3])
        if month:
            hour = int(hour) % 12
            if ampm.upper() == "PM":
                hour += 12
            try:
                dt = datetime(int(year), month, int(day), hour, int(minute), tzinfo=VLADTV_EASTERN)
                date_iso = dt.astimezone(timezone.utc).isoformat()
            except ValueError:
                pass

    return {"title": title, "excerpt": excerpt, "thumbnail": thumbnail, "date": date_iso}


def fetch_vladtv(known_links: set[str], known_dedupe_keys: set[str]) -> list[dict]:
    """VladTV has no RSS feed (confirmed -- see comment above), so this
    scrapes the homepage's server-rendered 'Latest Videos' grid for entry
    links instead of parsing a feed, then fetches each new article's own
    page for title/excerpt/thumbnail/date. Every VladTV story is a video
    interview/commentary clip (an embedded YouTube player on every article
    page), so it's routed straight to the "videos" category rather than
    through categorize()."""
    name = "VladTV"
    try:
        resp = requests.get(VLADTV_HOMEPAGE, timeout=THUMBNAIL_TIMEOUT, headers=REQUEST_HEADERS)
        if resp.status_code != 200:
            print(f"  [!] {name}: HTTP {resp.status_code} on homepage -- skipping this run")
            return []
        html = resp.text
    except requests.RequestException as e:
        print(f"  [!] {name}: {type(e).__name__}: {e} -- skipping this run")
        return []

    entries = VLADTV_ENTRY_RE.findall(html)
    if not entries:
        print(f"  [!] {name}: 0 entries found on homepage (markup may have changed) -- skipping this run")
        return []

    articles = []
    skipped = 0
    for _entry_id, href in entries[:MAX_PER_SOURCE]:
        link = "https://www.vladtv.com" + href
        if link in known_links:
            skipped += 1
            continue

        details = fetch_vladtv_article(link)
        if not details:
            print(f"  [!] {name}: could not fetch/parse {link} -- skipping")
            continue

        title = soften_title(details["title"])
        excerpt = details["excerpt"]
        summary = generate_summary(title, excerpt, excerpt)

        dedupe_key = republish_dedupe_key(name, title, details["date"])
        if dedupe_key in known_dedupe_keys:
            skipped += 1
            continue

        articles.append(
            {
                "source": name,
                "title": title,
                "slug": slugify(title),
                "excerpt": excerpt,
                "summary": summary,
                "category": "videos",
                "link": link,
                "thumbnail": details["thumbnail"],
                "date": details["date"],
            }
        )
        known_links.add(link)
        known_dedupe_keys.add(dedupe_key)

    print(f"  [+] {name}: {len(articles)} new, {skipped} already archived")
    return articles


def main():
    print("Pulling feeds for UNO Entertainment...")

    try:
        with open("articles.json") as f:
            existing = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        existing = []

    # One-time (but safe to run every time -- it's idempotent) cleanup pass
    # over the archive: normalize_copy() didn't exist when a chunk of these
    # were first fetched, so their title/excerpt/summary can still carry the
    # spaced-ellipsis and space-before-punctuation artifacts described at
    # ELLIPSIS_SPACE_RE/PUNCT_SPACE_RE above. Fixing that here (rather than
    # hand-editing articles.json) means the retention policy above still
    # holds -- an article's fields are never re-derived from its source --
    # while still cleaning up copy that's already archived.
    normalized_count = 0
    for article in existing:
        for field in ("title", "excerpt", "summary"):
            original = article.get(field)
            if original:
                cleaned = normalize_copy(original)
                if cleaned != original:
                    article[field] = cleaned
                    normalized_count += 1
    if normalized_count:
        print(f"[cleanup] normalized copy on {normalized_count} existing field(s)")

    # Brand-safety pass: soften any known asterisk-censored profanity
    # already sitting in the archive's titles (see soften_title /
    # PROFANITY_SOFTEN_PATTERNS above). Same idempotent-cleanup shape as
    # the normalize_copy pass just above -- titles aren't re-derived from
    # the source, just cleaned up in place. Only the display title is
    # touched, never article["slug"], so existing article URLs don't move.
    softened_count = 0
    for article in existing:
        original_title = article.get("title")
        if original_title:
            cleaned_title = soften_title(original_title)
            if cleaned_title != original_title:
                article["title"] = cleaned_title
                softened_count += 1
    if softened_count:
        print(f"[cleanup] softened profanity in {softened_count} existing title(s)")

    # Repair pass: on some sources the RSS/og:title metadata is itself a URL
    # slug rather than real prose (see looks_like_slug_title) -- when that
    # happens fetch_source()'s empty-title check never fires (the field
    # technically isn't empty), so a slug ends up archived as the headline.
    # Only the display title is touched here -- article["slug"] (and so the
    # article's URL) is left exactly as first generated, since that URL may
    # already be shared/indexed and must keep working.
    repaired_count = 0
    for article in existing:
        if looks_like_slug_title(article.get("title", "")):
            real_title, real_excerpt = fetch_real_title_and_excerpt(article.get("link"))
            if real_title and not looks_like_slug_title(real_title):
                real_title = soften_title(real_title)
                print(f"  [repair] {article.get('slug')}: {article['title']!r} -> {real_title!r}")
                article["title"] = real_title
                if real_excerpt and not article.get("excerpt"):
                    article["excerpt"] = real_excerpt[:EXCERPT_LENGTH].rstrip()
                repaired_count += 1
    if repaired_count:
        print(f"[cleanup] repaired {repaired_count} slug-shaped title(s)")

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
    known_dedupe_keys = {
        republish_dedupe_key(a.get("source", ""), a["title"], a.get("date", ""))
        for a in existing
        if a.get("title")
    }
    starting_count = len(existing)

    new_articles = []
    for name, url in SOURCES:
        new_articles.extend(fetch_source(name, url, known_links, known_dedupe_keys))
    new_articles.extend(fetch_vladtv(known_links, known_dedupe_keys))

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
