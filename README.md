# MorningNews

A personal news dashboard with a GitHub-inspired dark theme. It groups similar stories across outlets, shows US, Europe, and Middle East in three columns, and places Atlanta local news below. A Latest ticker sits above the columns; on narrow screens, the regional columns and ticker can be swiped horizontally.

## Set up GitHub Pages

1. **Settings → Pages** should use **GitHub Actions** as the build and deployment source; this repository is already configured that way.
2. The keepalive job explicitly requests `contents: write` for its empty commit; no personal access token or repo-wide permission change is needed.
3. The dashboard workflow deploys on a 30-minute schedule and can also be started with **Actions → Update news dashboard → Run workflow**.
4. Open **Settings → Pages** for the published URL. On an iPhone, open that URL in Safari, tap **Share → Add to Home Screen**, and enable **Open as Web App** if offered. Tap **Refresh** on the dashboard to load the latest published snapshot.

The schedule can start several minutes after a half-hour boundary because GitHub may delay scheduled jobs during high load. The Refresh button reloads the latest published dashboard; it does not start a feed fetch, so new headlines appear after the next scheduled deployment (or a manual Actions run). GitHub disables scheduled workflows in repositories with no activity for 60 days. The monthly keepalive workflow creates an empty commit on the default branch so this repository continues to have activity without adding generated HTML commits; it also has a manual dispatch for verification. The scheduled dashboard uses GitHub-hosted runners and GitHub Pages; the optional live ticker backend requires Cloudflare setup described below.

## Enable the near-live ticker

The ticker initially uses the latest headlines from the scheduled dashboard build. The optional Cloudflare Worker refreshes the RSS/Atom feeds every five minutes and the page checks for updates once a minute. Source publishing and scheduled-job delays mean this is near-live, not instantaneous. Headlines are labeled **Latest** because most feeds do not reliably identify breaking news.

1. In Cloudflare, create a Workers KV namespace and replace `REPLACE_WITH_CLOUDFLARE_KV_NAMESPACE_ID` in `worker/wrangler.toml` with its namespace ID.
2. Create a Cloudflare API token with permission to edit Workers Scripts and Workers KV Storage for the account.
3. In the repository’s **Settings → Secrets and variables → Actions**, add the secrets `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.
4. Run **Actions → Deploy live news worker**. The first scheduled refresh can take up to five minutes.
5. Add the repository Actions variable `LIVE_NEWS_URL` with the deployed worker URL ending in `/latest.json`, then run **Actions → Update news dashboard** so the page connects to it.

Without these Cloudflare settings, the ticker continues to show the latest scheduled snapshot and the rest of the dashboard works normally. The Worker reads the public `feeds.json` generated alongside the dashboard. See [Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/) and [Worker KV](https://developers.cloudflare.com/kv/).

The first Pages deployment may require an approval in the `github-pages` environment, depending on repository settings. A green build alone does not mean Pages is configured or that the site is publicly available.

## Feeds and ranking

`feeds.yaml` holds official RSS/Atom feed URLs, outlet names, US-focus flags, and a marker for Atlanta local feeds. Each feed is fetched concurrently with a 12-second request timeout. One failed source does not stop the update; if all sources fail to provide recent headlines, the job fails instead of deploying a blank dashboard.

The app compares normalized headline words to group likely matches and ranks by distinct outlet count, with a modest US-related and recency boost. Non-local region-matched stories appear in their regional column. Atlanta local stories are grouped only with other Atlanta feeds and shown in their own section. Region keywords in `feeds.yaml` are editable, and European or Middle Eastern matches take precedence over incidental US mentions. Summaries use only the RSS/Atom description field and are capped at 220 characters; some feeds do not provide a description for every item.

Article cards show an image when the RSS/Atom entry includes media metadata, an image enclosure, or an embedded image in its feed content. Images are loaded directly from the publisher with lazy loading; the dashboard does not scrape article pages, and some feeds do not provide images.

The header includes a compact, expandable list of configured outlets. Story headlines link directly to the publisher article, and each card ends with clickable source links for the outlets that covered the story.

The dashboard also shows a seven-day Atlanta, Georgia forecast from Open-Meteo, refreshed with each scheduled or manually triggered dashboard build. If the weather service is unavailable, the rest of the dashboard still deploys and the forecast area displays an unavailable message.

Active weather alerts for Atlanta are shown above the forecast using the National Weather Service API, with severity, affected area, and a link to each alert. The alerts refresh with dashboard builds; when none are active, the page says so, and service outages show a separate unavailable message.

The Atlanta section uses verified local RSS feeds from FOX 5 Atlanta, WSB-TV, 11Alive, and Atlanta News First. The requested Atlanta Voice page and its feed endpoints returned HTTP 403 during validation, so it is omitted rather than scraped; it can be added if a public official RSS/Atom feed becomes available.

The provided outlet list contained 27 outlets. Twenty-one had official RSS/Atom endpoints returning feed XML when checked for this setup. These six were omitted because a usable official feed could not be validated:

| Outlet | Finding |
| --- | --- |
| Pittsburgh Post-Gazette | The supplied `/rss` endpoint returned an HTML page, not an RSS feed. |
| CNN | Known candidate RSS endpoints could not be reached reliably during validation. |
| Associated Press | Candidate official RSS URLs returned HTTP 403. |
| Politico | Candidate official RSS URLs returned HTTP 403. |
| NOTUS | Candidate `/feed` and `/rss.xml` endpoints returned HTTP 404. |
| The Times of London | Candidate RSS endpoints returned HTTP 404. |

Those endpoints can be added to `feeds.yaml` if the publishers expose stable, public RSS/Atom feeds later. The MS NOW entry currently uses NBC News' official general-news feed, since it is the active successor brand.

## Run locally

```sh
python -m pip install -r requirements.txt
python fetch_news.py --output-dir dist
python -m unittest discover -s tests
```

Open `dist/index.html` in a browser to inspect the generated page.
