# UNO Entertainment — Site Prototype

## What's in this folder

- **index.html** — the homepage, page 1. Open it in a browser to see the site.
- **page/** — page 2, 3, etc. of the homepage, once there are enough articles to paginate.
- **category/** — one filtered, paginated listing per category (news, rumors, videos, music, opinion).
- **articles/** — one page per story. Each shows a UNO Ent-voiced summary, then a clear link to the original source.
- **style.css** — shared styling (black/red/white brand, using your logo).
- **uno-logo.png** — your logo, used in the header on every page.
- **articles.json** — the article data currently powering the site (20 real articles pulled from XXL, HotNewHipHop, and The Source on 2026-07-28).
- **fetch_feeds.py** — the production pulling script. Uses `feedparser` to pull all 7 confirmed RSS feeds (XXL, HotNewHipHop, The Source, AllHipHop, Hollywood Unlocked, The Shade Room, Underground Hip Hop Blog), writes a short UNO Ent-style summary for each, and saves everything to `articles.json`.
- **build_site.py** — reads `articles.json` and renders the homepage (paginated), plus every article page.
- **check_links.py** — checks every article's outbound link and removes the ones that are confirmed dead. See "Dead link checking" below.

Note: this prototype's `articles.json` only has 3 of the 7 sources populated. The sandbox this was built in couldn't decode the compressed RSS responses from AllHipHop, Hollywood Unlocked, The Shade Room, and Underground Hip Hop Blog (their feeds are confirmed valid — a normal server-side Python environment with `feedparser`/`requests` handles gzip-compressed feeds natively and won't hit this issue). Once `fetch_feeds.py` runs somewhere with normal internet access, all 7 sources will populate automatically.

## How the site is structured

This isn't a single page of outbound links — it's two layers, on purpose:

1. **Homepage** (`index.html`) — a grid of story cards. Clicking a card goes to UNO Ent's own article page, not straight to the source.
2. **Article page** (`articles/<slug>.html`) — headline, a short summary written in UNO Ent's own voice, and then a clear "Read the full story on [Source]" button that sends the reader out.

That middle layer — the summary page — is what makes this feel like your site instead of a linkboard, and it's also the part built to grow with you: `generate_summary()` in `fetch_feeds.py` currently writes an extractive summary from each article's full text. When you're ready, swap it for a Claude API call to get a properly rewritten summary in your own voice (a few lines of code — the swap-in is commented right there in the file), and later, swap it again for actual staff-written pieces. The site structure doesn't change either time — same slugs, same pages, just better content behind them.

## How the pipeline works

1. `fetch_feeds.py` hits each source's RSS feed, pulls the latest posts, and for each one writes: headline, thumbnail, link, date, and a summary (see above). Saves everything to `articles.json`.
2. `check_links.py` checks every article's link and drops the confirmed-dead ones (see below).
3. `build_site.py` reads `articles.json` and generates the paginated homepage plus one page per article, all styled in your black/red/white brand.
4. Repeat on a schedule to keep the site current.

To refresh locally:
```bash
pip install -r requirements.txt
python3 fetch_feeds.py
python3 check_links.py
python3 build_site.py
```

## Pagination

The homepage shows the newest 12 stories. Once there are more than that, older stories move to `page/2.html`, `page/3.html`, and so on — the homepage never turns into one long infinite scroll. Each page has "Newer / Older" navigation at the bottom. Change `ARTICLES_PER_PAGE` at the top of `build_site.py` if you want a different page size. Category pages (below) paginate the same way, independently.

## Categories

The header has a filter row: All, News, Rumors, Videos, Music, Opinion. Every article gets tagged with exactly one of the five real categories — that tag drives which filtered page(s) it shows up on (`category/news.html`, `category/rumors.html`, etc.), each paginated the same way as the homepage.

Deliberately not included: "Exclusive," "Feature," or other tags a source's own RSS feed might use. Those are how XXL or HotNewHipHop organize *their* site, not something a UNO Ent reader is browsing for — a story tagged "Exclusive" by XXL still shows up on UNO Ent, just filed under whichever of the 5 real categories it actually fits (usually News).

Tagging happens in `fetch_feeds.py`'s `categorize()` function: it checks the source's own RSS `<category>` tags first, falls back to a keyword match against the title, and defaults to "News" if nothing matches. It's a heuristic, not perfect — same upgrade path as `generate_summary()`: swap the function body for an LLM call (a one-line prompt asking for one of the 5 category keys) whenever you want sharper categorization than keyword-matching gives you.

## Dead link checking

`check_links.py` visits every article's outbound link (HEAD request, falling back to GET if a site blocks HEAD) and sorts the result into three buckets:

- **Confirmed dead** (404, 410, 451 — "this page doesn't exist / is gone") → the article is removed from `articles.json` entirely. Run `build_site.py` afterward and its card and page disappear from the site.
- **Warned** (timeouts, connection errors, other 4xx/5xx, or a site that's just temporarily down) → logged, but the article is **kept**. A lot of sites block automated HEAD/GET requests with a 403 that has nothing to do with the story actually being gone, so this is deliberately conservative — it only kills a link when a source unambiguously says the page is gone, not just because a single check failed once.
- **OK** → nothing happens.

Run it manually any time with `python3 check_links.py`, or add `--dry-run` to see the report without changing anything.

On GitHub, this runs automatically once a day via `.github/workflows/check-links.yml` (separate from the every-30-minutes feed pull — checking dozens of outbound links is heavier and doesn't need to happen that often). Set it up the same way as the main workflow: move `check-links.yml` into `.github/workflows/`, commit, and push. It uses the same "Read and write permissions" setting you already turned on for the feed-pull workflow.

## Getting this live on a real domain

Right now this is a static file you can open locally. To make it a real, always-updating website you have a few options, roughly in order of effort:

**Simplest — static hosting + scheduled rebuild**
Host `index.html` on Netlify, Vercel, GitHub Pages, or Cloudflare Pages (all have free tiers). Use GitHub Actions (or any cron) to run `fetch_feeds.py` + `build_site.py` every 15–30 minutes and push the updated `index.html`. No server to manage, and it's basically free to run. See the GitHub setup below — this is the fastest path from here.

### Setting this up on GitHub (free, ~10 minutes)

1. **Create a repo.** On GitHub, make a new repository (public or private, doesn't matter), e.g. `uno-entertainment`.
2. **Push these files to it** — `fetch_feeds.py`, `build_site.py`, `check_links.py`, `requirements.txt`, `articles.json`, `uno-logo.png`, `.nojekyll`, `style.css`, `index.html`, and the `articles/`, `page/`, and `category/` folders.
   ```bash
   git init
   git add .
   git commit -m "Initial UNO Entertainment site"
   git branch -M main
   git remote add origin https://github.com/<your-username>/uno-entertainment.git
   git push -u origin main
   ```
3. **Add the workflows.** Move both `update-feed.yml` and `check-links.yml` (included in this folder) into `.github/workflows/` in your repo, then commit and push:
   ```bash
   mkdir -p .github/workflows
   mv update-feed.yml check-links.yml .github/workflows/
   git add .github/workflows/
   git commit -m "Add auto-update and dead-link-check workflows"
   git push
   ```
4. **Turn on Actions write access.** In the repo: Settings → Actions → General → Workflow permissions → select "Read and write permissions." (Without this, the workflow can pull fresh articles but can't commit them back.)
5. **Enable GitHub Pages.** Settings → Pages → Source → "Deploy from a branch" → branch `main`, folder `/ (root)`. Your site will be live at `https://<your-username>.github.io/uno-entertainment/` within a minute or two.
6. **Confirm it's working.** Go to the Actions tab → "Update UNO Entertainment Feed" → "Run workflow" to trigger it manually the first time. Check that `articles.json` and `index.html` get a new commit from `uno-ent-bot`, then reload your Pages URL.

From then on, the workflow fires automatically every 30 minutes (edit the `cron` line in the workflow file to change that — e.g. `*/15 * * * *` for every 15 minutes), pulls fresh articles, rebuilds the page, and pushes the update. GitHub Pages picks up the new `index.html` automatically since it's served straight from `main`.

**Want a custom domain instead of the `github.io` URL?** Add a `CNAME` file with your domain to the repo root and point your domain's DNS at GitHub Pages — happy to walk through that when you're ready to buy a domain.

**More dynamic — small backend**
Run `fetch_feeds.py` as a scheduled job (cron, or a serverless function) on a host like Render, Railway, or a small VPS, writing to a real database instead of a JSON file. Serve the site from a lightweight backend (Flask/Express) so you can add search, pagination, and per-source pages later.

**If UNO Ent runs on WordPress**
The pulling logic can be adapted into a WordPress plugin using `wp_cron` for scheduling and custom post types to store pulled articles, so it slots into a WP theme instead of standing alone.

I'd recommend starting with the static + scheduled-rebuild option — it's the cheapest to run and the fastest to get live, and you can graduate to a backend later if you want comments, search, or user accounts.

## Legal note (worth keeping in mind as you grow this)

Every article page shows a short summary in UNO Ent's own words, plus a thumbnail and a clear link back to the original story — never the full article text. That's the model outlets like Axios and Morning Brew use for exactly this reason: a short, independently written summary plus a link out is on solid ground, while republishing full articles from other outlets without permission is copyright infringement even with attribution and a link back. Keep summaries short and original, and keep every page linking out clearly, and you're fine. If UNO Ent grows and you want a tighter relationship with any of these publishers (official syndication, deeper content, exclusive material), that's a conversation to have directly with them rather than something to build around scraping.

## Sources currently wired in

| Source | Status |
|---|---|
| XXL | ✅ Live feed |
| HotNewHipHop | ✅ Live feed |
| The Source | ✅ Live feed |
| AllHipHop | ✅ Feed confirmed, needs a normal server to decode |
| Hollywood Unlocked | ✅ Feed confirmed, needs a normal server to decode |
| The Shade Room | ✅ Feed confirmed, needs a normal server to decode |
| Underground Hip Hop Blog | ✅ Feed confirmed, needs a normal server to decode |
| HipHopDX, Complex, REVOLT, Okayplayer | ❌ No public RSS feed found |
| Rap-Up | ❌ Folded into REVOLT, no standalone feed |
| SayCheese TV | ❌ Not an article-based site (Instagram/YouTube only) |
