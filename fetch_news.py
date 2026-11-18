#!/usr/bin/env python3
"""Build a static news dashboard from configured RSS feeds."""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import html
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

import feedparser

USER_AGENT = "MorningNews/1.0 (+https://github.com/naokib23/MorningNews)"
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "into", "is", "it", "its", "of", "on", "or", "that", "the",
    "this", "to", "with",
}
MAX_ARTICLES_PER_FEED = 25
MAX_AGE_HOURS = 72
MAX_SUMMARY_CHARS = 220
MIN_OUTLETS_FOR_REGIONAL_SECTIONS = 3
MAX_VISIBLE_PER_SECTION = 10
REGION_NAMES = {
    "us": "US",
    "europe": "Europe",
    "middle_east": "Middle East",
    "other": "Other world",
}


@dataclass
class Article:
    title: str
    link: str
    outlet: str
    us_focused: bool
    published: datetime
    summary: str = ""

    @property
    def tokens(self) -> set[str]:
        return {
            token for token in re.findall(r"[a-z0-9]+", self.title.lower())
            if token not in STOP_WORDS and len(token) > 1
        }


@dataclass
class Story:
    articles: list[Article] = field(default_factory=list)

    @property
    def outlets(self) -> set[str]:
        return {article.outlet for article in self.articles}

    @property
    def latest(self) -> Article:
        return max(self.articles, key=lambda article: article.published)


def parse_date(entry: dict, fallback: datetime) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return fallback
    return datetime.fromtimestamp(calendar.timegm(parsed), timezone.utc)


def safe_link(value: str) -> str:
    try:
        parts = urlsplit(value)
        return value if parts.scheme in {"http", "https"} and parts.netloc else ""
    except ValueError:
        return ""


def clean_feed_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]*>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"(?s)<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_summary(value: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    text = clean_feed_text(value)
    if len(text) <= limit:
        return text
    end = text.rfind(" ", 0, limit - 1)
    if end < limit // 2:
        end = limit - 1
    return text[:end].rstrip(" ,;:-") + "…"


def fetch_feed(feed: dict, timeout: int = 12) -> list[Article]:
    request = urllib.request.Request(
        feed["url"],
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        parsed = feedparser.parse(response.read())

    now = datetime.now(timezone.utc)
    articles = []
    for entry in parsed.entries[:MAX_ARTICLES_PER_FEED]:
        title = clean_feed_text(str(entry.get("title", "")))
        if not title:
            continue
        published = parse_date(entry, now)
        if (now - published).total_seconds() > MAX_AGE_HOURS * 3600:
            continue
        articles.append(Article(
            title=title,
            link=safe_link(str(entry.get("link", ""))),
            outlet=feed["name"],
            us_focused=bool(feed.get("us_focused", False)),
            published=published,
            summary=clean_summary(str(entry.get("summary", ""))),
        ))
    return articles


def similar_titles(left: Article, right: Article) -> bool:
    left_tokens, right_tokens = left.tokens, right.tokens
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    if overlap >= 2 and overlap / min(len(left_tokens), len(right_tokens)) >= 0.8:
        return True
    union = left_tokens | right_tokens
    if overlap >= 3 and overlap / len(union) >= 0.48:
        return True
    return SequenceMatcher(None, " ".join(sorted(left_tokens)), " ".join(sorted(right_tokens))).ratio() >= 0.78


def contains_keywords(title: str, keywords: list[str]) -> bool:
    normalized = " ".join(re.findall(r"[a-z0-9]+", title.lower()))
    return any(
        f" {keyword_normalized} " in f" {normalized} "
        for keyword in keywords
        if (keyword_normalized := " ".join(re.findall(r"[a-z0-9]+", keyword.lower())))
    )


def is_us_related(title: str, keywords: list[str]) -> bool:
    return contains_keywords(title, keywords)


def cluster_articles(articles: list[Article]) -> list[Story]:
    stories: list[Story] = []
    for article in sorted(articles, key=lambda item: item.published, reverse=True):
        matching = [
            story for story in stories
            if article.outlet not in story.outlets
            and any(similar_titles(article, candidate) for candidate in story.articles)
        ]
        if matching:
            matching[0].articles.append(article)
        else:
            stories.append(Story([article]))
    return stories


def classify_region(
    story: Story,
    us_keywords: list[str],
    region_keywords: dict[str, list[str]],
) -> str:
    titles = [article.title for article in story.articles]
    # Explicit European or Middle Eastern references outweigh incidental US mentions.
    for region in ("middle_east", "europe"):
        if any(contains_keywords(title, region_keywords.get(region, [])) for title in titles):
            return region
    if any(is_us_related(title, us_keywords) for title in titles):
        return "us"
    return "other"


def rank_stories(
    stories: list[Story],
    keywords: list[str],
    now: datetime | None = None,
    region_keywords: dict[str, list[str]] | None = None,
) -> list[tuple[Story, str]]:
    now = now or datetime.now(timezone.utc)
    region_keywords = region_keywords or {}

    def score(story: Story) -> tuple[float, int, datetime]:
        latest = story.latest
        us_related = any(is_us_related(article.title, keywords) for article in story.articles)
        age_hours = max(0.0, (now - latest.published).total_seconds() / 3600)
        recency = 0.3 * max(0.0, 1.0 - age_hours / MAX_AGE_HOURS)
        us_outlets = sum(article.us_focused for article in story.articles)
        value = len(story.outlets) + (1.5 if us_related else 0.0) + recency
        if us_related:
            value += min(0.35, us_outlets * 0.05)
        return value, len(story.outlets), latest.published

    ranked = sorted(stories, key=score, reverse=True)
    return [
        (story, classify_region(story, keywords, region_keywords))
        for story in ranked
    ]


def group_sections(ranked_stories: list[tuple[Story, str]]) -> tuple[dict[str, list[Story]], list[tuple[Story, str]]]:
    sections = {"us": [], "europe": [], "middle_east": []}
    worth_a_look = []
    for story, region in ranked_stories:
        if region != "other":
            sections[region].append(story)
        if len(story.outlets) < MIN_OUTLETS_FOR_REGIONAL_SECTIONS or region == "other":
            worth_a_look.append((story, region))
    return sections, worth_a_look


def render_card(story: Story, region: str, small_news: bool = False) -> str:
    article = story.latest
    title = html.escape(article.title)
    linked_title = f'<a href="{html.escape(article.link, quote=True)}" rel="noopener noreferrer">{title}</a>' if article.link else title
    outlets = ", ".join(sorted(story.outlets))
    count = len(story.outlets)
    outlet_label = "outlet" if count == 1 else "outlets"
    badges = ""
    if small_news:
        tags = []
        if region != "other":
            tags.append(REGION_NAMES[region])
        tags.append("Under-covered" if count < MIN_OUTLETS_FOR_REGIONAL_SECTIONS else "Other world")
        badges = "".join(f'<span class="badge">{html.escape(tag)}</span>' for tag in tags)
    summary = html.escape(article.summary) if article.summary else "No summary provided by this feed."
    return (
        '<article class="story">'
        f'<h3>{badges}{linked_title}</h3>'
        f'<p class="summary">{summary}</p>'
        f'<p class="meta">{count} {outlet_label} · {html.escape(outlets)}</p>'
        '</article>'
    )


def render_section(
    section_id: str,
    title: str,
    description: str,
    stories: list[tuple[Story, str]],
    small_news: bool = False,
) -> str:
    visible = stories[:MAX_VISIBLE_PER_SECTION]
    cards = "\n".join(render_card(story, region, small_news) for story, region in visible)
    if len(stories) > len(visible):
        more_cards = "\n".join(
            render_card(story, region, small_news)
            for story, region in stories[len(visible):]
        )
        cards += (
            '<details class="more">'
            f'<summary>Show {len(stories) - len(visible)} more</summary>'
            f'{more_cards}'
            '</details>'
        )
    if not stories:
        cards = '<p class="empty">No recent headlines in this section.</p>'
    return (
        f'<section class="news-section section--{section_id}" id="{section_id}" aria-labelledby="{section_id}-title">'
        '<div class="section-heading">'
        '<div>'
        f'<h2 id="{section_id}-title">{title}</h2>'
        f'<p>{description}</p>'
        '</div>'
        f'<span class="section-count" aria-label="{len(stories)} headlines">{len(stories)}</span>'
        '</div>'
        f'{cards}'
        '</section>'
    )


def render_page(ranked_stories: list[tuple[Story, str]], updated: datetime) -> str:
    sections, worth_a_look = group_sections(ranked_stories)
    regional_sections = "\n".join([
        render_section("us", "US", "The most widely covered US stories.", [(story, "us") for story in sections["us"]]),
        render_section("europe", "Europe", "Major stories from across Europe.", [(story, "europe") for story in sections["europe"]]),
        render_section("middle-east", "Middle East", "Major stories from across the Middle East.", [(story, "middle_east") for story in sections["middle_east"]]),
    ])
    content = (
        f'<div class="regional-grid">{regional_sections}</div>'
        + render_section(
            "other-news",
            "Other News",
            "Less-covered stories from every region, plus notable world headlines.",
            worth_a_look,
            small_news=True,
        )
    )
    timestamp = updated.strftime("%b %d, %Y at %H:%M UTC")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#0d1117">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="Top News">
  <link rel="manifest" href="manifest.webmanifest">
  <title>Top News</title>
  <style>
    :root {{ color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0d1117; color: #e6edf3; scroll-behavior: smooth; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: max(20px, env(safe-area-inset-top)) 16px max(28px, env(safe-area-inset-bottom)); }}
    main {{ max-width: 1200px; margin: 0 auto; }}
    header {{ padding: 18px 4px 14px; }}
    .header-row {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
    h1 {{ margin: 0; font-size: clamp(1.8rem, 8vw, 2.5rem); letter-spacing: -.045em; }}
    header p {{ margin: 8px 0 0; color: #8b949e; font-size: .92rem; line-height: 1.5; }}
    .refresh-button {{ flex: 0 0 auto; min-height: 44px; border: 1px solid #30363d; border-radius: 999px; padding: 0 16px; background: #21262d; color: #e6edf3; font: inherit; font-size: .85rem; font-weight: 650; cursor: pointer; }}
    .refresh-button:hover, .refresh-button:focus-visible {{ border-color: #58a6ff; outline: none; }}
    .section-nav {{ position: sticky; top: 0; z-index: 2; display: flex; gap: 8px; overflow-x: auto; margin: 0 -16px 16px; padding: 10px 16px; background: #0d1117ed; backdrop-filter: blur(12px); scrollbar-width: none; }}
    .section-nav a {{ flex: 0 0 auto; border: 1px solid #30363d; border-radius: 999px; padding: 8px 12px; background: #161b22; color: #e6edf3; font-size: .8rem; font-weight: 650; text-decoration: none; }}
    .section-nav a:hover, .section-nav a:focus-visible {{ border-color: #58a6ff; outline: none; }}
    .news-section {{ --accent: #58a6ff; scroll-margin-top: 70px; margin: 0 0 26px; }}
    .section--europe {{ --accent: #39c5cf; }}
    .section--middle-east {{ --accent: #d29922; }}
    .section--other-news {{ --accent: #bc8cff; }}
    .regional-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); align-items: start; gap: 16px; }}
    .regional-grid .news-section {{ min-width: 0; }}
    .section-heading {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; border-left: 4px solid var(--accent); padding: 3px 4px 3px 12px; margin: 0 0 12px; }}
    .section-heading h2 {{ margin: 0; color: #e6edf3; font-size: 1.35rem; letter-spacing: -.025em; }}
    .section-heading p {{ margin: 4px 0 0; color: #8b949e; font-size: .8rem; line-height: 1.4; }}
    .section-count {{ display: grid; width: 30px; height: 30px; flex: 0 0 30px; place-items: center; border-radius: 50%; background: #21262d; color: var(--accent); font-size: .78rem; font-weight: 750; }}
    .story {{ background: #161b22; border: 1px solid #30363d; border-radius: 13px; padding: 15px; margin: 0 0 9px; box-shadow: 0 2px 8px #0104095c; }}
    .story h3 {{ margin: 0; font-size: 1rem; line-height: 1.45; font-weight: 650; }}
    .story a {{ color: #e6edf3; text-decoration: none; }}
    .story a:active {{ color: var(--accent); }}
    .summary {{ margin: 8px 0 0; color: #c9d1d9; font-size: .88rem; line-height: 1.55; }}
    .meta {{ margin: 9px 0 0; color: #8b949e; font-size: .76rem; line-height: 1.5; }}
    .badge {{ display: inline-block; margin: 0 6px 4px 0; border: 1px solid #30363d; border-radius: 999px; padding: 3px 8px; color: var(--accent); background: #21262d; font-size: .65rem; font-weight: 700; vertical-align: 2px; white-space: nowrap; }}
    .empty {{ background: #161b22; border: 1px solid #30363d; border-radius: 13px; padding: 16px; color: #8b949e; font-size: .85rem; }}
    .more {{ margin: 10px 0 0; }}
    .more summary {{ padding: 10px 4px; color: #58a6ff; font-size: .82rem; font-weight: 700; cursor: pointer; }}
    footer {{ color: #8b949e; font-size: .74rem; line-height: 1.5; padding: 10px 4px; }}
    @media (max-width: 700px) {{
      main {{ max-width: 760px; }}
      .regional-grid {{ grid-auto-flow: column; grid-auto-columns: minmax(84vw, 1fr); grid-template-columns: none; overflow-x: auto; margin: 0 -16px 22px; padding: 0 16px 10px; scroll-snap-type: x mandatory; scrollbar-color: #30363d transparent; }}
      .regional-grid .news-section {{ scroll-snap-align: start; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="header-row">
        <h1>Top News</h1>
        <button class="refresh-button" id="refresh-dashboard" type="button" aria-label="Refresh dashboard">Refresh</button>
      </div>
      <p>Regional headlines ranked by outlet coverage, with brief context from each feed.</p>
    </header>
    <nav class="section-nav" aria-label="News sections">
      <a href="#us">US</a>
      <a href="#europe">Europe</a>
      <a href="#middle-east">Middle East</a>
      <a href="#other-news">Other News</a>
    </nav>
    {content}
    <footer>Updated {html.escape(timestamp)} · Summaries come from RSS/Atom feeds; region labels are inferred from headlines.</footer>
  </main>
  <script>
    document.getElementById("refresh-dashboard").addEventListener("click", () => {{
      const url = new URL(window.location.href);
      url.searchParams.set("refresh", Date.now());
      window.location.replace(url.href);
    }});
  </script>
</body>
</html>
"""


def build_dashboard(config_path: Path, output_dir: Path) -> int:
    import yaml

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    feeds = config.get("feeds", [])
    keywords = config.get("us_keywords", [])
    region_keywords = config.get("region_keywords", {})
    articles: list[Article] = []
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, len(feeds) or 1)) as pool:
        tasks = {pool.submit(fetch_feed, feed): feed for feed in feeds}
        for future in concurrent.futures.as_completed(tasks):
            feed = tasks[future]
            try:
                results = future.result()
                articles.extend(results)
                print(f"{feed['name']}: {len(results)} recent headlines")
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                failures += 1
                print(f"Warning: {feed['name']} feed failed: {exc}", file=sys.stderr)

    if not articles:
        raise RuntimeError("No headlines were fetched; refusing to publish an empty dashboard.")

    now = datetime.now(timezone.utc)
    ranked = rank_stories(cluster_articles(articles), keywords, now, region_keywords)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(render_page(ranked, now), encoding="utf-8")
    manifest = {
        "name": "Top News",
        "short_name": "Top News",
        "start_url": "./",
        "display": "standalone",
        "background_color": "#0d1117",
        "theme_color": "#0d1117",
    }
    (output_dir / "manifest.webmanifest").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(ranked[:30])} ranked stories to {output_dir}; {failures} feeds failed.")
    return len(articles)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("feeds.yaml"))
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    try:
        build_dashboard(args.config, args.output_dir)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
