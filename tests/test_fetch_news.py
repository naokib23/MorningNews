import unittest
from datetime import datetime, timezone

from fetch_news import Article, Story, cluster_articles, is_us_related, rank_stories, render_page


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)

    def article(self, title, outlet, hours_ago=0, us_focused=False):
        from datetime import timedelta

        return Article(
            title=title,
            link="https://example.com/story",
            outlet=outlet,
            us_focused=us_focused,
            published=self.now - timedelta(hours=hours_ago),
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
        self.assertTrue(ranked[0][1])

    def test_html_escapes_untrusted_headlines_and_links(self):
        story = Story([self.article('<script>alert("x")</script>', "Outlet")])
        page = render_page([(story, False)], self.now)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("summary", page.lower())

    def test_us_keywords_use_word_boundaries(self):
        self.assertTrue(is_us_related("Congress debates a new bill", ["Congress"]))
        self.assertFalse(is_us_related("Congressional debates continue", ["Congress"]))


if __name__ == "__main__":
    unittest.main()
