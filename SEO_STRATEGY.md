# narve.ai SEO Strategy

> **Goal:** rank #1 on Google for the query `narve` within 90 days, while
> also ranking for `narve.ai`, `prediction market intelligence`, and
> adjacent commercial queries.

## Current benchmarks to beat

- Wikipedia pages for "Narve" (Norwegian given name)
- IMDB/Wikidata entries for people named Narve
- Name-meaning sites (e.g. behindthename.com)

"narve" is a common Scandinavian name, so outranking those pages requires
sustained on-page SEO, brand signals, and authoritative backlinks. The
product domain should win because it's the only destination actively
signalling "narve" as an entity.

## Shipped in this commit

### Technical foundations (done — see code)

- `GET /robots.txt` — auto-generated, apex-aware, explicit allow/disallow
  lists. See `server.py:seo_robots_txt`.
- `GET /sitemap.xml` — auto-generated URL set (`_SITEMAP_ENTRIES`),
  lastmod = today, per-URL changefreq + priority. See
  `server.py:seo_sitemap_xml`.
- `GET /narve` — brand-query landing page (`static/narve-brand.html`).
  URL, title, H1, meta description, Open Graph tags, canonical URL,
  Schema.org Organization JSON-LD all reference "narve".
- Schema.org JSON-LD on homepage (`static/prerelease.html`):
  Organization + WebSite + SoftwareApplication blocks.
- Schema.org + Open Graph + Twitter Card on `static/landing.html`.
- Meta descriptions, canonical URLs, and OG/Twitter tags on every public
  page.

### Not yet shipped — tracking required

- Google Search Console verification (requires manual DNS TXT record)
- Submit sitemap.xml in Search Console
- Bing Webmaster Tools verification
- Per-page content pages:
  - `/about` (1500+ words)
  - `/how-it-works` (2000+ words)
  - `/methodology` (3000+ words)
  - `/faq` with FAQ schema
  - `/team` with Person schema per member
  - `/press` (media mentions)
  - `/changelog` (monthly updates)
- SEO rank tracking cron + `/admin/seo` dashboard

## 90-day execution plan

### Weeks 1-2: Foundation (engineering + admin)

- [x] robots.txt + sitemap.xml shipping from server
- [x] Schema.org markup on homepage + landing + brand page
- [x] Canonical URLs on all public pages
- [x] `/narve` brand page live
- [ ] Verify narve.ai in Google Search Console
- [ ] Submit sitemap in Search Console + request indexing
- [ ] Core Web Vitals: audit Lighthouse score, fix any <90 categories
- [ ] Verify in Bing Webmaster Tools

### Weeks 3-4: Content velocity

Each content page should be 1500+ words minimum, include the word "narve"
multiple times naturally, and use its own canonical URL + description.

- [ ] `/about` — who narve is, what narve does, team
- [ ] `/how-it-works` — end-to-end user journey, with screenshots
- [ ] `/methodology` — technical explanation of the credibility engine
- [ ] `/faq` — common questions; wrap each in FAQ JSON-LD
- [ ] `/changelog` — product updates; post monthly
- [ ] Source profile pages auto-generating for every rated source

### Weeks 5-8: Backlinks

- [ ] Product Hunt launch (DA 91; aim for Tuesday-Thursday)
- [ ] Hacker News "Show HN" post — genuine, technical framing
- [ ] Reddit posts: r/PredictionMarkets, r/Polymarket, r/forecasting,
      r/slatestarcodex
- [ ] Substack cross-promotion (Astral Codex Ten, Silver Bulletin) — offer
      free Pro in exchange for a mention
- [ ] Publish SSRN methodology paper: "Bayesian Credibility Scoring for
      Social Media Prediction Sources"
- [ ] Add narve.ai as a reference citation on relevant Wikipedia pages
      (Polymarket, Prediction market, Kalshi) — never create a narve.ai
      Wikipedia page yourself; it will be deleted.
- [ ] Register public GitHub org @narveai, publish open-source tools
      (browser extension, change queue manager)

### Weeks 9-12: Authority

- [ ] 3 podcast appearances (forecasting / finance / crypto shows)
- [ ] 5 guest posts on finance/data blogs
- [ ] List in 10+ SaaS directories (BetaList, Startupbase, SaaSHub,
      Capterra, G2 stubs)
- [ ] Publish first case study (market that resolved correctly,
      narve signalled it, 500+ words)
- [ ] Secure first real press mention (TechCrunch, The Information,
      Bloomberg, or similar)

## Branded search protection — own the SERP

When someone Googles "narve", we want the whole first page to be us.
Register every "narve" handle (even if unused) so each profile returns
a result we control:

- Twitter/X: @narveai
- Instagram: @narve.ai
- LinkedIn: company/narve-ai
- Facebook: narve.ai
- YouTube: @narveai
- TikTok: @narve.ai
- Reddit: u/narveai, r/narve
- GitHub: @narveai
- Google Business Profile (even as a virtual business)

Each profile should link back to narve.ai and mention "narve" in the
description. Post at minimum once a month so they stay indexed.

## Success metrics

| Week | Target position for "narve" |
| ---- | --------------------------- |
| 4    | Top 10                      |
| 8    | Top 3                       |
| 12   | #1                          |

Also track:
- "narve.ai" (should be #1 within 2 weeks)
- "prediction market intelligence" (competitive)
- "polymarket analytics" (very competitive)
- "polymarket credibility" (long-tail, we should own this)

## Ongoing rhythm

- **Weekly:** publish one content piece (blog post, case study, or
  methodology deep-dive).
- **Monthly:** new case study + changelog update.
- **Quarterly:** methodology paper update (if any significant engine change).

## Notes for future work

- Build `/admin/seo` dashboard once SerpAPI or Dataforseo key is procured.
- Add `SEORankTracking` table + ARQ cron that queries positions daily.
- Alert admin via email if any tracked query drops >3 positions week-over-week.
