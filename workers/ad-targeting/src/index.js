// UNO Ent -- ad rail targeting Worker
//
// Sits in front of unoent.com (Route: unoent.com/*). For every HTML
// response, it looks at each ad slot marked with a data-campaign
// attribute in the ad rail and decides, per visitor and per request,
// whether that campaign is allowed to show:
//
//   1. Flight window   -- campaign.startDate / campaign.endDate (KV config)
//   2. Geo targeting    -- campaign.countries (KV config, null = everywhere)
//   3. Frequency cap    -- campaign.cap impressions per campaign.capWindowHours,
//                          tracked per visitor via a first-party cookie
//
// If a campaign fails any of those checks, its slot is swapped for the
// "Your Ad Here" house placeholder (same creative dimensions) instead of
// just vanishing -- keeps the rail looking intentional and doubles as a
// sales pitch. Everything else on the page passes through untouched.
//
// Campaign rules live in KV (binding: AD_CAMPAIGNS, key: "campaigns") so
// flight dates / geo / caps can be changed from the Cloudflare dashboard
// without redeploying this script. If KV is unreachable or a campaign is
// missing from the config, this fails OPEN (shows the real ad) rather
// than silently killing paid placements.

const COOKIE_NAME = "uno_ad_freq";
const COOKIE_MAX_AGE = 60 * 60 * 24 * 30; // 30 days -- window resets itself per-campaign

// Placeholder creative + real dimensions for each known ad slot. This is
// physical-layout metadata (which image, what size), not a business rule,
// so it's fine to keep it in code rather than KV.
const SLOT_META = {
  "holly-michelle": {
    placeholderImg: "/images/ad-placeholder-300x250.png",
    width: 300,
    height: 250,
    alt: "Advertise with UNO Ent",
  },
  "coffee-lemon-negra": {
    placeholderImg: "/images/ad-placeholder-300x600.png",
    width: 300,
    height: 600,
    alt: "Advertise with UNO Ent",
  },
};

const CONTACT_HREF =
  "mailto:support@unoent.com?subject=Advertising%20Inquiry%20--%20UNO%20Ent%20Media";

function placeholderHtml(slug) {
  const meta = SLOT_META[slug];
  if (!meta) return null;
  return (
    `<a href="${CONTACT_HREF}" style="display:block">` +
    `<img src="${meta.placeholderImg}" alt="${meta.alt}" width="${meta.width}" height="${meta.height}" loading="lazy">` +
    `</a>`
  );
}

function parseCookies(request) {
  const header = request.headers.get("Cookie") || "";
  const out = {};
  header.split(";").forEach((pair) => {
    const idx = pair.indexOf("=");
    if (idx === -1) return;
    const k = pair.slice(0, idx).trim();
    const v = pair.slice(idx + 1).trim();
    if (k) out[k] = v;
  });
  return out;
}

function readFrequencyState(request) {
  const cookies = parseCookies(request);
  const raw = cookies[COOKIE_NAME];
  if (!raw) return {};
  try {
    return JSON.parse(decodeURIComponent(raw));
  } catch (e) {
    return {};
  }
}

async function loadCampaignConfig(env) {
  try {
    const raw = await env.AD_CAMPAIGNS.get("campaigns");
    if (!raw) return {};
    return JSON.parse(raw);
  } catch (e) {
    // Fail open: no config readable means no restrictions applied anywhere.
    return {};
  }
}

// Decide eligibility for every known slot up front (before the HTML
// stream starts), so the HTMLRewriter handlers below are pure lookups.
function evaluateCampaigns(config, freqState, country) {
  const now = Date.now();
  const decisions = {}; // slug -> { eligible: bool, newFreq: {c, t} | undefined }

  for (const slug of Object.keys(SLOT_META)) {
    const rule = config[slug];

    // No rule for a known slot at all -- fail open, always show.
    if (!rule) {
      decisions[slug] = { eligible: true };
      continue;
    }

    let eligible = true;

    if (rule.startDate && now < Date.parse(rule.startDate)) eligible = false;
    if (rule.endDate && now > Date.parse(rule.endDate)) eligible = false;

    if (
      eligible &&
      Array.isArray(rule.countries) &&
      rule.countries.length > 0 &&
      country &&
      !rule.countries.includes(country)
    ) {
      eligible = false;
    }

    // Frequency cap
    let freq = freqState[slug];
    const capWindowMs = (rule.capWindowHours || 24) * 60 * 60 * 1000;
    if (!freq || typeof freq.t !== "number" || now - freq.t > capWindowMs) {
      freq = { c: 0, t: now };
    }

    if (eligible && typeof rule.cap === "number") {
      if (freq.c >= rule.cap) {
        eligible = false;
      }
    }

    if (eligible) {
      freq = { c: (freq.c || 0) + 1, t: freq.t };
      decisions[slug] = { eligible: true, newFreq: freq };
    } else {
      decisions[slug] = { eligible: false, newFreq: freq };
    }
  }

  return decisions;
}

class CampaignSlotHandler {
  constructor(slug, decision) {
    this.slug = slug;
    this.decision = decision;
  }
  element(element) {
    if (this.decision.eligible) return; // leave the real ad alone
    const html = placeholderHtml(this.slug);
    if (html) element.setInnerContent(html, { html: true });
  }
}

function buildCookieValue(freqState, decisions) {
  const next = { ...freqState };
  for (const slug of Object.keys(decisions)) {
    if (decisions[slug].newFreq) next[slug] = decisions[slug].newFreq;
  }
  return encodeURIComponent(JSON.stringify(next));
}

export default {
  async fetch(request, env, ctx) {
    const response = await fetch(request);

    const contentType = response.headers.get("Content-Type") || "";
    if (!contentType.includes("text/html")) {
      // Not a page -- images, CSS, JSON, etc. pass through untouched.
      return response;
    }

    let config = {};
    try {
      config = await loadCampaignConfig(env);
    } catch (e) {
      config = {};
    }

    const freqState = readFrequencyState(request);
    const country = request.cf && request.cf.country;
    const decisions = evaluateCampaigns(config, freqState, country);

    let rewriter = new HTMLRewriter();
    for (const slug of Object.keys(decisions)) {
      rewriter = rewriter.on(
        `[data-campaign="${slug}"]`,
        new CampaignSlotHandler(slug, decisions[slug])
      );
    }

    const transformed = rewriter.transform(response);

    // Rebuild the response so we can attach the updated frequency cookie
    // and make sure this personalized page is never cached (by the
    // browser or by Cloudflare's edge) and served to a different visitor.
    const out = new Response(transformed.body, transformed);
    out.headers.set("Cache-Control", "private, no-store");
    out.headers.append(
      "Set-Cookie",
      `${COOKIE_NAME}=${buildCookieValue(freqState, decisions)}; Path=/; Max-Age=${COOKIE_MAX_AGE}; SameSite=Lax`
    );

    return out;
  },
};
