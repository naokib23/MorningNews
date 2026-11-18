import json
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
                    ]):
                build_dashboard(config, output)

            feeds = json.loads((output / "feeds.json").read_text(encoding="utf-8"))
            self.assertEqual(feeds, [{
                "name": "Example News",
                "url": "https://example.com/feed.xml",
            }])
            self.assertTrue((output / "index.html").exists())

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
        story = Story([self.article(
            "Story with an image",
            "Outlet",
        )])
        story.latest.image_url = "https://images.example.com/story.jpg?size=large&crop=1"
        page = render_page([(story, "us")], self.now)
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
