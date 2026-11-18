import unittest
from datetime import datetime, timezone

from fetch_news import (
    Article,
    Story,
    cluster_articles,
    clean_summary,
    group_sections,
    is_us_related,
    rank_stories,
    render_page,
)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)

    def article(self, title, outlet, hours_ago=0, us_focused=False, summary=""):
        from datetime import timedelta

        return Article(
            title=title,
            link="https://example.com/story",
            outlet=outlet,
            us_focused=us_focused,
            published=self.now - timedelta(hours=hours_ago),
            summary=summary,
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
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;b&gt;feed summary&lt;/b&gt;", page)
        self.assertIn('<p class="summary">', page)
        self.assertIn("color-scheme: dark", page)
        self.assertIn("#0d1117", page)

    def test_feed_summaries_are_cleaned_and_capped(self):
        self.assertEqual(clean_summary("<p>Short &amp; useful.</p>"), "Short & useful.")
        clipped = clean_summary("A useful feed summary. " * 10, limit=40)
        self.assertLessEqual(len(clipped), 40)
        self.assertTrue(clipped.endswith("…"))

    def test_us_keywords_use_word_boundaries(self):
        self.assertTrue(is_us_related("Congress debates a new bill", ["Congress"]))
        self.assertFalse(is_us_related("Congressional debates continue", ["Congress"]))

    def test_regions_and_undercovered_stories_are_separated(self):
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
        other_world_story = Story([
            self.article("China announces new trade rules", "H"),
            self.article("China announces new trade rules", "I"),
            self.article("China announces new trade rules", "J"),
        ])
        ranked = rank_stories(
            [us_story, europe_story, middle_east_story, other_world_story],
            ["Senate", "US"],
            self.now,
            {"europe": ["Ukraine"], "middle_east": ["Iran", "Iranian"]},
        )
        sections, worth_a_look = group_sections(ranked)
        self.assertEqual(len(sections["us"]), 1)
        self.assertEqual(len(sections["europe"]), 1)
        self.assertEqual(len(sections["middle_east"]), 1)
        self.assertIn(middle_east_story, sections["middle_east"])
        self.assertEqual(len(worth_a_look), 2)
        self.assertIn("Middle East", render_page(ranked, self.now))
        page = render_page(ranked, self.now)
        self.assertIn("Other News", page)
        self.assertIn('class="regional-grid"', page)
        self.assertIn("grid-template-columns: repeat(3", page)
        self.assertIn("grid-auto-flow: column", page)
        self.assertLess(page.index('id="us"'), page.index('id="europe"'))
        self.assertLess(page.index('id="europe"'), page.index('id="middle-east"'))
        self.assertLess(page.index('id="middle-east"'), page.index('id="other-news"'))


if __name__ == "__main__":
    unittest.main()
