import json
from unittest.mock import Mock
from types import SimpleNamespace
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fetch_news import (
    Article,
    Story,
    build_dashboard,
    cluster_articles,
    clean_summary,
    extract_image_url,
    group_sections,
    is_us_related,
    rank_stories,
    render_page,
    render_card,
    fetch_atlanta_forecast,
    fetch_atlanta_alerts,
)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)

    def article(self, title, outlet, hours_ago=0, us_focused=False, summary="", local_news=False):
        from datetime import timedelta

        return Article(
            title=title,
            link="https://example.com/story",
            outlet=outlet,
            us_focused=us_focused,
            published=self.now - timedelta(hours=hours_ago),
            summary=summary,
            local_news=local_news,
        )

    def test_clusters_similar_titles_from_distinct_outlets(self):
        stories = cluster_articles([
            self.article("US Senate passes new climate bill", "Outlet A", us_focused=True),
            self.article("U.S. Senate passes climate bill", "Outlet B", us_focused=True),
        ])
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0].outlets, {"Outlet A", "Outlet B"})

    def test_us_related_stories_receive_priority(self):
        keywords = ["United States", "Senate"]
        stories = [
            Story([self.article("Leaders meet for summit", "A"), self.article("Summit gets underway", "B")]),
            Story([self.article("Senate approves major bill", "C", us_focused=True)]),
        ]
        ranked = rank_stories(stories, keywords, self.now)
        self.assertEqual(ranked[0][1], "us")

    def test_html_escapes_untrusted_headlines_and_links(self):
        story = Story([
            self.article('<script>alert("x")</script>', "Outlet", summary='<b>feed summary</b>')
        ])
        page = render_page([(story, "us")], self.now)
        self.assertNotIn('<script>alert("x")</script>', page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;b&gt;feed summary&lt;/b&gt;", page)
        self.assertIn('<p class="summary">', page)
        self.assertIn("color-scheme: dark", page)
        self.assertIn("#0d1117", page)
        self.assertNotIn('class="story-image"', page)
        self.assertIn('id="refresh-dashboard"', page)
        self.assertIn('url.searchParams.set("refresh", Date.now())', page)
        self.assertIn("window.location.replace(url.href)", page)

    def test_latest_ticker_starts_with_snapshot_and_can_poll_live_feed(self):
        story = Story([self.article("A developing story", "Outlet", summary="Details")])
        page = render_page(
            [(story, "us")],
            self.now,
            "https://morning-news-live.example.workers.dev/latest.json",
        )
        self.assertIn('aria-label="Latest headlines"', page)
        self.assertIn('data-live-url="https://morning-news-live.example.workers.dev/latest.json"', page)
        self.assertIn("A developing story", page)
        self.assertIn("window.setInterval(refreshLatestHeadlines, 60_000)", page)
        self.assertIn("Live feed unavailable · showing scheduled headlines", page)
        self.assertIn('href="https://example.com/story"', page)
        self.assertIn("anchor.href = link.href", page)
        self.assertNotIn("storyPageByLink", page)

    def test_market_movers_panel_uses_public_tradingview_widget(self):
        page = render_page(
            [],
            self.now,
        )
        self.assertIn("mode=\"market-movers\"", page)
        self.assertIn("exchange=\"US\"", page)
        self.assertIn("assets-type=\"stocks\"", page)
        self.assertIn(
            'src="https://widgets.tradingview-widget.com/w/en/tv-market-overview.js"',
            page,
        )
        self.assertIn("Market data by TradingView", page)
        self.assertIn(
            "Data availability and delay vary by exchange. For information only—not investment advice.",
            page,
        )

    def test_weather_and_market_panels_are_visible_above_unchanged_news_columns(self):
        page = render_page([], self.now, alerts=[])
        utility_start = page.index('<div class="utility-panel-grid">')
        news_nav_start = page.index('<nav class="section-nav"')

        self.assertNotIn("<dialog", page)
        self.assertNotIn("open-utility-drawer", page)
        self.assertIn('class="forecast"', page[utility_start:news_nav_start])
        self.assertIn('class="gas-strip"', page[utility_start:news_nav_start])
        self.assertIn('class="market-strip"', page[utility_start:news_nav_start])
        self.assertLess(page.index('class="weather-alerts"'), utility_start)
        self.assertLess(utility_start, news_nav_start)
        self.assertIn("utility-summary-grid", page)
        self.assertIn("utility-panel-grid", page)
        self.assertIn(".utility-panel-grid > *, .utility-summary-grid > *", page)
        self.assertIn('class="regional-grid"', page)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", page)

    def test_lower_atlantic_gas_panel_links_to_worker_and_states_coverage(self):
        page = render_page(
            [],
            self.now,
            "https://morning-news-live.example.workers.dev/latest.json",
        )
        self.assertIn(
            'data-live-url="https://morning-news-live.example.workers.dev/gas.json"',
            page,
        )
        self.assertIn("Weekly gas price · Lower Atlantic", page)
        self.assertIn(
            "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&amp;s=EMM_EPMRU_PTE_R1Z_DPG&amp;f=W",
            page,
        )
        self.assertIn("PADD 1C (Lower Atlantic) weekly regular-gasoline retail price", page)
        self.assertIn("not Atlanta or Georgia-specific", page)
        self.assertIn("refreshGasPrice()", page)

    def test_gas_panel_explains_api_key_setup_when_worker_is_not_configured(self):
        page = render_page([], self.now)
        self.assertIn("Add an EIA API key to the live Worker", page)
        self.assertIn('data-live-url=""', page)

    def test_header_lists_configured_sources_with_brief_copy(self):
        page = render_page(
            [],
            self.now,
            source_names=["Washington Post", "The Guardian"],
        )
        self.assertIn("Top stories from the U.S., Europe, the Middle East, and Atlanta.", page)
        self.assertIn("Sources (2)", page)
        self.assertIn("<li>Washington Post</li>", page)
        self.assertIn("<li>The Guardian</li>", page)
        self.assertNotIn("ranked by outlet coverage", page)

    def test_atlanta_forecast_appears_above_the_news_sections(self):
        forecast = [{
            "date": "Today",
            "condition": "Clear",
            "icon": "☀️",
            "high": 70,
            "low": 55,
            "precipitation": 20,
        }]
        page = render_page([], self.now, forecast=forecast)
        self.assertIn("Atlanta 7-day forecast", page)
        self.assertIn("Atlanta, Georgia", page)
        self.assertIn("Rain 20%", page)
        self.assertIn("Weather data by Open-Meteo", page)
        self.assertLess(page.index('class="forecast"'), page.index('class="latest-strip"'))

    def test_forecast_fallback_is_visible_when_weather_is_unavailable(self):
        page = render_page([], self.now)
        self.assertIn("Forecast unavailable right now.", page)
        self.assertIn("https://open-meteo.com/", page)

    def test_weather_alert_panel_shows_clear_no_alert_and_active_states(self):
        no_alerts_page = render_page([], self.now, alerts=[])
        self.assertIn("No active alerts for Atlanta, Georgia.", no_alerts_page)
        alert = {
            "event": "Severe Thunderstorm Warning",
            "headline": "Severe storms with damaging winds",
            "severity": "Severe",
            "area": "Fulton County",
            "url": "https://api.weather.gov/alerts/123",
        }
        active_page = render_page([], self.now, alerts=[alert])
        self.assertIn("weather-alert--severe", active_page)
        self.assertIn("Severe storms with damaging winds", active_page)
        self.assertIn("Fulton County", active_page)
        self.assertIn('href="https://api.weather.gov/alerts/123"', active_page)
        self.assertLess(
            active_page.index('class="weather-alerts"'),
            active_page.index('class="forecast"'),
        )

    def test_weather_alerts_api_parses_active_alerts_and_escapes_content(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({
            "features": [{
                "id": "https://api.weather.gov/alerts/123",
                "properties": {
                    "event": "Severe Thunderstorm Warning",
                    "headline": "Storms & wind <danger>",
                    "severity": "Severe",
                    "areaDesc": "Fulton County",
                },
            }],
        }).encode("utf-8")
        with patch("fetch_news.urllib.request.urlopen", return_value=response) as urlopen:
            alerts = fetch_atlanta_alerts()

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "Severe")
        self.assertEqual(alerts[0]["url"], "https://api.weather.gov/alerts/123")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 8)
        rendered = render_page([], self.now, alerts=alerts)
        self.assertIn("Storms &amp; wind &lt;danger&gt;", rendered)
        self.assertNotIn("Storms & wind <danger>", rendered)

    def test_forecast_api_returns_seven_formatted_days(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({
            "daily": {
                "time": [
                    "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27",
                    "2026-11-28", "2026-11-29", "2026-11-30",
                ],
                "weather_code": [3, 51, 53, 51, 3, 3, 3],
                "temperature_2m_max": [66.5, 59, 49.6, 61.1, 54, 63.5, 67.2],
                "temperature_2m_min": [51.7, 44.8, 43.5, 43.1, 38.7, 44.6, 48],
                "precipitation_probability_max": [15, 4, 82, 82, 1, 1, 13],
            },
        }).encode("utf-8")
        with patch("fetch_news.urllib.request.urlopen", return_value=response) as urlopen:
            forecast = fetch_atlanta_forecast()

        self.assertEqual(len(forecast), 7)
        self.assertIn("Today", forecast[0]["date"])
        self.assertIn("Nov 24", forecast[0]["date"])
        self.assertEqual(forecast[1]["date"], "Wed, Nov 25")
        self.assertEqual(forecast[0]["condition"], "Cloudy")
        self.assertEqual(forecast[0]["high"], 66)
        self.assertEqual(forecast[0]["low"], 52)
        self.assertEqual(forecast[0]["precipitation"], 15)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 8)

    def test_story_card_links_headline_and_each_source_to_publisher(self):
        lead = self.article("Matched headline", "Outlet A", summary="Brief RSS context")
        lead.link = "https://outlet-a.example/story"
        second = self.article("Matched headline", "Outlet B")
        second.link = "https://outlet-b.example/story"
        card = render_card(Story([lead, second]))
        self.assertIn('<h3><a href="https://outlet-a.example/story">Matched headline</a></h3>', card)
        self.assertIn('<p class="meta sources"><strong>Sources:</strong>', card)
        self.assertIn('<a href="https://outlet-a.example/story">Outlet A</a>', card)
        self.assertIn('<a href="https://outlet-b.example/story">Outlet B</a>', card)

    def test_build_publishes_feed_configuration_for_live_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "feeds.yaml"
            config.write_text(json.dumps({
                "feeds": [{
                    "name": "Example News",
                    "url": "https://example.com/feed.xml",
                }],
            }), encoding="utf-8")
            output = root / "dist"
            with patch.dict("sys.modules", {"yaml": SimpleNamespace(safe_load=json.loads)}), \
                    patch("fetch_news.fetch_feed", return_value=[
                        self.article("Latest headline", "Example News")
                    ]), \
                    patch("fetch_news.fetch_atlanta_forecast", side_effect=TimeoutError), \
                    patch("fetch_news.fetch_atlanta_alerts", side_effect=TimeoutError):
                build_dashboard(config, output)

            feeds = json.loads((output / "feeds.json").read_text(encoding="utf-8"))
            self.assertEqual(feeds, [{
                "name": "Example News",
                "url": "https://example.com/feed.xml",
            }])
            page = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn("Sources (1)", page)
            self.assertIn("<li>Example News</li>", page)
            self.assertIn("Forecast unavailable right now.", page)
            self.assertIn("Alerts temporarily unavailable.", page)
            self.assertFalse((output / "articles").exists())

    def test_feed_summaries_are_cleaned_and_capped(self):
        self.assertEqual(clean_summary("<p>Short &amp; useful.</p>"), "Short & useful.")
        clipped = clean_summary("A useful feed summary. " * 10, limit=40)
        self.assertLessEqual(len(clipped), 40)
        self.assertTrue(clipped.endswith("…"))

    def test_feed_images_use_rss_metadata_or_embedded_images(self):
        feed_url = "https://example.com/feed.xml"
        self.assertEqual(
            extract_image_url(
                {"media_content": [{"url": "https://images.example.com/story.jpg", "medium": "image"}]},
                feed_url,
            ),
            "https://images.example.com/story.jpg",
        )
        self.assertEqual(
            extract_image_url(
                {"summary": '<p>Story</p><img src="/images/story.webp">'},
                feed_url,
            ),
            "https://example.com/images/story.webp",
        )
        self.assertEqual(
            extract_image_url(
                {"media_thumbnail": [{"url": "http://images.example.com/story.jpg"}]},
                feed_url,
            ),
            "",
        )

    def test_rendered_article_images_are_lazy_and_escaped(self):
        local_story = Story([self.article(
            "Atlanta story with an image",
            "Local Outlet",
            local_news=True,
        )])
        local_story.latest.image_url = "https://images.example.com/story.jpg?size=large&crop=1"
        regional_story = Story([self.article("Regional story with an image", "Regional Outlet")])
        regional_story.latest.image_url = "https://images.example.com/regional.jpg"
        page = render_page(
            [(local_story, "us"), (regional_story, "us")],
            self.now,
        )
        self.assertEqual(page.count('<article class="story story--with-image">'), 2)
        self.assertIn(".story--with-image { display: grid;", page)
        self.assertIn(
            '<img class="story-image" src="https://images.example.com/story.jpg?size=large&amp;crop=1" '
            'alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">',
            page,
        )

    def test_us_keywords_use_word_boundaries(self):
        self.assertTrue(is_us_related("Congress debates a new bill", ["Congress"]))
        self.assertFalse(is_us_related("Congressional debates continue", ["Congress"]))

    def test_atlanta_stories_are_separated_from_regional_news(self):
        us_story = Story([
            self.article("Senate approves major bill", "A", us_focused=True),
            self.article("Senate approves major bill", "B", us_focused=True),
            self.article("Senate approves major bill", "C", us_focused=True),
        ])
        europe_story = Story([
            self.article("Ukraine announces new talks", "D"),
            self.article("Ukraine announces new talks", "E"),
            self.article("Ukraine announces new talks", "F"),
        ])
        middle_east_story = Story([self.article("US officials meet Iranian leaders", "G")])
        atlanta_story = Story([
            self.article("Atlanta council approves new transit plan", "FOX 5", local_news=True),
            self.article("Atlanta council approves new transit plan", "WSB-TV", local_news=True),
        ])
        nonlocal_story = self.article(
            "Atlanta council approves new transit plan", "National News"
        )
        ranked = rank_stories(
            [
                us_story,
                europe_story,
                middle_east_story,
                atlanta_story,
                Story([nonlocal_story]),
            ],
            ["Senate", "US"],
            self.now,
            {"europe": ["Ukraine"], "middle_east": ["Iran", "Iranian"]},
        )
        sections, atlanta_stories = group_sections(ranked)
        self.assertEqual(len(sections["us"]), 1)
        self.assertEqual(len(sections["europe"]), 1)
        self.assertEqual(len(sections["middle_east"]), 1)
        self.assertIn(middle_east_story, sections["middle_east"])
        self.assertEqual(atlanta_stories, [atlanta_story])
        clusters = cluster_articles(atlanta_story.articles + [nonlocal_story])
        self.assertEqual(len(clusters), 2)
        page = render_page(ranked, self.now)
        self.assertIn("Atlanta", page)
        self.assertNotIn("Other News", page)
        self.assertIn('class="regional-grid"', page)
        self.assertIn("grid-template-columns: repeat(3", page)
        self.assertIn("grid-auto-flow: column", page)
        self.assertLess(page.index('id="us"'), page.index('id="europe"'))
        self.assertLess(page.index('id="europe"'), page.index('id="middle-east"'))
        self.assertLess(page.index('id="middle-east"'), page.index('id="atlanta"'))


if __name__ == "__main__":
    unittest.main()
