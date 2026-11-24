#!/usr/bin/env python3
"""Build a static news dashboard from configured RSS feeds."""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import html
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

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
MAX_VISIBLE_PER_SECTION = 10
ATLANTA_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=33.749&longitude=-84.388"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=7"
)
ATLANTA_ALERTS_URL = "https://api.weather.gov/alerts/active?point=33.749,-84.388"
NWS_ALERTS_URL = "https://www.weather.gov/ffc/"
ALERT_SEVERITIES = {"Extreme", "Severe", "Moderate", "Minor", "Unknown"}
WEATHER_CODES = {
    0: ("Clear", "☀️"),
    1: ("Mostly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Cloudy", "☁️"),
    45: ("Fog", "🌫️"),
    48: ("Icy fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Drizzle", "🌦️"),
    55: ("Heavy drizzle", "🌧️"),
    56: ("Freezing drizzle", "🌧️"),
    57: ("Freezing drizzle", "🌧️"),
    61: ("Light rain", "🌧️"),
    63: ("Rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"),
    67: ("Freezing rain", "🌧️"),
    71: ("Light snow", "🌨️"),
    73: ("Snow", "🌨️"),
    75: ("Heavy snow", "🌨️"),
    77: ("Snow grains", "🌨️"),
    80: ("Rain showers", "🌦️"),
    81: ("Rain showers", "🌧️"),
    82: ("Heavy showers", "🌧️"),
    85: ("Snow showers", "🌨️"),
    86: ("Heavy snow showers", "🌨️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm, hail", "⛈️"),
    99: ("Thunderstorm, hail", "⛈️"),
}


@dataclass
class Article:
    title: str
    link: str
    outlet: str
    us_focused: bool
    published: datetime
    summary: str = ""
    image_url: str = ""
    local_news: bool = False

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
    def local_news(self) -> bool:
        return self.articles[0].local_news

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


class _FeedImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        attributes = dict(attrs)
        for name in ("src", "data-src", "data-original"):
            source = attributes.get(name)
            if source:
                self.sources.append(source)


def safe_image_url(value: str, base_url: str = "") -> str:
    try:
        candidate = urljoin(base_url, html.unescape(value).strip())
        parts = urlsplit(candidate)
        if parts.scheme == "https" and parts.hostname and not parts.username and not parts.password:
            return candidate
    except ValueError:
        pass
    return ""


def extract_image_url(entry: dict, feed_url: str) -> str:
    image_suffixes = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")

    def is_image(item: dict, trusted_image_field: bool = False) -> str:
        source = str(item.get("url") or item.get("href") or "")
        candidate = safe_image_url(source, feed_url)
        if not candidate:
            return ""
        mime_type = str(item.get("type", "")).lower().split(";", 1)[0]
        medium = str(item.get("medium", "")).lower()
        path = urlsplit(candidate).path.lower()
        if trusted_image_field or medium == "image" or mime_type in {
            "image/jpeg", "image/png", "image/gif", "image/webp", "image/avif",
        } or path.endswith(image_suffixes):
            return candidate
        return ""

    for field_name, trusted_image_field in (
        ("media_thumbnail", True),
        ("media_content", False),
        ("itunes_image", True),
        ("image", True),
    ):
        values = entry.get(field_name, [])
        if isinstance(values, dict):
            values = [values]
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict):
                image_url = is_image(value, trusted_image_field)
                if image_url:
                    return image_url

    for field_name in ("enclosures", "links"):
        values = entry.get(field_name, [])
        if isinstance(values, dict):
            values = [values]
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            if field_name == "links" and value.get("rel") != "enclosure":
                continue
            image_url = is_image(value)
            if image_url:
                return image_url

    content_values = [str(entry.get("summary", ""))]
    content_values.extend(
        str(content.get("value", ""))
        for content in entry.get("content", [])
        if isinstance(content, dict)
    )
    for content in content_values:
        parser = _FeedImageParser()
        parser.feed(content)
        parser.close()
        for source in parser.sources:
            image_url = safe_image_url(source, feed_url)
            if image_url:
                return image_url
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
            image_url=extract_image_url(entry, feed["url"]),
            local_news=bool(feed.get("local_news", False)),
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
            and article.local_news == story.local_news
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


def group_sections(ranked_stories: list[tuple[Story, str]]) -> tuple[dict[str, list[Story]], list[Story]]:
    sections = {"us": [], "europe": [], "middle_east": []}
    atlanta_stories = []
    for story, region in ranked_stories:
        if story.local_news:
            atlanta_stories.append(story)
        elif region != "other":
            sections[region].append(story)
    return sections, atlanta_stories


def fetch_atlanta_forecast(timeout: int = 8) -> list[dict]:
    request = urllib.request.Request(
        ATLANTA_FORECAST_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())

    daily = payload.get("daily") if isinstance(payload, dict) else None
    fields = (
        "time",
        "weather_code",
        "temperature_2m_max",
        "temperature_2m_min",
        "precipitation_probability_max",
    )
    if not isinstance(daily, dict) or any(
        not isinstance(daily.get(key), list) or len(daily[key]) != 7
        for key in fields
    ):
        raise ValueError("Weather service returned an invalid 7-day forecast.")

    utc_offset = payload.get("utc_offset_seconds", -18_000)
    try:
        utc_offset = int(utc_offset)
    except (TypeError, ValueError):
        utc_offset = -18_000
    atlanta_today = (
        datetime.now(timezone.utc) + timedelta(seconds=utc_offset)
    ).date()
    forecast = []
    for day, code, high, low, precipitation in zip(*(daily[key] for key in fields)):
        try:
            parsed_day = datetime.strptime(day, "%Y-%m-%d")
            code = int(code)
            high = float(high)
            low = float(low)
            precipitation = (
                int(precipitation) if precipitation is not None else None
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Weather service returned invalid forecast values.") from exc
        if (
            not math.isfinite(high)
            or not math.isfinite(low)
            or (
                precipitation is not None
                and not 0 <= precipitation <= 100
            )
        ):
            raise ValueError("Weather service returned out-of-range forecast values.")
        condition, icon = WEATHER_CODES.get(code, ("Conditions unavailable", "•"))
        date_label = f"{parsed_day.strftime('%a, %b')} {parsed_day.day}"
        if parsed_day.date() == atlanta_today:
            date_label = f"Today · {date_label}"
        forecast.append({
            "date": date_label,
            "condition": condition,
            "icon": icon,
            "high": round(high),
            "low": round(low),
            "precipitation": precipitation,
        })
    return forecast


def fetch_atlanta_alerts(timeout: int = 8) -> list[dict]:
    request = urllib.request.Request(
        ATLANTA_ALERTS_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())

    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        raise ValueError("Weather service returned an invalid alerts response.")

    alerts = []
    for feature in features:
        properties = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(properties, dict):
            raise ValueError("Weather service returned an invalid alert.")
        event = properties.get("event")
        headline = properties.get("headline") or event
        if not isinstance(headline, str) or not headline.strip():
            continue
        severity = properties.get("severity")
        if not isinstance(severity, str) or severity not in ALERT_SEVERITIES:
            severity = "Unknown"
        area = properties.get("areaDesc")
        raw_url = feature.get("id")
        alerts.append({
            "event": event[:120] if isinstance(event, str) else "Weather alert",
            "headline": headline.strip()[:240],
            "severity": severity,
            "area": area[:200] if isinstance(area, str) else "",
            "url": safe_link(raw_url) if isinstance(raw_url, str) else "",
        })
    return alerts


def render_card(story: Story) -> str:
    article = story.latest
    title = html.escape(article.title)
    link = safe_link(article.link)
    linked_title = f'<a href="{html.escape(link, quote=True)}">{title}</a>' if link else title
    image = (
        f'<img class="story-image" src="{html.escape(article.image_url, quote=True)}" alt="" '
        'loading="lazy" decoding="async" referrerpolicy="no-referrer">'
        if safe_image_url(article.image_url) else ""
    )
    sources = []
    for source in sorted(story.articles, key=lambda item: item.outlet.lower()):
        outlet = html.escape(source.outlet)
        source_link = safe_link(source.link)
        sources.append(
            f'<a href="{html.escape(source_link, quote=True)}">{outlet}</a>'
            if source_link else outlet
        )
    source_list = " · ".join(sources)
    summary = html.escape(article.summary) if article.summary else "No summary provided by this feed."
    story_content = (
        f'<h3>{linked_title}</h3>'
        f'<p class="summary">{summary}</p>'
        f'<p class="meta sources"><strong>Sources:</strong> {source_list}</p>'
    )
    story_class = "story story--with-image" if image else "story"
    if image:
        story_content = f'<div class="story-content">{story_content}</div>'
    return (
        f'<article class="{story_class}">'
        f'{image}'
        f'{story_content}'
        '</article>'
    )


def render_section(
    section_id: str,
    title: str,
    description: str,
    stories: list[Story],
) -> str:
    visible = stories[:MAX_VISIBLE_PER_SECTION]
    cards = "\n".join(render_card(story) for story in visible)
    if len(stories) > len(visible):
        more_cards = "\n".join(
            render_card(story)
            for story in stories[len(visible):]
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


def render_ticker(ranked_stories: list[tuple[Story, str]]) -> str:
    latest = sorted(
        {story.latest.title: story for story, _ in ranked_stories}.values(),
        key=lambda story: story.latest.published,
        reverse=True,
    )[:12]
    items = []
    for story in latest:
        article = story.latest
        title = html.escape(article.title)
        link = safe_link(article.link)
        linked_title = (
            f'<a href="{html.escape(link, quote=True)}">{title}</a>'
            if link else title
        )
        published = article.published.strftime("%b %d, %H:%M UTC")
        items.append(
            '<article class="ticker-item">'
            f'<span class="ticker-headline">{linked_title}</span>'
            f'<span class="ticker-source">{html.escape(article.outlet)} · {published}</span>'
            '</article>'
        )
    if not items:
        items.append('<p class="ticker-empty">No recent headlines available.</p>')
    return "\n".join(items)


def render_forecast(forecast: list[dict] | None) -> str:
    if forecast is None:
        return (
            '<section class="forecast" aria-labelledby="forecast-title">'
            '<h2 id="forecast-title">Atlanta 7-day forecast</h2>'
            '<p class="forecast-unavailable">Forecast unavailable right now.</p>'
            '<a class="forecast-credit" href="https://open-meteo.com/" '
            'target="_blank" rel="noopener noreferrer">Weather data by Open-Meteo</a>'
            '</section>'
        )
    days = []
    for day in forecast:
        date = html.escape(day["date"])
        icon = html.escape(day["icon"])
        condition = html.escape(day["condition"])
        high = html.escape(str(day["high"]))
        low = html.escape(str(day["low"]))
        precipitation = (
            html.escape(str(day["precipitation"]))
            if day["precipitation"] is not None else "—"
        )
        days.append(
            '<article class="forecast-day">'
            f'<h3>{date}</h3>'
            f'<span class="forecast-icon" aria-hidden="true">{icon}</span>'
            f'<span class="forecast-condition">{condition}</span>'
            f'<p class="forecast-temperature"><strong>{high}°</strong> / {low}°</p>'
            f'<p class="forecast-rain">Rain {precipitation}{"%" if precipitation != "—" else ""}</p>'
            '</article>'
        )
    return (
        '<section class="forecast" aria-labelledby="forecast-title">'
        '<div class="forecast-heading">'
        '<h2 id="forecast-title">Atlanta 7-day forecast</h2>'
        '<span>Atlanta, Georgia</span>'
        '</div>'
        f'<div class="forecast-grid">{"".join(days)}</div>'
        '<a class="forecast-credit" href="https://open-meteo.com/" '
        'target="_blank" rel="noopener noreferrer">Weather data by Open-Meteo</a>'
        '</section>'
    )


def render_weather_alerts(alerts: list[dict] | None) -> str:
    if alerts is None:
        message = (
            '<p class="weather-alert-status">Alerts temporarily unavailable. '
            'Check the National Weather Service for current information.</p>'
        )
    elif not alerts:
        message = '<p class="weather-alert-status">No active alerts for Atlanta, Georgia.</p>'
    else:
        alert_cards = []
        for alert in alerts:
            severity = alert["severity"]
            severity_class = severity.lower()
            event = html.escape(alert["event"])
            headline = html.escape(alert["headline"])
            area = html.escape(alert["area"])
            details = (
                f'<a href="{html.escape(alert["url"], quote=True)}" '
                'target="_blank" rel="noopener noreferrer">NWS details</a>'
                if alert["url"] else ""
            )
            area_line = f'<p class="weather-alert-area">{area}</p>' if area else ""
            alert_cards.append(
                f'<article class="weather-alert weather-alert--{severity_class}">'
                f'<span class="weather-alert-severity">{html.escape(severity)}</span>'
                f'<h3>{event}</h3>'
                f'<p>{headline}</p>'
                f'{area_line}{details}'
                '</article>'
            )
        message = f'<div class="weather-alert-list">{"".join(alert_cards)}</div>'
    return (
        '<section class="weather-alerts" aria-labelledby="weather-alerts-title">'
        '<div class="weather-alerts-heading">'
        '<h2 id="weather-alerts-title">Atlanta weather alerts</h2>'
        f'<a href="{NWS_ALERTS_URL}" target="_blank" rel="noopener noreferrer">'
        'National Weather Service</a>'
        '</div>'
        f'{message}'
        '</section>'
    )


def render_page(
    ranked_stories: list[tuple[Story, str]],
    updated: datetime,
    live_news_url: str = "",
    source_names: list[str] | None = None,
    forecast: list[dict] | None = None,
    alerts: list[dict] | None = None,
) -> str:
    sections, atlanta_stories = group_sections(ranked_stories)
    if source_names is None:
        source_names = sorted({
            article.outlet
            for story, _ in ranked_stories
            for article in story.articles
        }, key=str.casefold)
    source_names = list(dict.fromkeys(source_names))
    source_list = "\n".join(
        f"<li>{html.escape(name)}</li>" for name in source_names
    )
    source_disclosure = (
        f'<details class="feed-sources"><summary>Sources ({len(source_names)})</summary>'
        f'<ul>{source_list}</ul></details>'
    )
    regional_sections = "\n".join([
        render_section("us", "US", "The most widely covered US stories.", sections["us"]),
        render_section("europe", "Europe", "Major stories from across Europe.", sections["europe"]),
        render_section("middle-east", "Middle East", "Major stories from across the Middle East.", sections["middle_east"]),
    ])
    content = (
        f'<div class="regional-grid">{regional_sections}</div>'
        + render_section(
            "atlanta",
            "Atlanta",
            "Local headlines from Atlanta-area news outlets.",
            atlanta_stories,
        )
    )
    timestamp = updated.strftime("%b %d, %Y at %H:%M UTC")
    ticker_status = "Connecting to live headlines…" if live_news_url else f"Scheduled snapshot · {timestamp}"
    ticker = render_ticker(ranked_stories)
    gas_price_url = (
        live_news_url.rsplit("/latest.json", 1)[0] + "/gas.json"
        if live_news_url.endswith("/latest.json")
        else ""
    )
    gas_status = (
        "Loading weekly estimate…"
        if gas_price_url
        else "Add an EIA API key to the live Worker to enable this estimate."
    )
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
    .header-actions {{ display: flex; flex: 0 0 auto; align-items: center; gap: 8px; }}
    h1 {{ margin: 0; font-size: clamp(1.8rem, 8vw, 2.5rem); letter-spacing: -.045em; }}
    header p {{ margin: 8px 0 0; color: #8b949e; font-size: .92rem; line-height: 1.5; }}
    .feed-sources {{ margin-top: 10px; }}
    .feed-sources summary {{ width: fit-content; color: #58a6ff; font-size: .78rem; font-weight: 650; cursor: pointer; }}
    .feed-sources ul {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 0; padding: 0; list-style: none; }}
    .feed-sources li {{ border: 1px solid #30363d; border-radius: 999px; padding: 4px 8px; background: #161b22; color: #c9d1d9; font-size: .72rem; }}
    .forecast {{ margin: 0 0 18px; border: 1px solid #30363d; border-radius: 13px; padding: 14px; background: #161b22; }}
    .forecast-heading {{ display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 12px; }}
    .forecast-heading h2 {{ margin: 0; font-size: 1rem; }}
    .forecast-heading > span {{ color: #8b949e; font-size: .75rem; }}
    .forecast-grid {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 8px; }}
    .forecast-day {{ display: flex; min-width: 0; flex-direction: column; align-items: center; gap: 5px; border: 1px solid #30363d; border-radius: 10px; padding: 10px 5px; background: #0d1117; text-align: center; }}
    .forecast-day h3, .forecast-temperature, .forecast-rain {{ margin: 0; }}
    .forecast-day h3 {{ font-size: .72rem; }}
    .forecast-icon {{ font-size: 1.25rem; line-height: 1.3; }}
    .forecast-condition {{ min-height: 2em; color: #8b949e; font-size: .65rem; line-height: 1.3; }}
    .forecast-temperature {{ font-size: .72rem; white-space: nowrap; }}
    .forecast-temperature strong {{ color: #e6edf3; }}
    .forecast-rain {{ color: #8b949e; font-size: .65rem; }}
    .forecast-unavailable {{ margin: 0; color: #8b949e; font-size: .8rem; }}
    .forecast-credit {{ display: inline-block; margin-top: 10px; color: #8b949e; font-size: .68rem; text-decoration: none; }}
    .forecast-credit:hover, .forecast-credit:focus-visible {{ color: #58a6ff; text-decoration: underline; }}
    .weather-alerts {{ margin: 0 0 12px; }}
    .weather-alerts-heading {{ display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin: 0 2px 8px; }}
    .weather-alerts-heading h2 {{ margin: 0; font-size: .95rem; }}
    .weather-alerts-heading a, .weather-alert a {{ color: #58a6ff; font-size: .72rem; text-decoration: none; }}
    .weather-alerts-heading a:hover, .weather-alert a:hover {{ text-decoration: underline; }}
    .weather-alert-status {{ margin: 0; border: 1px solid #30363d; border-radius: 9px; padding: 10px 12px; background: #161b22; color: #8b949e; font-size: .78rem; }}
    .weather-alert-list {{ display: grid; gap: 8px; }}
    .weather-alert {{ border: 1px solid #d29922; border-left-width: 4px; border-radius: 10px; padding: 10px 12px; background: #161b22; }}
    .weather-alert--extreme, .weather-alert--severe {{ border-color: #f85149; }}
    .weather-alert--moderate {{ border-color: #d29922; }}
    .weather-alert--minor {{ border-color: #58a6ff; }}
    .weather-alert-severity {{ color: #f0883e; font-size: .67rem; font-weight: 750; text-transform: uppercase; }}
    .weather-alert--extreme .weather-alert-severity, .weather-alert--severe .weather-alert-severity {{ color: #ff7b72; }}
    .weather-alert--minor .weather-alert-severity {{ color: #58a6ff; }}
    .weather-alert h3 {{ margin: 3px 0; font-size: .85rem; }}
    .weather-alert p {{ margin: 4px 0; color: #c9d1d9; font-size: .78rem; line-height: 1.4; }}
    .weather-alert .weather-alert-area {{ color: #8b949e; font-size: .7rem; }}
    .gas-strip {{ margin: 0 0 18px; border: 1px solid #30363d; border-radius: 12px; padding: 12px; background: #161b22; }}
    .gas-heading {{ display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }}
    .gas-heading h2 {{ margin: 0; font-size: 1rem; }}
    .gas-status, .gas-credit, .gas-disclaimer {{ color: #8b949e; font-size: .7rem; }}
    .gas-price {{ margin: 10px 0 4px; color: #e6edf3; font-size: 1.6rem; font-weight: 750; }}
    .gas-credit {{ margin: 5px 0 0; }}
    .gas-credit a {{ color: #8b949e; }}
    .gas-disclaimer {{ margin: 6px 0 0; line-height: 1.45; }}
    .market-strip {{ margin: 0 0 18px; border: 1px solid #30363d; border-radius: 12px; padding: 12px; background: #161b22; }}
    .market-heading {{ display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin: 0 2px 10px; }}
    .market-heading h2 {{ margin: 0; font-size: 1rem; }}
    .market-widget {{ overflow: hidden; min-height: 440px; border: 1px solid #30363d; border-radius: 9px; background: #0d1117; }}
    .market-widget tv-market-overview {{ display: block; min-height: 440px; width: 100%; }}
    .market-attribution, .market-disclaimer {{ color: #8b949e; font-size: .7rem; }}
    .market-attribution {{ margin: 8px 2px 0; }}
    .market-attribution a {{ color: #8b949e; }}
    .market-disclaimer {{ margin: 6px 2px 0; line-height: 1.45; }}
    .refresh-button, .utility-button {{ flex: 0 0 auto; min-height: 44px; border: 1px solid #30363d; border-radius: 999px; padding: 0 14px; background: #21262d; color: #e6edf3; font: inherit; font-size: .82rem; font-weight: 650; cursor: pointer; }}
    .refresh-button:hover, .refresh-button:focus-visible, .utility-button:hover, .utility-button:focus-visible {{ border-color: #58a6ff; outline: none; }}
    .utility-drawer {{ position: fixed; inset: 0 0 0 auto; width: min(440px, 100%); max-width: none; height: 100vh; height: 100dvh; max-height: 100vh; max-height: 100dvh; margin: 0; border: 1px solid #30363d; border-radius: 14px 0 0 14px; padding: 14px; overflow-y: auto; background: #0d1117; color: #e6edf3; }}
    .utility-drawer::backdrop {{ background: #010409b3; backdrop-filter: blur(2px); }}
    .utility-drawer-heading {{ position: sticky; top: -14px; z-index: 1; display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: -14px -14px 14px; border-bottom: 1px solid #30363d; padding: 14px; background: #0d1117; }}
    .utility-drawer-heading h2 {{ margin: 0; font-size: 1.05rem; }}
    .drawer-close {{ min-height: 40px; border: 1px solid #30363d; border-radius: 999px; padding: 0 13px; background: #21262d; color: #e6edf3; font: inherit; font-size: .8rem; font-weight: 650; cursor: pointer; }}
    .drawer-close:hover, .drawer-close:focus-visible {{ border-color: #58a6ff; outline: none; }}
    .section-nav {{ position: sticky; top: 0; z-index: 2; display: flex; gap: 8px; overflow-x: auto; margin: 0 -16px 16px; padding: 10px 16px; background: #0d1117ed; backdrop-filter: blur(12px); scrollbar-width: none; }}
    .section-nav a {{ flex: 0 0 auto; border: 1px solid #30363d; border-radius: 999px; padding: 8px 12px; background: #161b22; color: #e6edf3; font-size: .8rem; font-weight: 650; text-decoration: none; }}
    .section-nav a:hover, .section-nav a:focus-visible {{ border-color: #58a6ff; outline: none; }}
    .news-section {{ --accent: #58a6ff; scroll-margin-top: 70px; margin: 0 0 26px; }}
    .section--europe {{ --accent: #39c5cf; }}
    .section--middle-east {{ --accent: #d29922; }}
    .section--atlanta {{ --accent: #bc8cff; }}
    .story--with-image {{ display: grid; grid-template-columns: clamp(100px, 22%, 180px) minmax(0, 1fr); align-items: start; gap: 0 14px; }}
    .story--with-image .story-image {{ grid-column: 1; width: 100%; height: clamp(80px, 7vw, 112px); aspect-ratio: auto; margin: 0; }}
    .story-content {{ grid-column: 2; min-width: 0; }}
    .latest-strip {{ margin: 0 0 20px; border: 1px solid #30363d; border-radius: 12px; background: #161b22; overflow: hidden; }}
    .latest-header {{ display: flex; align-items: center; gap: 10px; padding: 10px 14px 0; }}
    .latest-label {{ flex: 0 0 auto; color: #ff7b72; font-size: .75rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }}
    .latest-status {{ color: #8b949e; font-size: .72rem; }}
    .ticker-track {{ display: flex; gap: 10px; overflow-x: auto; padding: 10px 14px 14px; scroll-snap-type: x mandatory; scrollbar-color: #30363d transparent; }}
    .ticker-item {{ display: flex; flex: 0 0 min(340px, 82vw); flex-direction: column; justify-content: space-between; gap: 7px; min-height: 78px; border: 1px solid #30363d; border-radius: 9px; padding: 10px 12px; background: #0d1117; scroll-snap-align: start; }}
    .ticker-headline {{ color: #e6edf3; font-size: .84rem; font-weight: 650; line-height: 1.4; }}
    .ticker-headline a {{ color: inherit; text-decoration: none; }}
    .ticker-headline a:hover, .ticker-headline a:focus-visible {{ color: #58a6ff; text-decoration: underline; }}
    .ticker-source {{ color: #8b949e; font-size: .7rem; }}
    .ticker-empty {{ margin: 0; color: #8b949e; font-size: .82rem; }}
    .regional-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); align-items: start; gap: 16px; }}
    .regional-grid .news-section {{ min-width: 0; }}
    .section-heading {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; border-left: 4px solid var(--accent); padding: 3px 4px 3px 12px; margin: 0 0 12px; }}
    .section-heading h2 {{ margin: 0; color: #e6edf3; font-size: 1.35rem; letter-spacing: -.025em; }}
    .section-heading p {{ margin: 4px 0 0; color: #8b949e; font-size: .8rem; line-height: 1.4; }}
    .section-count {{ display: grid; width: 30px; height: 30px; flex: 0 0 30px; place-items: center; border-radius: 50%; background: #21262d; color: var(--accent); font-size: .78rem; font-weight: 750; }}
    .story {{ background: #161b22; border: 1px solid #30363d; border-radius: 13px; padding: 15px; margin: 0 0 9px; box-shadow: 0 2px 8px #0104095c; }}
    .story-image {{ display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: cover; margin: 0 0 12px; border-radius: 8px; background: #0d1117; }}
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
      .forecast-grid {{ grid-auto-flow: column; grid-auto-columns: minmax(88px, 1fr); grid-template-columns: none; overflow-x: auto; padding-bottom: 4px; scrollbar-color: #30363d transparent; }}
      .market-widget, .market-widget tv-market-overview {{ min-height: 410px; }}
      .regional-grid {{ grid-auto-flow: column; grid-auto-columns: minmax(84vw, 1fr); grid-template-columns: none; overflow-x: auto; margin: 0 -16px 22px; padding: 0 16px 10px; scroll-snap-type: x mandatory; scrollbar-color: #30363d transparent; }}
      .regional-grid .news-section {{ scroll-snap-align: start; }}
      .utility-drawer {{ width: min(440px, 100%); border-radius: 0; }}
    }}
    @media (max-width: 360px) {{
      .header-row {{ flex-wrap: wrap; }}
      .header-actions {{ margin-left: auto; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="header-row">
        <h1>Top News</h1>
        <div class="header-actions">
          <button class="utility-button" id="open-utility-drawer" type="button"
            aria-haspopup="dialog" aria-controls="utility-drawer">Tools</button>
          <button class="refresh-button" id="refresh-dashboard" type="button" aria-label="Refresh dashboard">Refresh</button>
        </div>
      </div>
      <p>Top stories from the U.S., Europe, the Middle East, and Atlanta.</p>
      {source_disclosure}
    </header>
    {render_weather_alerts(alerts)}
    <dialog class="utility-drawer" id="utility-drawer" aria-labelledby="utility-title">
      <div class="utility-drawer-heading">
        <h2 id="utility-title">Weather &amp; markets</h2>
        <button class="drawer-close" id="close-utility-drawer" type="button">Close</button>
      </div>
      {render_forecast(forecast)}
      <section class="gas-strip" aria-labelledby="gas-title">
        <div class="gas-heading">
          <h2 id="gas-title">Weekly gas price · Lower Atlantic</h2>
          <span class="gas-status" id="gas-status" aria-live="polite">{html.escape(gas_status)}</span>
        </div>
        <p class="gas-price" id="gas-price" data-live-url="{html.escape(gas_price_url, quote=True)}">
          Configure an EIA API key to load this estimate.
        </p>
        <p class="gas-credit"><a href="https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&amp;s=EMM_EPMRU_PTE_R1Z_DPG&amp;f=W"
          target="_blank" rel="noopener noreferrer">EIA Lower Atlantic price series</a></p>
        <p class="gas-disclaimer">PADD 1C (Lower Atlantic) weekly regular-gasoline retail price; this is a multi-state regional estimate, not Atlanta or Georgia-specific.</p>
      </section>
      <section class="market-strip" aria-labelledby="market-title">
        <div class="market-heading">
          <h2 id="market-title">Market movers</h2>
        </div>
        <div class="market-widget">
          <tv-market-overview mode="market-movers" exchange="US" symbol-sectors="[]"
            time-frame="12M" item-size="normal" assets-type="stocks" chart-size="normal"
            theme="dark">
            <p class="market-disclaimer">Loading U.S. stock movers…</p>
          </tv-market-overview>
        </div>
        <p class="market-attribution"><a href="https://www.tradingview.com/markets/stocks-usa/market-movers-gainers/"
          target="_blank" rel="noopener noreferrer">Market data by TradingView</a></p>
        <p class="market-disclaimer">Data availability and delay vary by exchange. For information only—not investment advice.</p>
      </section>
    </dialog>
    <nav class="section-nav" aria-label="News sections">
      <a href="#us">US</a>
      <a href="#europe">Europe</a>
      <a href="#middle-east">Middle East</a>
      <a href="#atlanta">Atlanta</a>
    </nav>
    <section class="latest-strip" aria-label="Latest headlines">
      <div class="latest-header">
        <span class="latest-label">Latest</span>
        <span class="latest-status" id="latest-status" aria-live="polite">{html.escape(ticker_status)}</span>
      </div>
      <div class="ticker-track" id="latest-items" data-live-url="{html.escape(live_news_url, quote=True)}">
        {ticker}
      </div>
    </section>
    {content}
    <footer>Updated {html.escape(timestamp)} · Summaries come from RSS/Atom feeds; region labels are inferred from headlines.</footer>
  </main>
  <script type="module" src="https://widgets.tradingview-widget.com/w/en/tv-market-overview.js"></script>
  <script>
    const utilityDrawer = document.getElementById("utility-drawer");
    document.getElementById("open-utility-drawer").addEventListener("click", () => utilityDrawer.showModal());
    document.getElementById("close-utility-drawer").addEventListener("click", () => utilityDrawer.close());
    utilityDrawer.addEventListener("click", event => {{
      if (event.target === utilityDrawer) utilityDrawer.close();
    }});

    document.getElementById("refresh-dashboard").addEventListener("click", () => {{
      const url = new URL(window.location.href);
      url.searchParams.set("refresh", Date.now());
      window.location.replace(url.href);
    }});

    const latestItems = document.getElementById("latest-items");
    const latestStatus = document.getElementById("latest-status");
    const liveNewsUrl = latestItems.dataset.liveUrl;
    if (liveNewsUrl) {{
      let isRefreshing = false;
      async function refreshLatestHeadlines() {{
        if (isRefreshing) return;
        isRefreshing = true;
        try {{
          const response = await fetch(liveNewsUrl, {{ cache: "no-store" }});
          if (!response.ok) throw new Error("Live headline request failed.");
          const snapshot = await response.json();
          if (!Array.isArray(snapshot.items)) throw new Error("Invalid live headline response.");
          const fragment = document.createDocumentFragment();
          for (const item of snapshot.items.slice(0, 12)) {{
            if (!item || typeof item.title !== "string" || typeof item.link !== "string") continue;
            let link;
            try {{
              link = new URL(item.link);
              if (!["http:", "https:"].includes(link.protocol)) continue;
            }} catch {{
              continue;
            }}
            const card = document.createElement("article");
            card.className = "ticker-item";
            const headline = document.createElement("span");
            headline.className = "ticker-headline";
            const anchor = document.createElement("a");
            anchor.href = link.href;
            anchor.rel = "noopener noreferrer";
            anchor.textContent = item.title;
            headline.append(anchor);
            const source = document.createElement("span");
            source.className = "ticker-source";
            const published = new Date(item.published_at);
            const publishedText = Number.isNaN(published.getTime())
              ? ""
              : " · " + published.toLocaleTimeString([], {{ hour: "numeric", minute: "2-digit" }});
            source.textContent = (typeof item.outlet === "string" ? item.outlet : "News") + publishedText;
            card.append(headline, source);
            fragment.append(card);
          }}
          if (!fragment.childNodes.length) throw new Error("No usable live headlines.");
          latestItems.replaceChildren(fragment);
          const updated = new Date(snapshot.updated_at);
          latestStatus.textContent = Number.isNaN(updated.getTime())
            ? "Live feed updated"
            : "Live feed updated " + updated.toLocaleTimeString([], {{ hour: "numeric", minute: "2-digit" }});
        }} catch {{
          latestStatus.textContent = "Live feed unavailable · showing scheduled headlines";
        }} finally {{
          isRefreshing = false;
        }}
      }}
      refreshLatestHeadlines();
      window.setInterval(refreshLatestHeadlines, 60_000);
    }}

    const gasPrice = document.getElementById("gas-price");
    const gasStatus = document.getElementById("gas-status");
    const gasPriceUrl = gasPrice.dataset.liveUrl;
    if (gasPriceUrl) {{
      async function refreshGasPrice() {{
        try {{
          const response = await fetch(gasPriceUrl, {{ cache: "no-store" }});
          if (!response.ok) throw new Error("Gas-price request failed.");
          const snapshot = await response.json();
          if (!Number.isFinite(snapshot.price)
            || typeof snapshot.period !== "string"
            || !/^\\d{{4}}-\\d{{2}}-\\d{{2}}$/.test(snapshot.period)) {{
            throw new Error("Invalid gas-price response.");
          }}
          gasPrice.textContent = "$" + snapshot.price.toFixed(3) + " / gallon";
          gasStatus.textContent = "Week of " + new Date(snapshot.period + "T12:00:00")
            .toLocaleDateString([], {{ month: "short", day: "numeric", year: "numeric" }});
        }} catch {{
          gasPrice.textContent = "Weekly estimate unavailable";
          gasStatus.textContent = "EIA data not configured";
        }}
      }}
      refreshGasPrice();
      window.setInterval(refreshGasPrice, 30 * 60_000);
    }}
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
    try:
        forecast = fetch_atlanta_forecast()
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"Warning: Atlanta forecast unavailable: {exc}", file=sys.stderr)
        forecast = None
    try:
        alerts = fetch_atlanta_alerts()
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"Warning: Atlanta weather alerts unavailable: {exc}", file=sys.stderr)
        alerts = None
    output_dir.mkdir(parents=True, exist_ok=True)
    live_news_url = os.environ.get("LIVE_NEWS_URL", "").strip()
    if live_news_url and safe_link(live_news_url) != live_news_url:
        raise ValueError("LIVE_NEWS_URL must be a valid HTTPS URL.")
    if live_news_url and urlsplit(live_news_url).scheme != "https":
        raise ValueError("LIVE_NEWS_URL must use HTTPS.")
    (output_dir / "index.html").write_text(
        render_page(
            ranked,
            now,
            live_news_url,
            [feed["name"] for feed in feeds],
            forecast,
            alerts,
        ),
        encoding="utf-8",
    )
    (output_dir / "feeds.json").write_text(
        json.dumps(
            [{"name": feed["name"], "url": feed["url"]} for feed in feeds],
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
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
