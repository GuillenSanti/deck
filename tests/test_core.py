import unittest
from pathlib import Path

import cards.inbox
import dash_github
import dash_ui
import pr_review


class CoreTests(unittest.TestCase):
    def test_gitleaks_does_not_exclude_private_paths(self):
        config = (Path(__file__).parents[1] / ".gitleaks.toml").read_text()
        self.assertNotRegex(config, r"(?m)^\[allowlist\]")
        self.assertNotRegex(config, r"(?m)^paths\s*=")

    def test_parse_pull_request_inputs(self):
        self.assertEqual(pr_review.parse("https://github.com/acme/shop/pull/42"), ("acme/shop", 42))
        self.assertEqual(pr_review.parse("acme/shop#42"), ("acme/shop", 42))

    def test_first_line_removes_markdown_wrappers(self):
        self.assertEqual(cards.inbox.first_line("## Hello\n\n> quoted"), "Hello")

    def test_latest_check_attempt_wins(self):
        checks = [
            {"name": "unit", "startedAt": "2026-01-01T00:00:00Z", "conclusion": "FAILURE"},
            {"name": "unit", "startedAt": "2026-01-02T00:00:00Z", "conclusion": "SUCCESS"},
        ]
        self.assertEqual(dash_github.check_counts(checks)["failed"], 0)

    def test_github_text_cannot_inject_terminal_controls(self):
        data = {"title": "safe\x1b]52;c;payload\x07text", "nested": ["one\ntwo"]}
        safe = dash_github.safe_api_data(data)
        self.assertEqual(safe["title"], "safe ]52;c;payload text")
        self.assertEqual(safe["nested"], ["one two"])
        self.assertNotIn("\x1b", safe["title"])

    def test_fit_keeps_columns_with_colours(self):
        line = dash_ui.fit("\033[31mhello\033[0m", 8)
        self.assertEqual(dash_ui.width(line), 8)


if __name__ == "__main__":
    unittest.main()
