# MorningNews

A personal, headlines-only news dashboard that groups similar stories across outlets and ranks stories appearing in more sources first, with a modest boost for headlines identified as US-related. Its output is a static mobile-first page; it does not collect or publish article summaries.

## Set up GitHub Pages

1. **Settings → Pages** should use **GitHub Actions** as the build and deployment source; this repository is already configured that way.
2. The keepalive job explicitly requests `contents: write` for its empty commit; no personal access token or repo-wide permission change is needed.
3. The dashboard workflow deploys on a 30-minute schedule and can also be started with **Actions → Update news dashboard → Run workflow**.
4. Open **Settings → Pages** for the published URL. On an iPhone, open that URL in Safari, tap **Share → Add to Home Screen**, and enable **Open as Web App** if offered.

The schedule can start several minutes after a half-hour boundary because GitHub may delay scheduled jobs during high load. GitHub disables scheduled workflows in repositories with no activity for 60 days. The monthly keepalive workflow creates an empty commit on the default branch so this repository continues to have activity without adding generated HTML commits; it also has a manual dispatch for verification. Scheduled runs use GitHub-hosted runners and GitHub Pages; no server or secrets are required.

The first Pages deployment may require an approval in the `github-pages` environment, depending on repository settings. A green build alone does not mean Pages is configured or that the site is publicly available.

## Feeds and ranking

`feeds.yaml` holds the official RSS/Atom feed URLs, outlet names, and a flag for US-focused outlets. Each feed is fetched concurrently with a 12-second request timeout. One failed source does not stop the update; if all sources fail to provide recent headlines, the job fails instead of deploying a blank dashboard.

The app compares normalized headline words to group likely matches, counts distinct outlets in each group, then gives a small US-related and recency boost. US keywords are editable in `feeds.yaml`. Only headline text, outlet names, and publication links appear on the page.

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
