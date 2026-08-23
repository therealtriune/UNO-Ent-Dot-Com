#!/usr/bin/env python3
"""
UNO Entertainment — static site builder.

Reads articles.json (produced by fetch_feeds.py) and renders:
  - index.html                       homepage, page 1 (newest ARTICLES_PER_PAGE stories)
  - page/2/index.html, page/3/...    older stories on the homepage, paginated
  - category/<cat>/index.html        page 1 of a single category (news, rumors,
                                      videos, music, opinion, sports), same pagination
  - category/<cat>/2/index.html ...  older stories within that category
  - articles/<slug>/index.html       one page per story, with UNO Ent's own
                                      summary, then a clear link out to the source

Every generated page lives at its own directory + index.html (the same
pattern the homepage has always used at the root) so the .html extension
never shows up in the address bar or in anything someone shares -- just
/category/sports/ or /articles/some-story/, not .../sports.html. Every
internal link this file generates uses that clean, trailing-slash, absolute
path (e.g. href="/articles/{slug}/"); only asset references (style.css, the
logo, favicons) use the relative `prefix` arg, since those don't need a
clean URL and are already correct at any folder depth.

Categories are a curation layer, not a publisher directory: "exclusives" and
"features" (tags some source RSS feeds use) don't get their own filter — that
kind of label is specific to how XXL or HotNewHipHop organize their own site,
not something a reader browsing UNO Ent needs. Those stories still show up
in the main feed and in whichever of the six real categories fits them.
"Sports" (basketball, football, boxing, UFC via Yahoo Sports' RSS feeds) is UNO
Ent's one crossover section outside the hip-hop/culture beat.

Homepage cards link to the internal article page first — NOT straight out to
the source. The article page is where the outbound link lives. That's
deliberate: the summary field is written in UNO Ent's own voice today, and
is exactly what gets swapped for original in-house reporting later without
changing any URLs or site structure.

Run this after fetch_feeds.py any time you want to refresh the site.
"""

import json
import re
from datetime import datetime, timezone
from html import escape


def jsonld_script(data: dict) -> str:
    """Serialize a dict as a <script type="application/ld+json"> tag, safely.

    json.dumps() only performs JSON escaping, not HTML escaping. Embedding
    untrusted content (e.g. RSS-derived article titles/excerpts) via a raw
    json.dumps() inside an HTML <script> tag allows a "</script>" sequence
    in the data to close the script early and inject arbitrary markup/script
    (stored XSS). Escaping '<', '>', and '&' as unicode escapes neutralizes
    this while remaining valid, semantically-identical JSON.
    """
    payload = json.dumps(data)
    payload = (
        payload.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    return f'<script type="application/ld+json">{payload}</script>'

with open("articles.json") as f:
    ARTICLES = json.load(f)


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:70]


def time_ago(date_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = datetime.now(timezone.utc) - dt
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "just now"
    if hours < 24:
        h = int(hours)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    days = int(hours / 24)
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''} ago"
    weeks = int(days / 7)
    return f"{weeks} week{'s' if weeks != 1 else ''} ago"


# Assign a stable, unique slug to every article. fetch_feeds.py already sets
# one for each article it pulls — this only fills the gap (and de-dupes)
# when a slug is missing or collides.
seen_slugs = {}
for a in ARTICLES:
    base = a.get("slug") or slugify(a["title"])
    n = seen_slugs.get(base, 0)
    seen_slugs[base] = n + 1
    a["slug"] = base if n == 0 else f"{base}-{n + 1}"

# Newest first, then paginate.
ARTICLES.sort(key=lambda a: a["date"], reverse=True)

ARTICLES_PER_PAGE = 12

# The only categories that get a filter pill in the header. Anything else a
# source tags an article with (e.g. "Exclusive", "Feature") is publisher-
# specific and isn't part of UNO Ent's own taxonomy — those stories still
# appear under "All" and get bucketed into whichever of these fits best.
CATEGORIES = [
    ("news", "News"),
    ("rumors", "Rumors"),
    ("videos", "Videos"),
    ("music", "Music"),
    ("sports", "Sports"),
    ("opinion", "Opinion"),
]
CATEGORY_LABELS = dict(CATEGORIES)

# One genuinely distinct meta description per category, instead of a single
# templated sentence with the category name swapped in — search engines can
# treat near-identical descriptions across pages as low-value/duplicate, so
# each of these is written to stand on its own.
CATEGORY_DESCRIPTIONS = {
    "news": "Breaking hip-hop news, curated daily — arrests, releases, beefs, "
            "business moves, and everything else moving the culture right now.",
    "rumors": "The hip-hop rumor mill, sorted and summarized — dating "
              "speculation, industry whispers, and stories still developing.",
    "videos": "New hip-hop music videos and visuals, rounded up as they drop "
              "from your favorite artists.",
    "music": "New singles, albums, and music news from hip-hop's biggest "
             "names and its most interesting up-and-comers.",
    "sports": "Basketball, football, boxing, and UFC news with a hip-hop "
              "culture lens — where sports and the culture collide.",
    "opinion": "Takes, analysis, and commentary on hip-hop culture from the "
               "UNO Entertainment desk — not just what happened, but what it means.",
}

# ---------------------------------------------------------------------------
# Topic hubs — persistent pages for the names that show up again and again
# across the archive (an artist, or a running storyline like a trial). These
# aren't a 7th filter pill in the header alongside News/Rumors/etc. — they're
# a second, orthogonal way of slicing the same archive, so a reader (or a
# search engine) landing on "Drake" can see everything UNO Ent has on him
# regardless of which category any individual story got filed under.
#
# Matching is done by regex against each article's own title+excerpt text at
# build time — there's no separate "artists" field to maintain by hand, and
# because it's re-derived on every build, a topic hub is never stale: it
# picks up new matching stories automatically as fetch_feeds.py adds them.
# \b word-boundary patterns are required (not bare substrings) — an early,
# looser version of this that matched "BIA" and "Ye" as plain substrings
# produced false positives inside unrelated words ("Arabian", "yesterday").
#
# This list was seeded by scanning the live archive for the top recurring
# names and keeping anything with at least 4 matching articles today — worth
# revisiting periodically as the archive grows (some names currently just
# under that bar, like Nas, will clear it soon).
TOPICS = [
    ("drake", "Drake", [r"\bDrake\b"]),
    ("tupac-shakur", "Tupac Shakur", [r"\bTupac\b", r"\b2Pac\b"]),
    ("diddy", "Diddy", [r"\bDiddy\b", r"\bSean Combs\b"]),
    ("cardi-b", "Cardi B", [r"\bCardi B\b"]),
    ("keefe-d", "Keefe D", [r"\bKeefe D\b"]),
    ("blueface", "Blueface", [r"\bBlueface\b"]),
    ("asap-rocky", "A$AP Rocky", [r"\bA\$AP Rocky\b", r"\bAsap Rocky\b"]),
    ("50-cent", "50 Cent", [r"\b50 Cent\b"]),
    ("chris-brown", "Chris Brown", [r"\bChris Brown\b"]),
    ("usher", "Usher", [r"\bUsher\b"]),
    ("nba-youngboy", "NBA YoungBoy", [r"\bNBA YoungBoy\b", r"\bYoungBoy\b"]),
    ("jay-z", "Jay-Z", [r"\bJay-Z\b", r"\bJay Z\b"]),
    ("kendrick-lamar", "Kendrick Lamar", [r"\bKendrick Lamar\b", r"\bKendrick\b"]),
    ("nicki-minaj", "Nicki Minaj", [r"\bNicki Minaj\b", r"\bNicki\b"]),
    ("tyga", "Tyga", [r"\bTyga\b"]),
    ("megan-thee-stallion", "Megan Thee Stallion", [r"\bMegan Thee Stallion\b"]),
    ("kanye-west", "Kanye West", [r"\bKanye West\b", r"\bKanye\b", r"\bYe\b"]),
    ("beyonce", "Beyoncé", [r"\bBeyonc"]),
    ("kim-kardashian", "Kim Kardashian", [r"\bKim Kardashian\b"]),
    ("rick-ross", "Rick Ross", [r"\bRick Ross\b"]),
    ("latto", "Latto", [r"\bLatto\b"]),
    ("lil-durk", "Lil Durk", [r"\bLil Durk\b"]),
    ("21-savage", "21 Savage", [r"\b21 Savage\b"]),
    ("eminem", "Eminem", [r"\bEminem\b"]),
    ("doja-cat", "Doja Cat", [r"\bDoja Cat\b"]),
    ("lil-uzi-vert", "Lil Uzi Vert", [r"\bLil Uzi Vert\b"]),
    ("boosie", "Boosie", [r"\bBoosie\b"]),
    ("the-game", "The Game", [r"\bThe Game\b"]),
    ("young-thug", "Young Thug", [r"\bYoung Thug\b"]),
]
TOPIC_LABELS = {slug: name for slug, name, _ in TOPICS}
# Deliberately case-sensitive (no re.IGNORECASE): names in feed titles/
# excerpts are consistently capitalized, and case-sensitivity is what keeps
# a short pattern like "\bYe\b" from matching the common lowercase word
# "ye" (dialect/slang for "you") instead of just Kanye West's stage name.
TOPIC_PATTERNS = {
    slug: re.compile("|".join(patterns)) for slug, _, patterns in TOPICS
}


def topic_articles(slug: str) -> list:
    pattern = TOPIC_PATTERNS[slug]
    return [
        a for a in ARTICLES
        if pattern.search(a.get("title", "") + " " + a.get("excerpt", ""))
    ]


def article_topic_slugs(a: dict) -> list:
    """Every topic slug this article matches, in TOPICS order — used to link
    an article page to its topic hub(s) (see build_article)."""
    blob = a.get("title", "") + " " + a.get("excerpt", "")
    return [slug for slug, pattern in TOPIC_PATTERNS.items() if pattern.search(blob)]


ARTICLE_COUNT = len(ARTICLES)

# ---------------------------------------------------------------------------
# Thumbnail QA — catches the "logo instead of a photo" problem automatically
# instead of relying on someone noticing it on the live site. Runs on every
# build (including the scheduled GitHub Action) and writes any problems to
# thumbnail_flags.json at the repo root, plus a warning line to the build
# log. This never blocks the build — a flagged article still gets built and
# published, it just gets logged so it can be fixed by hand later.
#
# Three things get flagged:
#   1. No thumbnail at all.
#   2. A thumbnail URL that matches a known site-logo/placeholder pattern —
#      see LOGO_URL_PATTERNS in fetch_feeds.py; kept here as a duplicate,
#      lightweight copy so build_site.py doesn't need to import that module.
#   3. A thumbnail reused in a way that breaks the "max 2 uses, and never on
#      the same rendered page" rule — computed against final sort order and
#      pagination, the same way it was checked by hand for the 74-article
#      cleanup pass.
_LOGO_URL_PATTERNS = [
    "logo-for-white-backgrounds", "logo-for-dark-backgrounds", "/og.jpg",
    "/og-image", "site-logo", "sharing-default", "default-social",
    "hu-logo", "shaderoom-logo", "-logo.png", "-logo.jpg", "/logo.png",
    "/logo.jpg", "placeholder",
]


def _looks_like_logo(url):
    if not url:
        return False
    lowered = url.lower()
    return any(p in lowered for p in _LOGO_URL_PATTERNS)


def check_thumbnails(articles):
    flags = []
    for i, a in enumerate(articles):
        thumb = a.get("thumbnail")
        if not thumb:
            flags.append({"link": a["link"], "issue": "missing_thumbnail"})
        elif _looks_like_logo(thumb):
            flags.append({"link": a["link"], "issue": "logo_thumbnail", "thumbnail": thumb})

    by_thumb = {}
    for i, a in enumerate(articles):
        thumb = a.get("thumbnail")
        if thumb:
            by_thumb.setdefault(thumb, []).append((i, a))

    cat_index = {}
    for cat_key, _ in CATEGORIES:
        cat_articles = [a for a in articles if a.get("category") == cat_key]
        for j, a in enumerate(cat_articles):
            cat_index[a["link"]] = (cat_key, j // ARTICLES_PER_PAGE)

    for thumb, uses in by_thumb.items():
        if len(uses) > 2:
            flags.append({
                "issue": "overused_thumbnail",
                "thumbnail": thumb,
                "links": [a["link"] for _, a in uses],
                "use_count": len(uses),
            })
        elif len(uses) == 2:
            (i1, a1), (i2, a2) = uses
            home_page_1, home_page_2 = i1 // ARTICLES_PER_PAGE, i2 // ARTICLES_PER_PAGE
            if home_page_1 == home_page_2:
                flags.append({
                    "issue": "same_homepage_page",
                    "thumbnail": thumb,
                    "links": [a1["link"], a2["link"]],
                })
            cat1, cat2 = cat_index.get(a1["link"]), cat_index.get(a2["link"])
            if cat1 and cat2 and cat1 == cat2:
                flags.append({
                    "issue": "same_category_page",
                    "thumbnail": thumb,
                    "links": [a1["link"], a2["link"]],
                })

    with open("thumbnail_flags.json", "w") as f:
        json.dump(flags, f, indent=2)

    if flags:
        print(f"[!] thumbnail QA: {len(flags)} issue(s) flagged — see thumbnail_flags.json")
    else:
        print("[+] thumbnail QA: no issues found")

    return flags


# Canonical domain, used to build absolute URLs for canonical links and
# og:image / og:url (required by iMessage, Facebook, Twitter/X previews --
# relative URLs don't work for those tags).
SITE_URL = "https://unoent.com"
DEFAULT_OG_IMAGE = f"{SITE_URL}/og-image.png"
SITE_DESCRIPTION = (
    "The latest hip-hop news, rumors, videos, music, and opinion. Where culture gathers."
)

# Google Tag Manager container for unoent.com (GTM-KNZNCCPT). Feeds a GA4
# property via a "Google Tag" configuration tag set up inside GTM itself --
# nothing about the GA4 measurement ID lives in this file, so swapping
# analytics providers later only ever means changing tags inside GTM, never
# touching the site templates again.
GTM_CONTAINER_ID = "GTM-KNZNCCPT"

GTM_HEAD_SNIPPET = f"""<!-- Google Tag Manager -->
<script>(function(w,d,s,l,i){{w[l]=w[l]||[];w[l].push({{'gtm.start':
new Date().getTime(),event:'gtm.js'}});var f=d.getElementsByTagName(s)[0],
j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src=
'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);
}})(window,document,'script','dataLayer','{GTM_CONTAINER_ID}');</script>
<!-- End Google Tag Manager -->"""

GTM_BODY_SNIPPET = f"""<!-- Google Tag Manager (noscript) -->
<noscript><iframe src="https://www.googletagmanager.com/ns.html?id={GTM_CONTAINER_ID}"
height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>
<!-- End Google Tag Manager (noscript) -->"""

# Light/dark theme -- applied as early as possible in <head>, before the
# stylesheet loads, so a visitor who previously chose light mode never sees
# a flash of the default dark theme on load. The default (no saved
# preference) stays dark, matching the site's existing look for everyone
# who hasn't explicitly opted into light mode -- see the theme-toggle-btn
# in header_html() for how the choice gets made and saved. Wrapped in
# try/except since localStorage can throw in some browsers (e.g. private
# mode with storage blocked), and that should never break page load.
THEME_INIT_SNIPPET = """<script>
(function() {
  try {
    if (localStorage.getItem('uno_theme') === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    }
  } catch (e) {}
})();
</script>"""

# Disqus comments, article pages only. Disqus hosts the whole thread (storage,
# moderation, spam filtering, and the Google/Facebook/Microsoft/Apple/Twitter
# login prompts) -- this site never touches a visitor's login or comment data
# directly. page.url/page.identifier are set per-article below so each story
# gets its own thread even if the title or URL slug is ever reused.
#
# Click-to-load rather than auto-embedded: with zero engagement so far on
# every article, an immediately-visible empty comment box (login prompt,
# empty textarea, "Be the first to comment") reads as "nobody reads this
# site" to a visitor -- and to a prospective advertiser or sponsor doing
# due diligence before a deal. This shows a plain "Show Comments" button
# instead and only mounts the real Disqus thread when someone actually
# clicks it, so the empty state is never the default view. Once real
# comment activity builds up, this can be swapped back to always-on embed.
DISQUS_SHORTNAME = "unoent"


def disqus_html(page_url: str, page_identifier: str) -> str:
    return f"""
<div class="comments-wrap">
  <h2 class="comments-heading">Comments</h2>
  <div id="disqus-loader">
    <button type="button" class="comments-load-btn" id="disqus-load-btn">Show Comments</button>
    <p class="comments-load-note">Comments are hosted by Disqus and only load once you click above.</p>
  </div>
  <div id="disqus_thread" style="display:none;"></div>
  <script>
    (function() {{
      var btn = document.getElementById('disqus-load-btn');
      var loader = document.getElementById('disqus-loader');
      var thread = document.getElementById('disqus_thread');
      if (!btn) return;
      btn.addEventListener('click', function() {{
        thread.style.display = '';
        loader.style.display = 'none';
        window.disqus_config = function () {{
          this.page.url = "{page_url}";
          this.page.identifier = "{page_identifier}";
        }};
        var d = document, s = d.createElement('script');
        s.src = 'https://{DISQUS_SHORTNAME}.disqus.com/embed.js';
        s.setAttribute('data-timestamp', +new Date());
        (d.head || d.body).appendChild(s);
      }}, {{ once: true }});
    }})();
  </script>
  <noscript>Please enable JavaScript to view the <a href="https://disqus.com/?ref_noscript">comments powered by Disqus.</a></noscript>
</div>"""

# ---------------------------------------------------------------------------
# Shared stylesheet
# ---------------------------------------------------------------------------

STYLE_CSS = """
:root {
  --bg: #0a0a0a;
  --bg-card: #141414;
  --bg-card-hover: #1c1c1c;
  --red: #e0202e;
  --text: #f5f5f5;
  --text-secondary: #e6e6e6;
  --gray: #9a9a9a;
  --border: #262626;
  --shimmer-1: #161616;
  --shimmer-2: #232323;
  --search-cancel-filter: invert(1);
}
/* Light mode -- opt-in only (see THEME_INIT_SNIPPET/theme-toggle-btn below),
   default experience stays exactly the dark theme above for anyone who
   hasn't explicitly switched. Everything, including the header and footer,
   follows the toggle via these variables. The UNO-logo thumbnail/hero
   fallback placeholders are the one exception -- uno-logo.png is a
   near-white mark designed to sit on a dark surface, so those content-area
   placeholder badges stay on their original dark literal colors in both
   themes rather than going invisible. The header and footer logos are
   handled separately: they swap between uno-logo.png (dark theme) and
   uno-logo-dark.png (light theme) via the .logo-dark-mode/.logo-light-mode
   toggle below, so they don't need this exception.
*/
[data-theme="light"] {
  --bg: #f7f7f5;
  --bg-card: #ffffff;
  --bg-card-hover: #f0f0ee;
  --red: #e0202e;
  --text: #161616;
  --text-secondary: #2c2c2c;
  --gray: #6b6b6b;
  --border: #e0e0dd;
  --shimmer-1: #e9e9e6;
  --shimmer-2: #f6f6f4;
  --search-cancel-filter: none;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: 'Helvetica Neue', Arial, sans-serif;
}
a { color: inherit; }
header {
  background: var(--bg-card);
  border-bottom: 4px solid var(--red);
  padding: 22px 5vw;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  flex-wrap: wrap;
  gap: 16px;
  position: relative;
}
header a.brand-link { display: flex; align-items: center; gap: 16px; text-decoration: none; }
header img.logo { height: 52px; width: auto; display: block; }
/* Two logo variants -- uno-logo.png is a near-white mark for the dark
   theme, uno-logo-dark.png is a near-black recolor of the exact same
   artwork for the light theme (same file, same shapes, just the letter
   fill swapped -- see the comment on uno-logo-dark.png's generation).
   Only one is ever visible at a time, toggled the same way as the
   sun/moon icons below. */
.logo-light-mode { display: none; }
[data-theme="light"] .logo-dark-mode { display: none; }
[data-theme="light"] .logo-light-mode { display: block; }
/* header img.logo sets display:block with higher specificity than the
   generic rules above, so the header's own logo pair needs an explicit
   override at matching specificity (the footer logos have no competing
   display rule, so the generic rules above are enough for them). */
header img.logo.logo-light-mode { display: none; }
[data-theme="light"] header img.logo.logo-dark-mode { display: none; }
[data-theme="light"] header img.logo.logo-light-mode { display: block; }
header .tagline {
  color: var(--gray);
  font-size: 12px;
  letter-spacing: 2.5px;
  text-transform: uppercase;
  border-left: 2px solid var(--red);
  padding-left: 16px;
}

.header-filters { display: flex; gap: 8px; flex-wrap: wrap; margin-left: auto; }
.header-filters a {
  font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--gray); text-decoration: none; padding: 7px 16px; border-radius: 999px;
  border: 1px solid var(--border);
}
.header-filters a:hover { color: var(--text); border-color: var(--gray); }
.header-filters a.active { color: #fff; background: var(--red); border-color: var(--red); }

/* Mobile hamburger menu -- hidden checkbox drives a CSS-only dropdown, no JS. */
.nav-toggle-checkbox { display: none; }
.nav-toggle-btn {
  display: none;
  flex-direction: column;
  justify-content: center;
  gap: 5px;
  width: 30px;
  height: 24px;
  cursor: pointer;
  flex-shrink: 0;
}
.nav-toggle-btn span {
  display: block;
  height: 2px;
  width: 100%;
  background: var(--text);
  border-radius: 2px;
  transition: transform 0.15s ease, opacity 0.15s ease;
}

/* Standalone mobile-header-row copy of the toggle -- hidden above the
   mobile breakpoint, where the nav-pill copy inside .header-filters
   (below) is used instead. See the mobile media query for its visible
   styling; this just keeps it out of the desktop layout. */
.theme-toggle-btn-mobile { display: none; }

/* Light/dark toggle -- last pill inside .header-filters itself (right
   after Opinion), styled to match those pills exactly so it reads as
   part of the nav bar instead of a separate floating control. On mobile
   this copy is hidden (see media query) in favor of the standalone
   .theme-toggle-btn-mobile button that sits in the visible header row,
   so the control doesn't require opening the hamburger menu to reach. */
.header-filters .theme-toggle-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 7px 16px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--gray);
  cursor: pointer;
  line-height: 1;
}
.header-filters .theme-toggle-btn:hover { color: var(--text); border-color: var(--gray); }
.theme-icon-moon { display: none; }
[data-theme="light"] .theme-icon-sun { display: none; }
[data-theme="light"] .theme-icon-moon { display: block; }

@media (max-width: 760px) {
  header .tagline { display: none; }
  header { gap: 10px; }
  /* Visible header row on mobile, left to right: logo, search, theme
     toggle, hamburger. The theme toggle lives here (not just inside the
     hamburger's dropdown) so switching themes doesn't require opening the
     menu first. */
  .theme-toggle-btn-mobile {
    display: inline-flex;
    order: 3;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 50%;
    color: var(--gray);
    cursor: pointer;
    flex-shrink: 0;
  }
  .nav-toggle-btn { display: flex; order: 4; }
  .header-filters {
    display: none;
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    flex-direction: column;
    align-items: stretch;
    background: var(--bg-card);
    border-bottom: 4px solid var(--red);
    padding: 10px 5vw 20px;
    gap: 6px;
    z-index: 30;
  }
  .header-filters a { padding: 13px 16px; border-radius: 6px; text-align: left; }
  /* The dropdown's own toggle copy is redundant now that the standalone
     mobile-row button above is always reachable, so it's dropped here
     rather than shown twice. */
  .header-filters .theme-toggle-btn-desktop { display: none; }
  .nav-toggle-checkbox:checked ~ .header-filters { display: flex; }
  .nav-toggle-checkbox:checked ~ .nav-toggle-btn span:nth-child(1) { transform: translateY(7px) rotate(45deg); }
  .nav-toggle-checkbox:checked ~ .nav-toggle-btn span:nth-child(2) { opacity: 0; }
  .nav-toggle-checkbox:checked ~ .nav-toggle-btn span:nth-child(3) { transform: translateY(-7px) rotate(-45deg); }
}

main { padding: 32px 5vw 80px; }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 24px;
}
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  text-decoration: none;
  color: inherit;
  transition: transform 0.15s ease, background 0.15s ease, border-color 0.15s ease;
}
.card:hover { transform: translateY(-3px); background: var(--bg-card-hover); border-color: var(--red); }
.card-thumb {
  width: 100%; height: 180px; object-fit: cover; display: block;
  background: linear-gradient(100deg, var(--shimmer-1) 30%, var(--shimmer-2) 50%, var(--shimmer-1) 70%);
  background-size: 300% 100%;
  animation: thumb-shimmer 1.4s ease-in-out infinite;
}
.card-thumb-fallback {
  object-fit: contain;
  padding: 34px 44px;
  background: linear-gradient(135deg, #1a1a1a, #0d0d0d);
  animation: none;
}
@keyframes thumb-shimmer {
  0% { background-position: 100% 0; }
  100% { background-position: -100% 0; }
}
.card-body { padding: 18px 20px 22px; display: flex; flex-direction: column; flex: 1; }
.card-meta { font-size: 11px; color: var(--gray); margin-bottom: 10px; letter-spacing: 0.3px; }
.card-category { color: var(--red); font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; }
.card-dot { margin: 0 6px; }
.card-title { font-size: 18px; line-height: 1.3; margin: 0 0 10px; font-weight: 800; color: var(--text); }
.card-excerpt { font-size: 14px; color: var(--gray); line-height: 1.5; margin: 0 0 16px; flex: 1; }
.card-link { font-size: 13px; font-weight: 700; color: var(--text); text-transform: uppercase;
  letter-spacing: 0.5px; border-bottom: 2px solid var(--red); padding-bottom: 3px; align-self: flex-start; }
footer { padding: 56px 5vw 0; border-top: 1px solid var(--border); color: var(--gray); font-size: 13px; line-height: 1.7; background: var(--bg-card); }
footer strong { color: var(--text); }
footer a { color: var(--gray); text-decoration: none; }
footer a:hover { color: var(--text); }
.footer-inner { max-width: 1200px; margin: 0 auto; }
.footer-top { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 10px; padding-bottom: 40px; }
.footer-logo { height: 40px; width: auto; }
.footer-tagline { margin: 0; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px; color: var(--red); }
.footer-columns {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 32px;
  padding-bottom: 40px; border-top: 1px solid var(--border); padding-top: 40px;
}
.footer-col h4 {
  margin: 0 0 16px; font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.8px; color: var(--text);
}
.footer-col a { display: block; margin-bottom: 10px; font-size: 13px; }
.footer-col p { margin: 0 0 10px; font-size: 13px; color: var(--gray); }
.footer-col .footer-contact-email { color: var(--red); font-weight: 700; }
.footer-col .footer-contact-email:hover { text-decoration: underline; }
.footer-bottom { border-top: 1px solid var(--border); padding: 24px 0 28px; }
.footer-source-note { margin: 0 0 16px; font-size: 12px; color: var(--gray); max-width: 720px; }
.footer-copy {
  margin: 0; font-size: 11px; color: var(--gray); display: flex; justify-content: space-between;
  flex-wrap: wrap; gap: 8px;
}
@media (max-width: 640px) {
  .footer-columns { grid-template-columns: 1fr; gap: 28px; text-align: center; }
  .footer-copy { justify-content: center; text-align: center; }
}

.pagination { display: flex; align-items: center; justify-content: center; gap: 20px; margin-top: 48px; }
.pagination a, .pagination span {
  font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
  padding: 10px 18px; border-radius: 6px; text-decoration: none;
}
.pagination a { color: var(--text); border: 1px solid var(--border); }
.pagination a:hover { border-color: var(--red); color: var(--red); }
.pagination .disabled { color: #4a4a4a; border: 1px solid var(--border); opacity: 0.5; }
.pagination .page-count { color: var(--gray); border: none; text-transform: none; letter-spacing: 0; font-weight: 400; }
.page-jump {
  display: flex; align-items: center; gap: 8px;
  color: var(--gray); font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
}
.page-jump select {
  background: var(--bg-card); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 9px 14px; font-size: 13px; font-weight: 700;
  font-family: inherit; cursor: pointer;
}
.page-jump select:hover { border-color: var(--red); }
.page-jump select:focus { outline: none; border-color: var(--red); }

@media (max-width: 600px) {
  .pagination { flex-wrap: wrap; row-gap: 14px; column-gap: 10px; }
  .pagination a, .pagination .page-count { font-size: 12px; padding: 10px 14px; }
  .page-jump {
    order: 99;
    flex-basis: 100%;
    justify-content: center;
  }
  .page-jump select {
    flex: 0 1 240px;
    padding: 13px 16px;
    font-size: 15px;
  }
}

/* Search bar -- shared component in the header and next to the page-jump
   dropdown in pagination. Reused as-is (no per-placement variants beyond
   width/order) so it looks identical wherever it appears. */
.search-bar {
  display: flex;
  align-items: center;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 3px 3px 3px 16px;
  flex: 0 1 220px;
  min-width: 0;
  position: relative;
}
.search-bar:focus-within { border-color: var(--red); }
.search-bar input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: none;
  outline: none;
  color: var(--text);
  font-family: inherit;
  font-size: 13px;
  padding: 8px 0;
}
.search-bar input::placeholder { color: var(--gray); }
.search-bar input::-webkit-search-cancel-button { filter: var(--search-cancel-filter); opacity: 0.6; }
.search-bar button {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  border: none;
  cursor: pointer;
  background: var(--red);
  color: #fff;
  flex-shrink: 0;
}
.search-bar button:hover { background: #c81a27; }
                          
.header-search { order: 2; }
@media (max-width: 760px) {
  /* Collapse to an icon-only button on mobile -- there isn't room for logo +
     full text input + hamburger on one row, so the input is visually hidden
     and tapping the circular red button submits the (empty) form to
     /search/, where the visitor types their query on a dedicated page. */
  .header-search {
    order: 2;
    flex: 0 0 auto;
    width: 40px;
    height: 40px;
    max-width: none;
    min-width: 0;
    margin: 0 0 0 auto;
    padding: 0;
    justify-content: center;
    border-radius: 50%;
    overflow: hidden;
  }
  .header-search .search-input { display: none; }
  .header-search button {
    width: 100%;
    height: 100%;
    border-radius: 50%;
  }
  /* Tapping the icon first expands the pill (via SEARCH_SUGGEST_JS below)
     instead of submitting an empty query -- this is what actually shows
     the input so a visitor can type. */
  .header-search.expanded {
    width: auto;
    max-width: none;
    flex: 1 1 auto;
    border-radius: 999px;
    padding: 3px 3px 3px 14px;
    overflow: visible;
  }
  .header-search.expanded .search-input { display: block; }
  .header-search.expanded button { width: 30px; height: 30px; }
}

.pagination-search { flex-basis: 220px; }
@media (max-width: 600px) {
  .pagination-search { order: 99; flex-basis: 100%; max-width: 320px; margin: 0 auto; }
}

/* Live search suggestions dropdown -- populated by SEARCH_SUGGEST_JS as the
   visitor types. Anchored to the .search-bar it belongs to (position:
   relative set above), so header-search and pagination-search each get
   their own independent panel. */
.search-suggest {
  position: absolute;
  top: calc(100% + 8px);
  left: 0;
  right: 0;
  min-width: 260px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
  z-index: 50;
  box-shadow: 0 12px 28px rgba(0, 0, 0, 0.45);
}
.search-suggest-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  text-decoration: none;
  color: var(--text);
  border-bottom: 1px solid var(--border);
}
.search-suggest-item:last-of-type { border-bottom: none; }
.search-suggest-item:hover, .search-suggest-item.active { background: var(--bg-card-hover); }
.search-suggest-item img {
  width: 40px; height: 40px; object-fit: cover; border-radius: 6px;
  flex-shrink: 0; background: #1a1a1a;
}
.search-suggest-item img.search-suggest-fallback { object-fit: contain; padding: 6px; }
.search-suggest-text { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.search-suggest-title {
  font-size: 13px; font-weight: 600; line-height: 1.3;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.search-suggest-cat { font-size: 10px; text-transform: uppercase; letter-spacing: 0.4px; color: var(--gray); }
.search-suggest-empty { padding: 14px 12px; font-size: 13px; color: var(--gray); }
.search-suggest-viewall {
  display: block; padding: 10px 12px; font-size: 12px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.4px; color: var(--red);
  text-decoration: none; text-align: center; background: rgba(224, 32, 46, 0.06);
}
.search-suggest-viewall:hover { background: rgba(224, 32, 46, 0.14); }

/* Search results page */
.search-status { color: var(--gray); font-size: 14px; margin: 0 0 24px; }
.search-load-more {
  display: block;
  margin: 40px auto 0;
  padding: 12px 28px;
  border-radius: 999px;
  background: var(--bg-card);
  color: var(--text);
  border: 1px solid var(--border);
  font-size: 13px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  cursor: pointer;
}
.search-load-more:hover { border-color: var(--red); color: var(--red); }

/* Article page */
.article-wrap { max-width: 720px; margin: 0 auto; padding: 48px 5vw 80px; }
.back-link { display: inline-block; font-size: 13px; color: var(--gray); text-decoration: none; margin-bottom: 24px; }
.back-link:hover { color: var(--text); }
.article-meta { font-size: 12px; color: var(--gray); margin-bottom: 14px; letter-spacing: 0.3px; }
.article-title { font-size: 32px; line-height: 1.25; font-weight: 800; margin: 0 0 24px; color: var(--text); }
.article-hero {
  width: 100%; max-height: 420px; object-fit: cover; border-radius: 10px;
  margin-bottom: 28px; border: 1px solid var(--border);
  background: linear-gradient(100deg, var(--shimmer-1) 30%, var(--shimmer-2) 50%, var(--shimmer-1) 70%);
  background-size: 300% 100%;
  animation: thumb-shimmer 1.4s ease-in-out infinite;
}
.article-hero-fallback {
  object-fit: contain;
  padding: 48px 60px;
  background: linear-gradient(135deg, #1a1a1a, #0d0d0d);
  animation: none;
}
.article-summary { font-size: 17px; line-height: 1.7; color: var(--text-secondary); margin-bottom: 32px; }
/* Hand-authored article bodies (UNO Ent originals, not RSS summaries) --
   set via an article's optional "body_html" field in articles.json, which
   fully replaces the single-paragraph .article-summary block below with
   real multi-paragraph HTML the article was written with directly. Reuses
   the same typography as .article-summary so it reads identically. */
.article-body { font-size: 17px; line-height: 1.7; color: var(--text-secondary); margin-bottom: 8px; }
.article-body p { margin: 0 0 18px; }
.article-body h2 { font-size: 21px; font-weight: 800; color: var(--text); margin: 32px 0 14px; }
.article-body a { color: var(--red); font-weight: 700; text-decoration: none; }
.article-body a:hover { text-decoration: underline; }
.article-body .outbound-cta { margin: 8px 0 24px; }
/* Responsive 16:9 video embed for hand-authored posts that include a
   YouTube (or similar) player inline -- e.g. a music release announcement
   embedding the official video. */
.article-video-embed {
  position: relative; width: 100%; aspect-ratio: 16 / 9; margin: 4px 0 28px;
  border-radius: 10px; overflow: hidden; border: 1px solid var(--border); background: #000;
}
.article-video-embed iframe { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }
.outbound-cta {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--red); color: #fff; text-decoration: none;
  font-weight: 700; font-size: 15px; padding: 14px 26px; border-radius: 6px;
  letter-spacing: 0.3px;
}
.outbound-cta:hover { background: #c81a27; }
.outbound-note { font-size: 12px; color: var(--gray); margin-top: 14px; }
.article-related-topics { font-size: 13px; color: var(--gray); margin-top: 24px; }
.article-related-topics a { color: var(--red); font-weight: 700; text-decoration: none; }
.article-related-topics a:hover { text-decoration: underline; }

/* Premium "feature" layout -- opt-in via an article's "layout": "feature"
   field, used for hand-authored UNO Ent originals that should read like a
   magazine feature rather than a wire-summary post (see .article-body
   above for the plain version). Structurally separate from .article-wrap
   so a feature's full-bleed hero doesn't affect any of the other 3500+
   standard article pages. Design language: brand red stays the primary
   accent (CTAs, drop cap), a restrained gold is layered in as the second
   "premium" accent for dividers/labels only -- two accent colors, used
   sparingly, is what keeps this feeling classy instead of loud. */
:root { --gold: #c9a24b; }
.feature-hero {
  position: relative; width: 100%; max-height: 720px; overflow: hidden;
  background: #000;
}
.feature-hero-img { width: 100%; height: 100%; max-height: 720px; object-fit: cover; display: block; opacity: 0.92; }
.feature-hero-overlay {
  position: absolute; inset: 0;
  background: linear-gradient(180deg, rgba(0,0,0,0.55) 0%, rgba(0,0,0,0.05) 22%, rgba(0,0,0,0.15) 55%, rgba(0,0,0,0.92) 100%);
  display: flex; align-items: flex-end;
}
.feature-topbar { position: absolute; top: 0; left: 0; right: 0; z-index: 5; padding: 20px 5vw 0; }
.feature-topbar .back-link {
  display: inline-flex; align-items: center; gap: 6px; color: #fff;
  background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.25);
  padding: 8px 16px; border-radius: 999px; font-size: 12px; font-weight: 700;
  letter-spacing: 0.3px; margin: 0; backdrop-filter: blur(4px);
}
.feature-topbar .back-link:hover { background: rgba(0,0,0,0.6); color: #fff; }
.feature-hero-inner { max-width: 820px; margin: 0 auto; padding: 40px 5vw 44px; width: 100%; box-sizing: border-box; }
.feature-kicker {
  display: inline-flex; align-items: center; gap: 8px;
  color: #fff; background: var(--red);
  font-size: 11px; font-weight: 800; letter-spacing: 2px; text-transform: uppercase;
  padding: 8px 16px; margin-bottom: 20px; border-bottom: 2px solid var(--gold);
}
.feature-title {
  color: #fff; font-size: 48px; line-height: 1.08; font-weight: 800; margin: 0 0 18px;
  letter-spacing: -0.5px; text-transform: uppercase; text-shadow: 0 4px 20px rgba(0,0,0,0.5);
}
.feature-byline {
  display: flex; align-items: center; gap: 10px; color: #cfcfcf;
  font-size: 12px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
}
.feature-byline strong { color: var(--gold); font-weight: 800; }
.feature-byline .dot { color: var(--gold); }
.feature-wrap { max-width: 720px; margin: 0 auto; padding: 48px 5vw 80px; }
.feature-body { font-size: 18px; line-height: 1.8; color: var(--text-secondary); }
.feature-body p { margin: 0 0 22px; }
.feature-body > p:first-of-type::first-letter {
  float: left; font-size: 76px; line-height: 0.78; font-weight: 800; color: var(--red);
  padding: 6px 10px 0 0; font-family: 'Helvetica Neue', Arial, sans-serif;
}
.feature-body h2 {
  font-size: 13px; font-weight: 800; color: var(--text); margin: 48px 0 20px;
  text-transform: uppercase; letter-spacing: 2px; padding-bottom: 12px;
  border-bottom: 3px solid var(--gold); display: inline-block;
}
.feature-body a { color: var(--red); font-weight: 700; text-decoration: none; }
.feature-body a:hover { text-decoration: underline; }
.feature-body .outbound-cta { margin: 10px 0 28px; border-radius: 2px; text-transform: uppercase; letter-spacing: 1px; color: #fff; }
.feature-pullquote {
  position: relative; margin: 40px 0; padding: 8px 0 8px 40px;
  font-size: 30px; line-height: 1.35; font-weight: 800; color: var(--text);
  letter-spacing: -0.3px;
}
.feature-pullquote::before {
  content: "\201C"; position: absolute; left: -4px; top: -18px;
  font-size: 76px; font-weight: 800; color: var(--gold); line-height: 1; font-family: Georgia, serif;
}
.feature-video-caption {
  display: block; color: var(--red); font-size: 11px; font-weight: 800;
  letter-spacing: 1.6px; text-transform: uppercase; margin: 0 0 12px;
}
.feature-gallery-section { margin: 48px 0; }
.feature-section-label {
  display: block; color: var(--gold); font-size: 11px; font-weight: 800;
  letter-spacing: 2px; text-transform: uppercase; margin: 0 0 16px;
}
.feature-gallery { column-count: 2; column-gap: 12px; }
.feature-gallery img {
  width: 100%; display: block; margin: 0 0 12px; border-radius: 2px;
  border: 1px solid var(--border); transition: transform 0.2s ease, filter 0.2s ease;
  break-inside: avoid;
}
.feature-gallery img:hover { transform: scale(1.015); filter: brightness(1.05); }
@media (max-width: 640px) {
  .feature-title { font-size: 30px; }
  .feature-hero { max-height: 480px; }
  .feature-hero-inner { padding: 30px 6vw 28px; }
  .feature-body { font-size: 17px; }
  .feature-body > p:first-of-type::first-letter { font-size: 56px; }
  .feature-pullquote { font-size: 22px; padding-left: 30px; }
  .feature-pullquote::before { font-size: 56px; top: -14px; }
}
.comments-wrap { margin-top: 48px; padding-top: 32px; border-top: 1px solid var(--border); }
.comments-heading { font-size: 20px; font-weight: 800; margin: 0 0 20px; color: var(--text); }
.comments-load-btn {
  background: var(--red); color: #fff; border: none; border-radius: 6px;
  font-weight: 700; font-size: 14px; padding: 12px 22px; cursor: pointer;
  letter-spacing: 0.3px;
}
.comments-load-btn:hover { background: #c81a27; }
.comments-load-note { font-size: 13px; color: var(--gray); margin-top: 10px; }

/* Cookie consent banner */
.cookie-banner {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 200;
  background: var(--bg-card); border-top: 1px solid var(--border);
  padding: 16px 5vw; box-shadow: 0 -8px 24px rgba(0, 0, 0, 0.4);
}
.cookie-banner[hidden] { display: none; }
.cookie-banner-inner {
  max-width: 1200px; margin: 0 auto; display: flex; align-items: center;
  justify-content: space-between; gap: 20px; flex-wrap: wrap;
}
.cookie-banner-inner p { margin: 0; font-size: 13px; color: var(--gray); line-height: 1.5; flex: 1; min-width: 240px; }
.cookie-banner-inner a { color: var(--red); font-weight: 700; text-decoration: none; }
.cookie-banner-inner a:hover { text-decoration: underline; }
.cookie-banner-btn {
  -webkit-appearance: none; appearance: none;
  background: var(--red) !important; color: #fff !important; border: none; border-radius: 6px;
  font-weight: 700; font-size: 13px; padding: 11px 26px; cursor: pointer;
  text-transform: uppercase; letter-spacing: 0.5px; flex-shrink: 0;
}
.cookie-banner-btn:hover { background: #c81a27; }

/* Legal pages (Privacy Policy, Terms of Service) */
.legal-wrap { max-width: 760px; margin: 0 auto; padding: 48px 5vw 80px; }
.legal-wrap h1 { font-size: 30px; font-weight: 800; margin: 0 0 8px; color: var(--text); }
.legal-wrap .legal-updated { font-size: 12px; color: var(--gray); margin-bottom: 36px; letter-spacing: 0.3px; }
.legal-wrap h2 { font-size: 18px; font-weight: 700; margin: 36px 0 12px; color: var(--text); }
.legal-wrap p { font-size: 15px; line-height: 1.7; color: var(--text-secondary); margin: 0 0 16px; }
.legal-wrap a { color: var(--red); text-decoration: none; font-weight: 700; }
.legal-wrap a:hover { text-decoration: underline; }

/* Topic hub pages (/topic/{slug}/) and the /topics/ directory */
.topic-hub-heading { margin: 0 0 28px; }
.topic-hub-heading h1 { font-size: 30px; font-weight: 800; margin: 0 0 8px; color: var(--text); }
.topic-hub-sub { font-size: 14px; color: var(--gray); margin: 0; }
.topic-index-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 14px; }
.topic-index-item {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px;
  padding: 16px 18px; text-decoration: none;
}
.topic-index-item:hover { background: var(--bg-card-hover); border-color: var(--gray); }
.topic-index-name { font-size: 15px; font-weight: 700; color: var(--text); }
.topic-index-count { font-size: 12px; color: var(--gray); flex-shrink: 0; }

/* Hip-Hop Beef Tracker pillar page (/hip-hop-beef-tracker/) */
.beef-tracker-intro { font-size: 15px; line-height: 1.7; color: var(--text-secondary); max-width: 760px; margin: 0 0 40px; }
.beef-entry {
  background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px;
  padding: 24px 26px; margin-bottom: 20px;
}
.beef-entry-header { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 10px; }
.beef-entry-title { font-size: 19px; font-weight: 800; color: var(--text); margin: 0; }
.beef-entry-status {
  font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
  padding: 4px 12px; border-radius: 999px; flex-shrink: 0;
}
.beef-entry-status.active { background: rgba(224, 32, 46, 0.16); color: var(--red); }
.beef-entry-status.dormant { background: rgba(154, 154, 154, 0.16); color: var(--gray); }
.beef-entry-updated { font-size: 12px; color: var(--gray); margin: 0 0 12px; }
.beef-entry-summary { font-size: 15px; line-height: 1.65; color: var(--text-secondary); margin: 0 0 14px; }
.beef-entry-links { display: flex; flex-wrap: wrap; gap: 8px 18px; }
.beef-entry-links a { font-size: 13px; font-weight: 700; color: var(--red); text-decoration: none; }
.beef-entry-links a:hover { text-decoration: underline; }
"""

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------



def search_bar_html(extra_class: str = "") -> str:
    """A GET-submitting search form. Reused in the header (every page) and
    next to the page-picker in pagination -- a page can render more than
    one instance at once (e.g. the homepage has both), so every instance
    shares the "search-input" class rather than an id, letting the search
    page's own script prefill all of them from the URL on load. Submits to
    /search/?q=... where search/index.html does the actual matching
    client-side against search-index.json -- this is a static site with no
    backend, so there's no server-side query handling to wire up."""
    classes = ("search-bar " + extra_class).strip()
    return f"""
    <form class="{classes}" action="/search/" method="get" role="search">
      <input type="search" name="q" class="search-input" placeholder="Search articles&hellip;" aria-label="Search articles" autocomplete="off">
      <button type="submit" aria-label="Search">
        <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="8.5" cy="8.5" r="6"></circle>
          <line x1="13.2" y1="13.2" x2="18" y2="18"></line>
        </svg>
      </button>
    </form>"""

def header_html(prefix: str, active: str = None) -> str:
    """
    active is one of "all", a category key, or None. None still renders the
    filter pills (so they're reachable from article pages) but leaves all of
    them unhighlighted.
    """
    pills = [f'<a class="{"active" if active == "all" else ""}" href="/">All</a>']
    for key, label in CATEGORIES:
        pills.append(f'<a class="{"active" if active == key else ""}" href="/category/{key}/">{label}</a>')
    # About intentionally isn't in this top nav -- it's linked from the
    # footer's "About UNO Entertainment" column instead, so it doesn't
    # compete for space with the actual content categories up here.

    return f"""
<header>
  <a class="brand-link" href="/">
    <img class="logo logo-dark-mode" src="{prefix}uno-logo.png" alt="UNO Entertainment">
    <img class="logo logo-light-mode" src="{prefix}uno-logo-dark.png" alt="UNO Entertainment">
    <span class="tagline">The Culture's Feed</span>
  </a>
  <input type="checkbox" id="nav-toggle" class="nav-toggle-checkbox">
  <button type="button" class="theme-toggle-btn theme-toggle-btn-mobile" aria-label="Switch to light mode" title="Switch to light mode">
    <svg class="theme-icon-sun" viewBox="0 0 20 20" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
      <circle cx="10" cy="10" r="4"></circle>
      <path d="M10 1.5v2.4M10 16.1v2.4M3.5 3.5l1.7 1.7M14.8 14.8l1.7 1.7M1.5 10h2.4M16.1 10h2.4M3.5 16.5l1.7-1.7M14.8 5.2l1.7-1.7"></path>
    </svg>
    <svg class="theme-icon-moon" viewBox="0 0 20 20" width="15" height="15" fill="currentColor">
      <path d="M17.3 13.3A8 8 0 016.7 2.7a.6.6 0 00-.7-.85A8 8 0 1018.15 14a.6.6 0 00-.85-.7z"></path>
    </svg>
  </button>
  <label for="nav-toggle" class="nav-toggle-btn" aria-label="Menu">
    <span></span><span></span><span></span>
  </label>
  {search_bar_html("header-search")}
  <nav class="header-filters">
    {"".join(pills)}
    <button type="button" class="theme-toggle-btn theme-toggle-btn-desktop" aria-label="Switch to light mode" title="Switch to light mode">
      <svg class="theme-icon-sun" viewBox="0 0 20 20" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
        <circle cx="10" cy="10" r="4"></circle>
        <path d="M10 1.5v2.4M10 16.1v2.4M3.5 3.5l1.7 1.7M14.8 14.8l1.7 1.7M1.5 10h2.4M16.1 10h2.4M3.5 16.5l1.7-1.7M14.8 5.2l1.7-1.7"></path>
      </svg>
      <svg class="theme-icon-moon" viewBox="0 0 20 20" width="15" height="15" fill="currentColor">
        <path d="M17.3 13.3A8 8 0 016.7 2.7a.6.6 0 00-.7-.85A8 8 0 1018.15 14a.6.6 0 00-.85-.7z"></path>
      </svg>
    </button>
  </nav>
</header>
<script>
(function() {{
  // Two toggle buttons exist in the DOM (one styled as a nav pill for
  // desktop, one styled as a standalone icon button for the mobile header
  // row) so the control is reachable without opening the hamburger menu on
  // mobile. Only one is ever visible at a time per the CSS media query, but
  // both are kept in sync here in case that ever changes.
  var btns = document.querySelectorAll('.theme-toggle-btn');
  if (!btns.length) return;
  function applyLabel() {{
    var isLight = document.documentElement.getAttribute('data-theme') === 'light';
    var label = isLight ? 'Switch to dark mode' : 'Switch to light mode';
    btns.forEach(function(btn) {{
      btn.setAttribute('aria-label', label);
      btn.setAttribute('title', label);
    }});
  }}
  applyLabel();
  btns.forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var isLight = document.documentElement.getAttribute('data-theme') === 'light';
      if (isLight) {{
        document.documentElement.removeAttribute('data-theme');
        try {{ localStorage.setItem('uno_theme', 'dark'); }} catch (e) {{}}
      }} else {{
        document.documentElement.setAttribute('data-theme', 'light');
        try {{ localStorage.setItem('uno_theme', 'light'); }} catch (e) {{}}
      }}
      applyLabel();
    }});
  }});
}})();
</script>"""


def cookie_banner_html(prefix: str) -> str:
    """Lightweight, dependency-free cookie notice. UNO Ent doesn't set any
    tracking cookies today, but this is here ahead of adding analytics/ads
    so there's already a consent mechanism and a Privacy Policy to point to.
    Dismissal is remembered in localStorage so it only shows once per
    browser; wrapped in try/except since some browsers block storage access
    entirely (e.g. private mode) and that shouldn't ever break the page."""
    return f"""
<div id="uno-cookie-banner" class="cookie-banner" hidden>
  <div class="cookie-banner-inner">
    <p>UNO Entertainment doesn't use tracking cookies today. If that changes — for analytics or
    advertising — we'll ask first. Read our <a href="/privacy-policy/">Privacy Policy</a> for details.</p>
    <button type="button" class="cookie-banner-btn" onclick="unoCookieConsent()">Got It</button>
  </div>
</div>
<script>
(function() {{
  try {{
    if (!localStorage.getItem('uno_cookie_notice_seen')) {{
      var b = document.getElementById('uno-cookie-banner');
      if (b) b.hidden = false;
    }}
  }} catch (e) {{}}
}})();
function unoCookieConsent() {{
  try {{ localStorage.setItem('uno_cookie_notice_seen', '1'); }} catch (e) {{}}
  var b = document.getElementById('uno-cookie-banner');
  if (b) b.hidden = true;
}}
</script>"""


SEARCH_SUGGEST_JS = """
(function () {
  var CATEGORY_LABELS = { news: "News", rumors: "Rumors", videos: "Videos", music: "Music", sports: "Sports", opinion: "Opinion" };
  var indexPromise = null;
  function loadIndex() { if (!indexPromise) { indexPromise = fetch("/search-index.json").then(function (r) { return r.json(); }); } return indexPromise; }
  function escapeHtml(s) { return (s || "").replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function scoreArticle(a, tokens) {
    var title = (a.title || "").toLowerCase(), excerpt = (a.excerpt || "").toLowerCase(), source = (a.source || "").toLowerCase(), s = 0;
    for (var i = 0; i < tokens.length; i++) { var t = tokens[i]; if (!t) continue; if (title.indexOf(t) !== -1) s += 3; if (excerpt.indexOf(t) !== -1) s += 1; if (source.indexOf(t) !== -1) s += 1; }
    return s;
  }
  document.querySelectorAll(".search-bar").forEach(function (bar) {
    var input = bar.querySelector(".search-input");
    if (!input) return;
    if (bar.classList.contains("header-search")) {
      var toggleBtn = bar.querySelector('button[type="submit"]');
      if (toggleBtn) {
        toggleBtn.addEventListener("click", function (e) {
          if (window.matchMedia("(max-width: 760px)").matches && !bar.classList.contains("expanded")) {
            e.preventDefault();
            bar.classList.add("expanded");
            input.focus();
          }
        });
        document.addEventListener("click", function (e) {
          if (!bar.contains(e.target) && bar.classList.contains("expanded") && !input.value) {
            bar.classList.remove("expanded");
          }
        });
      }
    }
    var panel = document.createElement("div");
    panel.className = "search-suggest"; panel.hidden = true; bar.appendChild(panel);
    var debounceTimer = null, activeIndex = -1;
    function closePanel() { panel.hidden = true; panel.innerHTML = ""; activeIndex = -1; }
    function highlight(items) {
      items.forEach(function (it, i) { it.classList.toggle("active", i === activeIndex); });
      if (activeIndex > -1 && items[activeIndex]) { items[activeIndex].scrollIntoView({ block: "nearest" }); }
    }
    function renderSuggestions(items, q) {
      activeIndex = -1;
      if (!items.length) { panel.innerHTML = '<div class="search-suggest-empty">No matches for &ldquo;' + escapeHtml(q) + '&rdquo;</div>'; panel.hidden = false; return; }
      panel.innerHTML = items.map(function (a) {
        var thumb = a.thumbnail ? '<img src="' + escapeHtml(a.thumbnail) + '" alt="' + escapeHtml(a.title) + '" loading="lazy">' : '<img src="/uno-logo.png" alt="UNO Entertainment" loading="lazy" class="search-suggest-fallback">';
        var cat = CATEGORY_LABELS[a.category];
        return '<a href="/articles/' + a.slug + '/" class="search-suggest-item">' + thumb + '<span class="search-suggest-text"><span class="search-suggest-title">' + escapeHtml(a.title) + '</span>' + (cat ? '<span class="search-suggest-cat">' + cat + '</span>' : '') + '</span></a>';
      }).join("") + '<a href="/search/?q=' + encodeURIComponent(q) + '" class="search-suggest-viewall">See all results for &ldquo;' + escapeHtml(q) + '&rdquo; &rarr;</a>';
      panel.hidden = false;
    }
    function runQuery(q) {
      loadIndex().then(function (data) {
        var tokens = q.toLowerCase().split(/\s+/).filter(Boolean), scored = [];
        for (var i = 0; i < data.length; i++) { var s = scoreArticle(data[i], tokens); if (s > 0) scored.push([s, data[i]]); }
        scored.sort(function (a, b) { if (b[0] !== a[0]) return b[0] - a[0]; return new Date(b[1].date) - new Date(a[1].date); });
        renderSuggestions(scored.slice(0, 6).map(function (p) { return p[1]; }), q);
      }).catch(function () { closePanel(); });
    }
    input.addEventListener("input", function () {
      var q = input.value.trim();
      clearTimeout(debounceTimer);
      if (q.length < 2) { closePanel(); return; }
      debounceTimer = setTimeout(function () { runQuery(q); }, 150);
    });
    input.addEventListener("keydown", function (e) {
      if (panel.hidden) return;
      var items = panel.querySelectorAll(".search-suggest-item");
      if (e.key === "ArrowDown") { e.preventDefault(); activeIndex = Math.min(activeIndex + 1, items.length - 1); highlight(items); }
      else if (e.key === "ArrowUp") { e.preventDefault(); activeIndex = Math.max(activeIndex - 1, -1); highlight(items); }
      else if (e.key === "Enter") { if (activeIndex > -1 && items[activeIndex]) { e.preventDefault(); window.location.href = items[activeIndex].getAttribute("href"); } }
      else if (e.key === "Escape") { closePanel(); }
    });
    input.addEventListener("blur", function () { setTimeout(closePanel, 150); });
    document.addEventListener("click", function (e) { if (!bar.contains(e.target)) closePanel(); });
  });
})();
"""


def footer_html(prefix: str) -> str:
    year = datetime.now(timezone.utc).year
    section_links = "".join(
        f'\n        <a href="/category/{key}/">{label}</a>' for key, label in CATEGORIES
    )
    return f"""
<footer>
  <div class="footer-inner">
    <div class="footer-top">
      <a href="/"><img class="footer-logo logo-dark-mode" src="{prefix}uno-logo.png" alt="UNO Entertainment"><img class="footer-logo logo-light-mode" src="{prefix}uno-logo-dark.png" alt="UNO Entertainment"></a>
      <p class="footer-tagline">The Culture's Feed</p>
    </div>

    <div class="footer-columns">
      <div class="footer-col">
        <h4>Sections</h4>
        <a href="/">Home</a>{section_links}
        <a href="/topics/">Topics</a>
        <a href="/hip-hop-beef-tracker/">Beef Tracker</a>
        <a href="/about/">About</a>
      </div>
      <div class="footer-col">
        <h4>About UNO Entertainment</h4>
        <p>Los Angeles. Hip-hop and culture, summarized. Credit always goes back to the source.</p>
        <a href="/about/">Read more</a>
      </div>
      <div class="footer-col">
        <h4>Get In Touch</h4>
        <p>Questions or a story tip?</p>
        <a class="footer-contact-email" href="mailto:support@unoent.com">support@unoent.com</a>
      </div>
    </div>

    <div class="footer-bottom">
      <p class="footer-source-note">Every story here includes a summary and a link to the original reporting
      from outlets like XXL, HotNewHipHop, and The Source. Full credit and the complete story always live
      with them.</p>
      <p class="footer-copy">
        <span>&copy; {year} UNO Entertainment. All Rights Reserved.</span>
        <span><a href="/privacy-policy/">Privacy Policy</a> &middot; <a href="/terms/">Terms of Service</a></span>
      </p>
    </div>
  </div>
</footer>
{cookie_banner_html(prefix)}
<script>{SEARCH_SUGGEST_JS}</script>"""


def meta_html(prefix: str, title: str, description: str, canonical_url: str, image_url: str = None) -> str:
    """Favicon links + Open Graph / Twitter card tags, shared by every page.
    image_url and canonical_url must be absolute (http/https) -- social apps
    like iMessage, Facebook, and Twitter/X ignore relative og:image URLs."""
    image_url = image_url or DEFAULT_OG_IMAGE
    return f"""
<meta name="description" content="{escape(description)}">
<link rel="canonical" href="{canonical_url}">
<link rel="icon" href="{prefix}favicon.ico" sizes="any">
<link rel="icon" type="image/png" sizes="32x32" href="{prefix}favicon-32.png">
<link rel="icon" type="image/png" sizes="16x16" href="{prefix}favicon-16.png">
<link rel="apple-touch-icon" sizes="180x180" href="{prefix}favicon-180.png">
<meta property="og:type" content="website">
<meta property="og:site_name" content="UNO Entertainment">
<meta property="og:title" content="{escape(title)}">
<meta property="og:description" content="{escape(description)}">
<meta property="og:image" content="{image_url}">
<meta property="og:url" content="{canonical_url}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{escape(title)}">
<meta name="twitter:description" content="{escape(description)}">
<meta name="twitter:image" content="{image_url}">"""


def card_html(a: dict, prefix: str) -> str:
    thumb = a.get("thumbnail")
    thumb_html = (
        f'<img src="{escape(thumb)}" alt="{escape(a["title"])}" loading="lazy" class="card-thumb" onload="this.style.animation=\'none\'">'
        if thumb
        else f'<img src="{prefix}uno-logo.png" alt="UNO Entertainment" loading="lazy" class="card-thumb card-thumb-fallback">'
    )
    cat_key = a.get("category")
    cat_label = CATEGORY_LABELS.get(cat_key)
    cat_html = f'<span class="card-category">{escape(cat_label)}</span><span class="card-dot">&middot;</span>' if cat_label else ""
    return f"""
    <a class="card" href="/articles/{a['slug']}/">
      {thumb_html}
      <div class="card-body">
        <div class="card-meta">{cat_html}{escape(time_ago(a['date']))}</div>
        <h2 class="card-title">{escape(a['title'])}</h2>
        <p class="card-excerpt">{escape(a['excerpt'])}</p>
        <span class="card-link">Read More &rarr;</span>
      </div>
    </a>"""


def page_href(target_page: int) -> str:
    """Absolute link to a homepage pagination page. Page 1 always links to
    the site root ("/") rather than index.html; later pages link to
    /page/{n}/ (which is page/{n}/index.html on disk) rather than
    page/{n}.html, so the .html extension never shows up in the address bar
    -- the same treatment the homepage itself has always had."""
    if target_page == 1:
        return "/"
    return f"/page/{target_page}/"


def page_jump_html(current_page: int, total_pages: int, href_for) -> str:
    """A 'go to page' dropdown, given a function mapping a 1-indexed target
    page number to the relative URL for it from the current page. Renders
    nothing for a single-page listing, same as the arrows above it."""
    if total_pages <= 1:
        return ""
    options = "".join(
        f'<option value="{escape(href_for(p))}"{" selected" if p == current_page else ""}>{p}</option>'
        for p in range(1, total_pages + 1)
    )
    return f"""
    <label class="page-jump">
      Go to page
      <select onchange="if(this.value) window.location.href=this.value;">
        {options}
      </select>
    </label>"""


def pagination_html(current_page: int, total_pages: int) -> str:
    if total_pages <= 1:
        return ""
    if current_page > 1:
        prev = f'<a href="{page_href(current_page - 1)}">&larr; Newer</a>'
    else:
        prev = '<span class="disabled">&larr; Newer</span>'
    if current_page < total_pages:
        nxt = f'<a href="{page_href(current_page + 1)}">Older &rarr;</a>'
    else:
        nxt = '<span class="disabled">Older &rarr;</span>'
    jump = page_jump_html(current_page, total_pages, page_href)
    return f"""
  <nav class="pagination">
    {prev}
    <span class="page-count">Page {current_page} of {total_pages}</span>
    {jump}
    {search_bar_html("pagination-search")}
    {nxt}
  </nav>"""


def build_page(page_num: int, total_pages: int):
    start = (page_num - 1) * ARTICLES_PER_PAGE
    page_articles = ARTICLES[start:start + ARTICLES_PER_PAGE]
    # page/{n}/index.html is 2 directories deep; index.html at the root is 0.
    prefix = "" if page_num == 1 else "../../"
    cards = "\n".join(card_html(a, prefix) for a in page_articles)
    title = "UNO Entertainment" if page_num == 1 else f"UNO Entertainment | Page {page_num}"
    canonical = SITE_URL + page_href(page_num)
    description = SITE_DESCRIPTION if page_num == 1 else f"{SITE_DESCRIPTION} (Page {page_num})"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
{GTM_HEAD_SNIPPET}
{THEME_INIT_SNIPPET}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
{meta_html(prefix, title, description, canonical)}
{website_jsonld() if page_num == 1 else ""}
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
{GTM_BODY_SNIPPET}
{header_html(prefix, "all")}
<main>
  <div class="grid">
    {cards}
  </div>
  {pagination_html(page_num, total_pages)}
</main>
{footer_html(prefix)}
</body>
</html>
"""
    if page_num == 1:
        with open("index.html", "w") as f:
            f.write(html)
    else:
        import os
        os.makedirs(f"page/{page_num}", exist_ok=True)
        with open(f"page/{page_num}/index.html", "w") as f:
            f.write(html)


def build_pages():
    import math
    total_pages = max(1, math.ceil(ARTICLE_COUNT / ARTICLES_PER_PAGE))
    for page_num in range(1, total_pages + 1):
        build_page(page_num, total_pages)
    return total_pages


# ---------------------------------------------------------------------------
# Category pages — same idea as build_page/build_pages, but scoped to one
# category and living in category/ instead of the root + page/.
# ---------------------------------------------------------------------------


def category_page_href(cat_key: str, target_page: int) -> str:
    """Absolute link to a category page. Page 1 is /category/{cat}/ (i.e.
    category/{cat}/index.html on disk); later pages are /category/{cat}/{n}/
    -- same clean-URL treatment as page_href() above, never a bare .html."""
    if target_page == 1:
        return f"/category/{cat_key}/"
    return f"/category/{cat_key}/{target_page}/"


def category_pagination_html(cat_key: str, current_page: int, total_pages: int) -> str:
    if total_pages <= 1:
        return ""
    if current_page > 1:
        prev = f'<a href="{category_page_href(cat_key, current_page - 1)}">&larr; Newer</a>'
    else:
        prev = '<span class="disabled">&larr; Newer</span>'
    if current_page < total_pages:
        nxt = f'<a href="{category_page_href(cat_key, current_page + 1)}">Older &rarr;</a>'
    else:
        nxt = '<span class="disabled">Older &rarr;</span>'
    jump = page_jump_html(current_page, total_pages, lambda p: category_page_href(cat_key, p))
    return f"""
  <nav class="pagination">
    {prev}
    <span class="page-count">Page {current_page} of {total_pages}</span>
    {jump}
    {search_bar_html("pagination-search")}
    {nxt}
  </nav>"""


def build_category(cat_key: str, cat_label: str):
    import math
    import os

    cat_articles = [a for a in ARTICLES if a.get("category") == cat_key]
    total_pages = max(1, math.ceil(len(cat_articles) / ARTICLES_PER_PAGE))

    for page_num in range(1, total_pages + 1):
        start = (page_num - 1) * ARTICLES_PER_PAGE
        page_articles = cat_articles[start:start + ARTICLES_PER_PAGE]
        # category/{cat}/index.html is 2 directories deep, category/{cat}/{n}/index.html is 3.
        prefix = "../../" if page_num == 1 else "../../../"
        cards = "\n".join(card_html(a, prefix) for a in page_articles)
        title = f"{cat_label} | UNO Entertainment" + (f" (Page {page_num})" if page_num > 1 else "")
        empty_state = (
            '<p style="color: var(--gray); font-size: 15px;">No stories in this category yet. Check back soon.</p>'
            if not page_articles else ""
        )
        canonical = f"{SITE_URL}{category_page_href(cat_key, page_num)}"
        base_description = CATEGORY_DESCRIPTIONS.get(
            cat_key,
            f"The latest {cat_label.lower()} in hip-hop and culture, curated by UNO Entertainment.",
        )
        description = base_description if page_num == 1 else f"{base_description} (Page {page_num})"
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
{GTM_HEAD_SNIPPET}
{THEME_INIT_SNIPPET}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
{meta_html(prefix, title, description, canonical)}
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
{GTM_BODY_SNIPPET}
{header_html(prefix, cat_key)}
<main>
  <div class="grid">
    {cards}
  </div>
  {empty_state}
  {category_pagination_html(cat_key, page_num, total_pages)}
</main>
{footer_html(prefix)}
</body>
</html>
"""
        out_dir = f"category/{cat_key}" if page_num == 1 else f"category/{cat_key}/{page_num}"
        os.makedirs(out_dir, exist_ok=True)
        with open(f"{out_dir}/index.html", "w") as f:
            f.write(html)


def build_categories():
    for cat_key, cat_label in CATEGORIES:
        build_category(cat_key, cat_label)


# ---------------------------------------------------------------------------
# Topic hub pages — same page shape as a category page (paginated grid of
# cards), but scoped to a topic match instead of a category field, and living
# under topic/ instead of category/.
# ---------------------------------------------------------------------------


def topic_page_href(slug: str, target_page: int) -> str:
    if target_page == 1:
        return f"/topic/{slug}/"
    return f"/topic/{slug}/{target_page}/"


def topic_pagination_html(slug: str, current_page: int, total_pages: int) -> str:
    if total_pages <= 1:
        return ""
    if current_page > 1:
        prev = f'<a href="{topic_page_href(slug, current_page - 1)}">&larr; Newer</a>'
    else:
        prev = '<span class="disabled">&larr; Newer</span>'
    if current_page < total_pages:
        nxt = f'<a href="{topic_page_href(slug, current_page + 1)}">Older &rarr;</a>'
    else:
        nxt = '<span class="disabled">Older &rarr;</span>'
    jump = page_jump_html(current_page, total_pages, lambda p: topic_page_href(slug, p))
    return f"""
  <nav class="pagination">
    {prev}
    <span class="page-count">Page {current_page} of {total_pages}</span>
    {jump}
    {search_bar_html("pagination-search")}
    {nxt}
  </nav>"""


def build_topic(slug: str, name: str):
    import math
    import os

    articles = topic_articles(slug)
    if not articles:
        # Nothing matches yet -- don't publish an empty hub page. It'll get
        # built automatically as soon as the archive picks up a matching
        # story (this function re-runs on every build).
        return False
    total_pages = max(1, math.ceil(len(articles) / ARTICLES_PER_PAGE))

    for page_num in range(1, total_pages + 1):
        start = (page_num - 1) * ARTICLES_PER_PAGE
        page_articles = articles[start:start + ARTICLES_PER_PAGE]
        # topic/{slug}/index.html is 2 directories deep, topic/{slug}/{n}/index.html is 3.
        prefix = "../../" if page_num == 1 else "../../../"
        cards = "\n".join(card_html(a, prefix) for a in page_articles)
        title = f"{name} News | UNO Entertainment" + (f" (Page {page_num})" if page_num > 1 else "")
        canonical = f"{SITE_URL}{topic_page_href(slug, page_num)}"
        base_description = (
            f"Every UNO Entertainment story on {name} -- news, rumors, and updates, "
            f"all in one place."
        )
        description = base_description if page_num == 1 else f"{base_description} (Page {page_num})"
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
{GTM_HEAD_SNIPPET}
{THEME_INIT_SNIPPET}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
{meta_html(prefix, title, description, canonical)}
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
{GTM_BODY_SNIPPET}
{header_html(prefix)}
<main>
  <div class="topic-hub-heading">
    <h1>{escape(name)}</h1>
    <p class="topic-hub-sub">Every UNO Entertainment story on {escape(name)}, newest first.</p>
  </div>
  <div class="grid">
    {cards}
  </div>
  {topic_pagination_html(slug, page_num, total_pages)}
</main>
{footer_html(prefix)}
</body>
</html>
"""
        out_dir = f"topic/{slug}" if page_num == 1 else f"topic/{slug}/{page_num}"
        os.makedirs(out_dir, exist_ok=True)
        with open(f"{out_dir}/index.html", "w") as f:
            f.write(html)
    return True


def build_topics_index(live_topics: list) -> None:
    """/topics/ -- a directory linking to every currently-live topic hub, so
    hub pages are always reachable by following links from the site (never
    orphan pages that only exist in the sitemap) and a visitor can browse
    "who does UNO Ent cover a lot of" at a glance. live_topics is the list of
    (slug, name, article_count) for topics that actually published a page --
    a topic with 0 matches today is left out until it has something to show."""
    import os

    prefix = "../"
    title = "Topics | UNO Entertainment"
    canonical = f"{SITE_URL}/topics/"
    description = "Browse UNO Entertainment's coverage by artist and storyline -- every topic hub in one place."
    items = "\n".join(
        f'    <a class="topic-index-item" href="/topic/{slug}/">'
        f'<span class="topic-index-name">{escape(name)}</span>'
        f'<span class="topic-index-count">{count} {"story" if count == 1 else "stories"}</span></a>'
        for slug, name, count in live_topics
    )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
{GTM_HEAD_SNIPPET}
{THEME_INIT_SNIPPET}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
{meta_html(prefix, title, description, canonical)}
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
{GTM_BODY_SNIPPET}
{header_html(prefix)}
<main>
  <div class="topic-hub-heading">
    <h1>Topics</h1>
    <p class="topic-hub-sub">Browse UNO Entertainment's coverage by artist and storyline.</p>
  </div>
  <div class="topic-index-grid">
{items}
  </div>
</main>
{footer_html(prefix)}
</body>
</html>
"""
    os.makedirs("topics", exist_ok=True)
    with open("topics/index.html", "w") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# Hip-Hop Beef Tracker — a single evergreen pillar page at
# /hip-hop-beef-tracker/ rounding up the storylines UNO Ent has covered the
# most (curated below, not auto-detected — matching two names co-occurring
# in an article is a decent signal for a topic hub, but "is this actually an
# active beef" takes a human call fabrication). Every claim in SUMMARY below
# is a paraphrase of something already reported in one of the linked
# ARTICLE_SLUGS -- nothing here should ever assert a fact that isn't backed
# by an article already in the archive.
#
# MAINTENANCE: this needs a human pass periodically, the same way the
# category/topic lists above do -- add a new entry when a storyline UNO Ent
# is covering repeatedly doesn't have one yet, flip STATUS to "dormant" once
# a beef goes quiet for a while, and add newer ARTICLE_SLUGS to an existing
# entry as the story develops. build_beef_tracker() below only ever links to
# slugs that still exist in ARTICLES (a slug removed by check_links.py's
# dead-link pruning is silently dropped from an entry's link list, never
# left as a broken link), and computes "last updated" from whichever of an
# entry's still-live articles is newest -- but it doesn't invent new
# entries or new claims on its own.
BEEF_TRACKER = [
    {
        "slug": "drake-kendrick-lamar",
        "title": "Drake vs. Kendrick Lamar",
        "status": "active",
        "summary": (
            "Their 2024 lyrical war never fully cooled off. Drake has kept "
            "referencing the battle -- including barking along to Kendrick's "
            "\"Not Like Us\" in a viral clip -- and the beef has since pulled "
            "in a second front: Drake trading shots with Jay-Z as tension "
            "with Roc Nation escalates, with Kendrick's name coming up in "
            "that conversation too."
        ),
        "article_slugs": [
            "drake-takes-more-shots-at-jay-z-as-roc-nation-beef-intensifies",
            "drake-makes-a-rare-admission-about-his-battle-with-kendrick-lamar",
            "drake-barks-to-the-beat-of-not-like-us-in-new-viral-video-edit",
        ],
        "topic_slugs": ["drake", "kendrick-lamar", "jay-z"],
    },
    {
        "slug": "doja-cat-tyga",
        "title": "Doja Cat vs. Tyga",
        "status": "active",
        "summary": (
            "Doja Cat publicly tore into Tyga over his new album $TARFACE, "
            "accusing him of leaning on AI in its production and calling him "
            "out during a livestream. Tyga pushed back, insisting the album "
            "isn't \"totally AI\" and responding directly to her criticism."
        ),
        "article_slugs": [
            "doja-cat-brutally-disses-tyga-his-tarface-album",
            "tyga-insists-tarface-album-not-totally-ai-responds-to-doja-cat",
            "doja-cat-calls-tyga-a-penis-for-releasing-a-i-album",
        ],
        "topic_slugs": ["doja-cat", "tyga"],
    },
    {
        "slug": "cardi-b-bia",
        "title": "Cardi B vs. BIA",
        "status": "active",
        "summary": (
            "A long-running feud between Cardi B and BIA flared back up: BIA "
            "made clear she wouldn't apologize to Cardi the way she had to "
            "Doja Cat, and Cardi responded by accusing BIA of spreading "
            "rumors about her relationship with Offset during an X Spaces "
            "conversation."
        ),
        "article_slugs": [
            "bia-revives-cardi-b-feud-cardi-b-unleashes-explosive-response",
            "cardi-b-reignites-feud-with-bia-over-offset-cheating-rumors",
        ],
        "topic_slugs": ["cardi-b"],
    },
    {
        "slug": "rick-ross-50-cent",
        "title": "Rick Ross vs. 50 Cent",
        "status": "active",
        "summary": (
            "50 Cent mocked Rick Ross's album sales and a sparse Detroit "
            "crowd; Ross fired back with his own streaming numbers for "
            "*Set In Stone* and challenged 50's business record, and the "
            "back-and-forth has since spilled into jokes about liquor "
            "brands and sneaker deals."
        ),
        "article_slugs": [
            "rick-ross-challenges-50-cent-s-business-record-after-album-sales-jab",
            "rick-ross-challenges-50-cent-s-sales-jokes-with-his-own-math",
            "50-cent-insists-rick-ross-career-is-over-after-low-album-sales",
        ],
        "topic_slugs": ["rick-ross", "50-cent"],
    },
    {
        "slug": "50-cent-diddy",
        "title": "50 Cent vs. Diddy",
        "status": "dormant",
        "summary": (
            "Not a diss-track beef so much as a running war of press hits: "
            "Diddy has accused Lil Rod of stealing private footage and "
            "feeding it to 50 Cent's Netflix documentary, and 50 has kept "
            "needling Diddy over the ongoing Tupac murder-trial coverage, "
            "including a public jab at Keefe D over his claims."
        ),
        "article_slugs": [
            "exclusive-diddy-claims-lil-rod-sold-stolen-footage-into-50-cent-s-netf",
            "50-cent-clowns-keefe-d-for-claiming-diddy-didn-t-murder-tupac",
        ],
        "topic_slugs": ["50-cent", "diddy", "keefe-d"],
    },
]


def beef_tracker_jsonld(live_entries: list) -> str:
    """ItemList structured data for the tracker -- lets Google understand
    this is a curated list of distinct storylines, not one long article."""
    data = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": "Hip-Hop Beef Tracker",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": i + 1,
                "name": entry["title"],
                "url": f"{SITE_URL}/hip-hop-beef-tracker/#{entry['slug']}",
            }
            for i, entry in enumerate(live_entries)
        ],
    }
    return jsonld_script(data)


def build_beef_tracker():
    """/hip-hop-beef-tracker/ -- one evergreen page, not paginated. Returns
    the list of entries that actually rendered (had at least one surviving
    article link) so main()/build_sitemap can both use it as the single
    source of truth, same pattern as build_topic_hubs()."""
    import os

    article_by_slug = {a["slug"]: a for a in ARTICLES}
    live_entries = []
    entries_html = []
    for entry in BEEF_TRACKER:
        live_articles = [article_by_slug[s] for s in entry["article_slugs"] if s in article_by_slug]
        if not live_articles:
            # Every source article for this storyline has since been pruned
            # (dead link) -- skip rather than publish an unsourced claim.
            continue
        live_entries.append(entry)
        last_updated = max(live_articles, key=lambda a: a["date"])["date"]
        links = " &middot; ".join(
            f'<a href="/articles/{a["slug"]}/">{escape(a["title"])}</a>' for a in live_articles
        )
        topic_links = " &middot; ".join(
            f'<a href="/topic/{slug}/">{escape(TOPIC_LABELS[slug])}</a>'
            for slug in entry["topic_slugs"]
            if slug in TOPIC_LABELS and topic_articles(slug)
        )
        entries_html.append(f"""
  <article class="beef-entry" id="{entry['slug']}">
    <div class="beef-entry-header">
      <h2 class="beef-entry-title">{escape(entry['title'])}</h2>
      <span class="beef-entry-status {entry['status']}">{entry['status']}</span>
    </div>
    <p class="beef-entry-updated">Last updated {escape(time_ago(last_updated))}</p>
    <p class="beef-entry-summary">{escape(entry['summary'])}</p>
    <div class="beef-entry-links">{links}{" &middot; " + topic_links if topic_links else ""}</div>
  </article>""")

    prefix = "../"
    title = "Hip-Hop Beef Tracker | UNO Entertainment"
    canonical = f"{SITE_URL}/hip-hop-beef-tracker/"
    description = (
        "Every hip-hop beef UNO Entertainment is tracking right now -- who's "
        "involved, what started it, and where each storyline stands."
    )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
{GTM_HEAD_SNIPPET}
{THEME_INIT_SNIPPET}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
{meta_html(prefix, title, description, canonical)}
{beef_tracker_jsonld(live_entries)}
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
{GTM_BODY_SNIPPET}
{header_html(prefix)}
<main>
  <div class="topic-hub-heading">
    <h1>Hip-Hop Beef Tracker</h1>
    <p class="topic-hub-sub">Every storyline UNO Entertainment is following right now, updated as it develops.</p>
  </div>
  <p class="beef-tracker-intro">From lyrical wars to X Spaces callouts, this is where UNO Ent keeps score.
  Each entry links back to the stories behind it -- click through for the full picture, or check the linked
  artist hub for everything else we've covered on them.</p>
{"".join(entries_html)}
</main>
{footer_html(prefix)}
</body>
</html>
"""
    os.makedirs("hip-hop-beef-tracker", exist_ok=True)
    with open("hip-hop-beef-tracker/index.html", "w") as f:
        f.write(html)
    return live_entries


def build_topic_hubs():
    """Builds every topic hub with at least one matching article today, plus
    the /topics/ directory linking to all of them. Returns the list of
    (slug, name, article_count) that actually got published, for the
    sitemap and the topics-index page to both use as their single source of
    truth."""
    live_topics = []
    for slug, name, _ in TOPICS:
        count = len(topic_articles(slug))
        if count > 0 and build_topic(slug, name):
            live_topics.append((slug, name, count))
    build_topics_index(live_topics)
    return live_topics


def article_jsonld(a: dict, canonical: str, description: str) -> str:
    """NewsArticle structured data for one article page. Lets Google show
    richer results (byline, publish date, article thumbnail) and is one of
    the signals used to qualify for Google News / Top Stories surfaces."""
    thumb = a.get("thumbnail") or DEFAULT_OG_IMAGE
    try:
        date_iso = datetime.fromisoformat(a["date"].replace("Z", "+00:00")).isoformat()
    except (ValueError, KeyError):
        date_iso = datetime.now(timezone.utc).isoformat()
    data = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": a["title"][:110],
        "description": description,
        "image": [thumb],
        "datePublished": date_iso,
        "dateModified": date_iso,
        "author": {"@type": "Organization", "name": "UNO Entertainment"},
        "publisher": {
            "@type": "Organization",
            "name": "UNO Entertainment",
            "logo": {"@type": "ImageObject", "url": f"{SITE_URL}/og-image.png"},
        },
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
    }
    return jsonld_script(data)


def website_jsonld() -> str:
    """WebSite + Organization structured data for the homepage — lets Google
    associate unoent.com's name/logo with the site and enables the sitelinks
    search box in results."""
    data = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": "UNO Entertainment",
        "url": SITE_URL,
        "description": SITE_DESCRIPTION,
        "publisher": {
            "@type": "Organization",
            "name": "UNO Entertainment",
            "logo": {"@type": "ImageObject", "url": f"{SITE_URL}/og-image.png"},
        },
        "potentialAction": {
            "@type": "SearchAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": f"{SITE_URL}/search/?q={{search_term_string}}",
            },
            "query-input": "required name=search_term_string",
        },
    }
    return jsonld_script(data)


def build_article(a: dict):
    # articles/{slug}/index.html is 2 directories deep.
    prefix = "../../"
    thumb = a.get("thumbnail")
    hero_html = (
        f'<img class="article-hero" src="{escape(thumb)}" alt="{escape(a["title"])}" onload="this.style.animation=\'none\'">'
        if thumb
        else f'<img class="article-hero article-hero-fallback" src="{prefix}uno-logo.png" alt="UNO Entertainment">'
    )
    # Hand-authored UNO Ent originals set "body_html" directly (full control
    # over paragraphs, subheads, embedded video, custom CTAs) instead of
    # going through the standard RSS-summary + "Read Full Story On {source}"
    # outbound-CTA block every scraped article uses.
    if a.get("body_html"):
        body_class = "feature-body" if a.get("layout") == "feature" else "article-body"
        body_html = f'<div class="{body_class}">{a["body_html"]}</div>'
    else:
        body_html = f'''<p class="article-summary">{escape(a.get('summary') or a.get('excerpt', ''))}</p>
  <a class="outbound-cta" href="{escape(a['link'])}" target="_blank" rel="noopener noreferrer">
    Read The Full Story On {escape(a['source'])} &rarr;
  </a>
  <p class="outbound-note">Original reporting by {escape(a['source'])}. This page is a summary. The full story, photos, and details live at the link above.</p>'''
    title = f"{a['title']} | UNO Entertainment"
    canonical = f"{SITE_URL}/articles/{a['slug']}/"
    description = a.get("excerpt") or SITE_DESCRIPTION
    # Link this article to any topic hub(s) its title/excerpt matches -- the
    # main reason topic hubs get any inbound links at all beyond /topics/,
    # and it points the reader at more coverage on the same person/story
    # right where they're already reading about them.
    matched_topics = [
        (slug, TOPIC_LABELS[slug]) for slug in article_topic_slugs(a) if topic_articles(slug)
    ]
    related_topics_html = ""
    if matched_topics:
        links = " &middot; ".join(
            f'<a href="/topic/{slug}/">{escape(name)}</a>' for slug, name in matched_topics
        )
        related_topics_html = f'<p class="article-related-topics">More on: {links}</p>'
    # "feature" layout -- opt-in via a["layout"] == "feature" -- swaps the
    # standard capped-height article-wrap for a full-bleed hero with the
    # kicker/headline/byline overlaid on the image, magazine-style. See the
    # .feature-* CSS block for the visual design. Every other article page
    # (the other 3500+) never sets "layout", so they're unaffected.
    if a.get("layout") == "feature":
        word_count = len(re.sub(r"<[^>]+>", " ", body_html).split())
        read_mins = max(1, round(word_count / 200))
        dot = '<span class="dot">&middot;</span>'
        # Default byline for hand-authored UNO Ent originals -- all of
        # today's feature-layout content is written by Figure Infinite for
        # UNO Ent Media; a["byline"] can still override this per-article.
        byline = a.get("byline") or (
            f"<strong>By Figure Infinite</strong> for UNO Ent Media {dot} "
            f"{escape(time_ago(a['date']))} {dot} {read_mins} min read"
        )
        kicker = escape(a.get("kicker") or CATEGORY_LABELS.get(a.get("category"), "Feature"))
        if thumb:
            feature_hero = f'''<div class="feature-hero">
  <div class="feature-topbar">
    <a class="back-link" href="/">&larr; Back to UNO Entertainment</a>
  </div>
  <img class="feature-hero-img" src="{escape(thumb)}" alt="{escape(a['title'])}">
  <div class="feature-hero-overlay">
    <div class="feature-hero-inner">
      <span class="feature-kicker">{kicker}</span>
      <h1 class="feature-title">{escape(a['title'])}</h1>
      <div class="feature-byline">{byline}</div>
    </div>
  </div>
</div>'''
        else:
            feature_hero = f'''<div class="feature-topbar" style="position:static;padding:24px 5vw 0;">
  <a class="back-link" href="/" style="color:var(--text);background:none;border:1px solid var(--border);">&larr; Back to UNO Entertainment</a>
</div>
<h1 class="feature-title" style="color:var(--text);padding:16px 5vw 0;text-shadow:none;">{escape(a['title'])}</h1>'''
        gallery = a.get("gallery") or []
        gallery_html = ""
        if gallery:
            imgs = "".join(
                f'<img src="{escape(src)}" alt="{escape(a["title"])}" loading="lazy">'
                for src in gallery
            )
            gallery_html = f'''<div class="feature-gallery-section">
  <span class="feature-section-label">In Frame</span>
  <div class="feature-gallery">{imgs}</div>
</div>'''
        # Allow the article's body_html to place the gallery mid-content via
        # a "{{GALLERY}}" marker (e.g. right before a "Booking & Contact"
        # section) instead of always tacking it on at the very end.
        if gallery_html and "{{GALLERY}}" in body_html:
            body_html = body_html.replace("{{GALLERY}}", gallery_html)
            gallery_html = ""
        content_html = f'''{feature_hero}
<div class="feature-wrap">
  {body_html}
  {gallery_html}
  {related_topics_html}
  {disqus_html(canonical, a['slug'])}
</div>'''
    else:
        content_html = f'''<div class="article-wrap">
  <a class="back-link" href="/">&larr; Back to UNO Entertainment</a>
  <div class="article-meta">{escape(time_ago(a['date']))}</div>
  <h1 class="article-title">{escape(a['title'])}</h1>
  {hero_html}
  {body_html}
  {related_topics_html}
  {disqus_html(canonical, a['slug'])}
</div>'''
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
{GTM_HEAD_SNIPPET}
{THEME_INIT_SNIPPET}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
{meta_html(prefix, title, description, canonical, image_url=thumb)}
{article_jsonld(a, canonical, description)}
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
{GTM_BODY_SNIPPET}
{header_html(prefix, a.get("category"))}
{content_html}
{footer_html(prefix)}
</body>
</html>
"""
    import os
    out_dir = f"articles/{a['slug']}"
    os.makedirs(out_dir, exist_ok=True)
    with open(f"{out_dir}/index.html", "w") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# Legal pages — Privacy Policy and Terms of Service, plus the cookie banner
# above. UNO Ent doesn't run analytics, ads, or affiliate links yet, so
# these are written for where the Site is today (no tracking) while leaving
# room for that to change later without a rewrite. Not a substitute for a
# lawyer's review — just a reasonable baseline to have live.
# ---------------------------------------------------------------------------

_legal_dt = datetime.now(timezone.utc)
LEGAL_EFFECTIVE_DATE = f"{_legal_dt.strftime('%B')} {_legal_dt.day}, {_legal_dt.year}"

PRIVACY_POLICY_BODY = """
<h2>1. Overview</h2>
<p>UNO Entertainment ("UNO Ent," "we," "us," or "our") operates unoent.com (the "Site"). This Privacy
Policy explains what information we collect, how we use it, and the choices you have. By using the Site,
you agree to the practices described here.</p>

<h2>2. Information We Collect</h2>
<p>You don't need an account to read UNO Ent, and we don't collect personal information unless you choose
to give it to us. That includes information you provide directly, such as your email address if you contact
us at <a href="mailto:support@unoent.com">support@unoent.com</a> with a question or story tip, and standard
technical information collected automatically by our hosting provider — like IP address, browser type, and
pages visited — used for basic site performance and security.</p>

<h2>3. Cookies</h2>
<p>UNO Ent doesn't currently use cookies to track visitors or personalize advertising. As the Site grows, we
may introduce cookies for purposes like traffic analytics (for example, Google Analytics) or advertising. If
we do, this policy will be updated and we'll ask for your consent through a cookie banner before any
non-essential cookie is set. You can control or delete cookies at any time through your browser settings.</p>

<h2>4. Third-Party Links</h2>
<p>UNO Ent publishes original summaries of hip-hop news with links to the original reporting from outlets
such as XXL, HotNewHipHop, and The Source. When you click through to one of these sites, you're subject to
their own privacy practices, which we don't control and aren't responsible for. We encourage you to review
the privacy policy of any site you visit.</p>

<h2>5. Children's Privacy</h2>
<p>UNO Ent isn't directed at children under 13, and we don't knowingly collect personal information from
children under 13. If you believe a child has provided us with personal information, contact us at
<a href="mailto:support@unoent.com">support@unoent.com</a> and we'll remove it.</p>

<h2>6. Changes to This Policy</h2>
<p>We may update this Privacy Policy from time to time as the Site evolves. The effective date above
reflects the most recent revision. Continued use of the Site after changes take effect means you accept the
updated policy.</p>

<h2>7. Contact Us</h2>
<p>Questions about this Privacy Policy? Email us at <a href="mailto:support@unoent.com">support@unoent.com</a>.</p>
"""

TERMS_BODY = """
<h2>1. Acceptance of Terms</h2>
<p>By accessing or using unoent.com (the "Site"), operated by UNO Entertainment ("UNO Ent," "we," "us," or
"our"), you agree to be bound by these Terms of Service. If you don't agree, please don't use the Site.</p>

<h2>2. What UNO Ent Is</h2>
<p>UNO Ent curates and summarizes hip-hop and culture news. Each story on the Site is our own original
summary paired with a link to the original reporting from the outlet that broke it — sources like XXL,
HotNewHipHop, and The Source. Full credit for original reporting, photography, and video belongs to those
outlets; UNO Ent's role is curation and commentary, not a substitute for the source.</p>

<h2>3. Intellectual Property</h2>
<p>The summaries, commentary, design, and branding on UNO Ent are our own and may not be reproduced without
permission. Thumbnails, photos, and embedded content sourced from third-party outlets remain the property of
their respective owners and are used here for commentary and news-reporting purposes with attribution and a
link back to the original.</p>

<h2>4. Third-Party Content and Links</h2>
<p>The Site links to third-party websites we don't own or control. We're not responsible for the content,
accuracy, or practices of those sites, and linking to them doesn't mean we endorse them. Visit third-party
sites at your own discretion.</p>

<h2>5. Acceptable Use</h2>
<p>You agree not to use the Site to violate any law, attempt to disrupt or gain unauthorized access to the
Site or its infrastructure, or scrape or republish UNO Ent's original content at scale without permission.</p>

<h2>6. Disclaimer of Warranties</h2>
<p>The Site and its content are provided "as is," without warranties of any kind. We work to keep
information accurate and current, but hip-hop news moves fast — we don't guarantee the Site will always be
error-free, complete, or up to date.</p>

<h2>7. Limitation of Liability</h2>
<p>To the fullest extent permitted by law, UNO Ent isn't liable for any indirect, incidental, or
consequential damages arising from your use of, or inability to use, the Site.</p>

<h2>8. Changes to These Terms</h2>
<p>We may revise these Terms as the Site evolves. The effective date above reflects the most recent
revision. Continued use of the Site after changes take effect means you accept the updated Terms.</p>

<h2>9. Contact Us</h2>
<p>Questions about these Terms? Email us at <a href="mailto:support@unoent.com">support@unoent.com</a>.</p>
"""

SEARCH_PAGE_JS = """
(function () {
  var params = new URLSearchParams(window.location.search);
  var q = (params.get("q") || "").trim();

  document.querySelectorAll(".search-input").forEach(function (el) {
    el.value = q;
  });

  var statusEl = document.getElementById("search-status");
  var resultsEl = document.getElementById("search-results");
  var loadMoreBtn = document.getElementById("search-load-more");

  var CATEGORY_LABELS = {
    news: "News", rumors: "Rumors", videos: "Videos",
    music: "Music", sports: "Sports", opinion: "Opinion"
  };
  var PAGE_SIZE = 24;
  var shown = 0;
  var matches = [];

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function timeAgo(iso) {
    var then = new Date(iso).getTime();
    if (isNaN(then)) return "";
    var diffSec = Math.max(0, (Date.now() - then) / 1000);
    if (diffSec < 60) return "Just now";
    if (diffSec < 3600) return Math.floor(diffSec / 60) + "m ago";
    if (diffSec < 86400) return Math.floor(diffSec / 3600) + "h ago";
    var days = Math.floor(diffSec / 86400);
    if (days < 30) return days + "d ago";
    var months = Math.floor(days / 30);
    if (months < 12) return months + "mo ago";
    return Math.floor(months / 12) + "y ago";
  }

  function cardHtml(a) {
    var thumb = a.thumbnail
      ? '<img src="' + escapeHtml(a.thumbnail) + '" alt="' + escapeHtml(a.title) + '" loading="lazy" class="card-thumb" onload="this.style.animation=\'none\'">'
      : '<img src="/uno-logo.png" alt="UNO Entertainment" loading="lazy" class="card-thumb card-thumb-fallback">';
    var catLabel = CATEGORY_LABELS[a.category];
    var catHtml = catLabel
      ? '<span class="card-category">' + catLabel + '</span><span class="card-dot">&middot;</span>'
      : "";
    return (
      '<a class="card" href="/articles/' + a.slug + '/">' +
      thumb +
      '<div class="card-body">' +
      '<div class="card-meta">' + catHtml + timeAgo(a.date) + "</div>" +
      '<h2 class="card-title">' + escapeHtml(a.title) + "</h2>" +
      '<p class="card-excerpt">' + escapeHtml(a.excerpt) + "</p>" +
      '<span class="card-link">Read More &rarr;</span>' +
      "</div>" +
      "</a>"
    );
  }

  function scoreArticle(a, tokens) {
    var title = (a.title || "").toLowerCase();
    var excerpt = (a.excerpt || "").toLowerCase();
    var source = (a.source || "").toLowerCase();
    var s = 0;
    for (var i = 0; i < tokens.length; i++) {
      var t = tokens[i];
      if (!t) continue;
      if (title.indexOf(t) !== -1) s += 3;
      if (excerpt.indexOf(t) !== -1) s += 1;
      if (source.indexOf(t) !== -1) s += 1;
    }
    return s;
  }

  function renderMore() {
    var next = matches.slice(shown, shown + PAGE_SIZE);
    resultsEl.insertAdjacentHTML("beforeend", next.map(cardHtml).join(""));
    shown += next.length;
    if (loadMoreBtn) loadMoreBtn.hidden = shown >= matches.length;
  }

  function runSearch(index) {
    if (!q) {
      statusEl.textContent = "Type a keyword above to search UNO Entertainment's archive.";
      return;
    }
    var tokens = q.toLowerCase().split(/\s+/).filter(Boolean);
    var scored = [];
    for (var i = 0; i < index.length; i++) {
      var s = scoreArticle(index[i], tokens);
      if (s > 0) scored.push([s, index[i]]);
    }
    scored.sort(function (a, b) {
      if (b[0] !== a[0]) return b[0] - a[0];
      return new Date(b[1].date) - new Date(a[1].date);
    });
    matches = scored.map(function (pair) { return pair[1]; });

    if (!matches.length) {
      statusEl.textContent = 'No results for “' + q + '”. Try a different keyword.';
      return;
    }
    statusEl.textContent =
      matches.length + (matches.length === 1 ? " result" : " results") +
      ' for “' + q + '”';
    renderMore();
  }

  if (loadMoreBtn) loadMoreBtn.addEventListener("click", renderMore);

  fetch("/search-index.json")
    .then(function (r) { return r.json(); })
    .then(runSearch)
    .catch(function () {
      statusEl.textContent = "Search is temporarily unavailable — please try again shortly.";
    });
})();
"""


def build_search_index():
    """Trimmed, JS-friendly index consumed by search/index.html's client-side
    script. Deliberately excludes fields the search results don't render
    (full summary, outbound link) to keep this file as small as possible --
    it's fetched by every visitor who searches, unlike articles.json which
    nothing in the browser ever loads directly. No indent -- this is a
    machine-read asset, not something anyone needs to read on disk."""
    index = [
        {
            "title": a["title"],
            "excerpt": a.get("excerpt", ""),
            "slug": a["slug"],
            "category": a.get("category"),
            "thumbnail": a.get("thumbnail"),
            "date": a["date"],
            "source": a.get("source", ""),
        }
        for a in ARTICLES
    ]
    with open("search-index.json", "w") as f:
        json.dump(index, f, separators=(",", ":"))

def build_search_page():
    """/search/ -- a static shell with no server-rendered results. All
    matching happens client-side in SEARCH_PAGE_JS against
    search-index.json, since this is a static site with no backend to run a
    real query against. Keeps the same header/pagination-adjacent search
    bars working as plain GET forms (add /search/?q=... to the URL) even
    with JS disabled; only the results themselves require JS to render."""
    import os

    prefix = "../"
    title = "Search | UNO Entertainment"
    canonical = f"{SITE_URL}/search/"
    description = "Search UNO Entertainment's archive of hip-hop news, rumors, music, videos, sports, and opinion."
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
{GTM_HEAD_SNIPPET}
{THEME_INIT_SNIPPET}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
{meta_html(prefix, title, description, canonical)}
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
{GTM_BODY_SNIPPET}
{header_html(prefix)}
<main>
  <p id="search-status" class="search-status">Loading&hellip;</p>
  <div id="search-results" class="grid"></div>
  <button id="search-load-more" type="button" class="search-load-more" hidden>Show more results</button>
</main>
{footer_html(prefix)}
<script>
{SEARCH_PAGE_JS}
</script>
</body>
</html>
"""
    os.makedirs("search", exist_ok=True)
    with open("search/index.html", "w") as f:
        f.write(html)

ABOUT_BODY = """
  <p>UNO Entertainment is The Culture's Feed. Hip-hop news, new music, beef, and the stories people actually talk about, pulled into one place.</p>
  <p>We're based in Los Angeles. Every story on this site is a short original summary with a link to the outlet that reported it. XXL, HotNewHipHop, The Source, and the rest of the desk get the credit.</p>
  <h2>What you'll find</h2>
  <p>The feed. Artist <a href="/topics/">topic hubs</a>. And the <a href="/hip-hop-beef-tracker/">Hip-Hop Beef Tracker</a>, where we keep score on the storylines that are still live.</p>
  <h2>Story tips</h2>
  <p><a href="mailto:support@unoent.com">support@unoent.com</a></p>
"""


def build_about_page():
    """/about/ -- who UNO Entertainment is. Same chrome as legal pages, with
    an About-active header pill and a location line instead of an effective date."""
    prefix = "../"
    title = "About UNO Entertainment"
    full_title = f"{title} | UNO Entertainment"
    canonical = f"{SITE_URL}/about/"
    description = "UNO Entertainment is The Culture's Feed. Hip-hop and culture news from Los Angeles."
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
{GTM_HEAD_SNIPPET}
{THEME_INIT_SNIPPET}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{full_title}</title>
{meta_html(prefix, full_title, description, canonical)}
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
{GTM_BODY_SNIPPET}
{header_html(prefix, "about")}
<div class="legal-wrap">
  <h1>{title}</h1>
  <p class="legal-updated">The Culture's Feed &middot; Los Angeles</p>
  {ABOUT_BODY}
</div>
{footer_html(prefix)}
</body>
</html>
"""
    import os
    os.makedirs("about", exist_ok=True)
    with open("about/index.html", "w") as f:
        f.write(html)


def build_legal_page(slug: str, title: str, body_html: str):
    """slug is a URL slug like 'privacy-policy' or 'terms', not a filename --
    this writes {slug}/index.html so the page is reachable at /{slug}/ with
    no .html in the address bar."""
    prefix = "../"
    full_title = f"{title} | UNO Entertainment"
    canonical = f"{SITE_URL}/{slug}/"
    description = f"{title} for UNO Entertainment."
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
{GTM_HEAD_SNIPPET}
{THEME_INIT_SNIPPET}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{full_title}</title>
{meta_html(prefix, full_title, description, canonical)}
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
{GTM_BODY_SNIPPET}
{header_html(prefix)}
<div class="legal-wrap">
  <h1>{title}</h1>
  <p class="legal-updated">Effective {LEGAL_EFFECTIVE_DATE}</p>
  {body_html}
</div>
{footer_html(prefix)}
</body>
</html>
"""
    import os
    os.makedirs(slug, exist_ok=True)
    with open(f"{slug}/index.html", "w") as f:
        f.write(html)


def prune_stale_article_pages():
    """
    Removes articles/<slug>/ directories left over from articles that are no
    longer in articles.json (e.g. dropped by check_links.py for a confirmed
    dead source link). build_article() only ever writes/overwrites pages for
    articles currently in ARTICLES -- it never deletes anything -- so without
    this, a removed article's page stays live and reachable at its old URL
    forever, even after it's gone from every listing on the site. Confirmed
    on the HotNewHipHop Vini Jr/Jay-Z sneaker post: removed from
    articles.json, but articles/vini-jr-.../index.html kept serving fine.
    """
    import os
    import shutil
    if not os.path.isdir("articles"):
        return
    current_slugs = {a["slug"] for a in ARTICLES}
    removed = 0
    for entry in os.listdir("articles"):
        if entry not in current_slugs and os.path.isdir(f"articles/{entry}"):
            shutil.rmtree(f"articles/{entry}")
            removed += 1
    if removed:
        print(f"Pruned {removed} stale article page(s) no longer in articles.json")


def build_robots_txt():
    """robots.txt pointing crawlers at sitemap.xml. No paths are disallowed
    -- everything on the Site is meant to be indexed."""
    content = f"""User-agent: *
Allow: /

Sitemap: {SITE_URL}/sitemap.xml
"""
    with open("robots.txt", "w") as f:
        f.write(content)


def build_sitemap(total_pages: int, live_topics: list, beef_tracker_live: list):
    """XML sitemap covering every clean-URL page the Site generates:
    homepage + pagination, every category (+ its pagination), every topic
    hub (+ its pagination), every article page, the legal pages, /topics/,
    and search. Article <lastmod> uses the article's own published date;
    listing pages use "now" since their content changes on every feed
    refresh. live_topics is the (slug, name, count) list build_topic_hubs()
    already computed -- reused here instead of recomputing which topics are
    actually live."""
    import math

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    urls = []

    for page_num in range(1, total_pages + 1):
        urls.append((SITE_URL + page_href(page_num), now_iso))

    for cat_key, _ in CATEGORIES:
        cat_articles = [a for a in ARTICLES if a.get("category") == cat_key]
        cat_total_pages = max(1, math.ceil(len(cat_articles) / ARTICLES_PER_PAGE))
        for page_num in range(1, cat_total_pages + 1):
            urls.append((f"{SITE_URL}{category_page_href(cat_key, page_num)}", now_iso))

    for slug, _, count in live_topics:
        topic_total_pages = max(1, math.ceil(count / ARTICLES_PER_PAGE))
        for page_num in range(1, topic_total_pages + 1):
            urls.append((f"{SITE_URL}{topic_page_href(slug, page_num)}", now_iso))
    if live_topics:
        urls.append((f"{SITE_URL}/topics/", now_iso))

    if beef_tracker_live:
        urls.append((f"{SITE_URL}/hip-hop-beef-tracker/", now_iso))

    for a in ARTICLES:
        try:
            lastmod = datetime.fromisoformat(a["date"].replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, KeyError):
            lastmod = now_iso
        urls.append((f"{SITE_URL}/articles/{a['slug']}/", lastmod))

    urls.append((f"{SITE_URL}/about/", now_iso))
    urls.append((f"{SITE_URL}/privacy-policy/", now_iso))
    urls.append((f"{SITE_URL}/terms/", now_iso))
    urls.append((f"{SITE_URL}/search/", now_iso))

    entries = "\n".join(
        f"  <url>\n    <loc>{escape(loc)}</loc>\n    <lastmod>{lastmod}</lastmod>\n  </url>"
        for loc, lastmod in urls
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{entries}
</urlset>
"""
    with open("sitemap.xml", "w") as f:
        f.write(xml)
    return len(urls)


def main():
    with open("style.css", "w") as f:
        f.write(STYLE_CSS)
    total_pages = build_pages()
    build_categories()
    live_topics = build_topic_hubs()
    beef_tracker_live = build_beef_tracker()
    prune_stale_article_pages()
    for a in ARTICLES:
        build_article(a)
    build_about_page()
    build_legal_page("privacy-policy", "Privacy Policy", PRIVACY_POLICY_BODY)
    build_legal_page("terms", "Terms of Service", TERMS_BODY)
    check_thumbnails(ARTICLES)
    build_search_index()
    build_search_page()
    build_robots_txt()
    sitemap_url_count = build_sitemap(total_pages, live_topics, beef_tracker_live)
    from collections import Counter
    counts = Counter(a.get("category") for a in ARTICLES)
    cat_summary = ", ".join(f"{label} {counts.get(key, 0)}" for key, label in CATEGORIES)
    print(
        f"Built {total_pages} homepage page(s) ({ARTICLES_PER_PAGE}/page) "
        f"+ {ARTICLE_COUNT} article pages in articles/*/ "
        f"+ category pages ({cat_summary}) "
        f"+ {len(live_topics)} topic hub(s) + /topics/ "
        f"+ /hip-hop-beef-tracker/ ({len(beef_tracker_live)} storylines) "
        f"+ /about/ + /privacy-policy/ + /terms/ "
        f"+ /search/ (search-index.json, {ARTICLE_COUNT} articles) "
        f"+ robots.txt + sitemap.xml ({sitemap_url_count} URLs), plus style.css"
    )


if __name__ == "__main__":
    main()
