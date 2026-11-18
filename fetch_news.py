#!/usr/bin/env python3
"""Build a static, headline-only news dashboard from configured RSS feeds."""

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


@dataclass
class Article:
    title: str
    link: str
    outlet: str
    us_focused: bool
    published: datetime

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
        title = html.unescape(re.sub(r"<[^>]*>", "", str(entry.get("title", "")))).strip()
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


def is_us_related(title: str, keywords: list[str]) -> bool:
    normalized = " ".join(re.findall(r"[a-z0-9]+", title.lower()))
    return any(
        f" {keyword_normalized} " in f" {normalized} "
        for keyword in keywords
        if (keyword_normalized := " ".join(re.findall(r"[a-z0-9]+", keyword.lower())))
    )


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


def rank_stories(stories: list[Story], keywords: list[str], now: datetime | None = None) -> list[tuple[Story, bool]]:
    now = now or datetime.now(timezone.utc)

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
        (story, any(is_us_related(article.title, keywords) for article in story.articles))
        for story in ranked
    ]


def render_page(ranked_stories: list[tuple[Story, bool]], updated: datetime) -> str:
    cards = []
    for story, us_related in ranked_stories[:30]:
        article = story.latest
        title = html.escape(article.title)
        linked_title = f'<a href="{html.escape(article.link, quote=True)}" rel="noopener noreferrer">{title}</a>' if article.link else title
        outlets = ", ".join(sorted(story.outlets))
        us_label = '<span class="badge">US-related</span> ' if us_related else ""
        cards.append(
            '<article class="story">'
            f'<h2>{us_label}{linked_title}</h2>'
            f'<p class="meta">{len(story.outlets)} outlets · {html.escape(outlets)}</p>'
            '</article>'
        )
    empty = '<p class="empty">No recent headlines are available. The next update will try again.</p>'
    content = "\n".join(cards) if cards else empty
    timestamp = updated.strftime("%b %d, %Y at %H:%M UTC")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#101b2b">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="Top News">
  <link rel="manifest" href="manifest.webmanifest">
  <title>Top News</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f3f5f8; color: #172033; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: max(20px, env(safe-area-inset-top)) 16px max(28px, env(safe-area-inset-bottom)); }}
    main {{ max-width: 760px; margin: 0 auto; }}
    header {{ padding: 18px 4px 20px; }}
    h1 {{ margin: 0; font-size: clamp(1.7rem, 7vw, 2.3rem); letter-spacing: -.04em; }}
    header p {{ margin: 8px 0 0; color: #627087; font-size: .9rem; }}
    .story {{ background: white; border: 1px solid #e4e9f0; border-radius: 14px; padding: 16px; margin: 0 0 11px; box-shadow: 0 2px 8px #1720330a; }}
    h2 {{ margin: 0; font-size: 1.05rem; line-height: 1.4; font-weight: 650; }}
    a {{ color: #142c4c; text-decoration: none; }}
    a:active {{ color: #315d92; }}
    .meta {{ margin: 11px 0 0; color: #68768b; font-size: .78rem; line-height: 1.5; }}
    .badge {{ display: inline-block; margin-right: 5px; border-radius: 999px; padding: 2px 7px; color: #175c43; background: #e3f5eb; font-size: .68rem; font-weight: 700; vertical-align: 2px; }}
    .empty {{ background: white; border-radius: 14px; padding: 22px; color: #627087; }}
    footer {{ color: #778398; font-size: .75rem; padding: 12px 4px; }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Top News</h1>
      <p>Headlines appearing across the most outlets, with US-related stories prioritized.</p>
    </header>
    {content}
    <footer>Updated {html.escape(timestamp)} · Headlines only; stories are grouped by title similarity.</footer>
  </main>
</body>
</html>
"""


def build_dashboard(config_path: Path, output_dir: Path) -> int:
    import yaml

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    feeds = config.get("feeds", [])
    keywords = config.get("us_keywords", [])
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
    ranked = rank_stories(cluster_articles(articles), keywords, now)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(render_page(ranked, now), encoding="utf-8")
    manifest = {
        "name": "Top News",
        "short_name": "Top News",
        "start_url": "./",
        "display": "standalone",
        "background_color": "#f3f5f8",
        "theme_color": "#101b2b",
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
