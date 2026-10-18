import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cards.inbox
import dash_github
import dash_common
import dash_settings
import dash_ui
import pr_review


class CoreTests(unittest.TestCase):
    def test_review_defaults_to_claude_and_an_english_prompt(self):
        with mock.patch.object(dash_common, "read_config", return_value={}):
            agent, prompt = pr_review.review_settings()

        self.assertEqual(agent, "claude")
        self.assertTrue(prompt.startswith("Review pull request {url}"))
        self.assertIn("{url}", prompt)

    def test_review_settings_accept_a_local_agent_and_prompt(self):
        settings = {"review": {"agent": "codex", "prompt": "Revisa {url} ({repo}#{number}) y conserva {braces}."}}
        with mock.patch.object(dash_common, "read_config", return_value=settings):
            agent, template = pr_review.review_settings()

        prompt = pr_review.review_prompt(template, "https://github.com/acme/shop/pull/42", "acme/shop", 42)
        self.assertEqual(agent, "codex")
        self.assertEqual(prompt, "Revisa https://github.com/acme/shop/pull/42 (acme/shop#42) y conserva {braces}.")

    def test_review_agent_commands_keep_custom_prompts_as_one_argument(self):
        prompt = "Check PR; don't run commands\nKeep {braces}."
        cases = {
            "claude": ["claude", "--session-id", "session-id", "-n", "review #42", prompt],
            "codex": ["codex", prompt],
            "opencode": ["opencode", "--prompt", prompt],
            "pi": ["pi", prompt],
        }
        for agent, expected in cases.items():
            with self.subTest(agent=agent):
                command = pr_review.agent_command(agent, prompt, "session-id", "review #42")
                self.assertEqual(shlex.split(command), expected)

    def test_review_settings_reject_unknown_agents(self):
        with mock.patch.object(dash_common, "read_config", return_value={"review": {"agent": "unknown"}}):
            with self.assertRaisesRegex(ValueError, "Unknown review agent"):
                pr_review.review_settings()

    def test_settings_show_review_agent_and_prompt_defaults(self):
        with mock.patch.object(dash_settings, "card_choices", return_value=[]), \
                mock.patch.object(dash_settings, "read_config", return_value={}), \
                mock.patch.object(dash_common, "REPO", ""):
            settings = dash_settings.sections()[-1][2]

        self.assertIn(("edit:review_agent", "text", "Review agent", "claude (default)"), settings)
        self.assertIn(("edit:review_prompt", "text", "Review prompt", "English default"), settings)

    def test_settings_save_review_fields_and_keep_the_other_value(self):
        config = {"review": {"agent": "codex", "prompt": "old prompt"}}
        with mock.patch.object(dash_settings, "read_config", return_value=config), \
                mock.patch.object(dash_settings, "save_config") as save_config:
            self.assertIsNone(dash_settings.save("review_agent", "PI"))
            save_config.assert_called_once_with({"review": {"agent": "pi", "prompt": "old prompt"}})
            save_config.reset_mock()

            self.assertIsNone(dash_settings.save("review_prompt", ""))
            save_config.assert_called_once_with({"review": {"agent": "codex"}})

    def test_settings_reject_unknown_review_agent(self):
        with mock.patch.object(dash_settings, "read_config", return_value={}), \
                mock.patch.object(dash_settings, "save_config") as save_config:
            error = dash_settings.save("review_agent", "unknown")

        self.assertIn("claude, codex, opencode, pi", error)
        save_config.assert_not_called()

    def test_review_agent_setting_opens_a_selector_and_saves_choice(self):
        settings = {"review": {"prompt": "Keep this prompt."}}
        with mock.patch.object(dash_settings, "read_config", return_value=settings), \
                mock.patch.object(dash_settings, "save_config") as save_config:
            dash_settings.OPEN[0] = True
            dash_settings.SELECT_AGENT[0] = ""
            dash_settings.act("edit:review_agent")
            lines, _, keys = dash_settings.body()
            self.assertIn("Choose the agent for terminal reviews:", lines)
            self.assertEqual(keys["2"], "agent:codex")

            dash_settings.key("2")

        save_config.assert_called_once_with({"review": {"agent": "codex", "prompt": "Keep this prompt."}})
        self.assertEqual(dash_settings.SELECT_AGENT[0], "")

    def test_review_agent_selector_recovers_from_an_invalid_saved_value(self):
        with mock.patch.object(dash_settings, "read_config", return_value={"review": {"agent": "unknown"}}):
            dash_settings.act("edit:review_agent")

        self.assertEqual(dash_settings.SELECT_AGENT[0], "claude")
        dash_settings.SELECT_AGENT[0] = ""

    def test_review_prompt_editor_loads_current_text_and_saves_edits(self):
        prompt = "Review {url}.\nKeep the summary short."
        settings = {"review": {"agent": "pi", "prompt": prompt}}
        with mock.patch.object(dash_settings, "read_config", return_value=settings), \
                mock.patch.object(dash_settings, "save_config") as save_config:
            dash_settings.EDIT[:] = ["", ""]
            dash_settings.act("edit:review_prompt")
            self.assertEqual(dash_settings.EDIT[1], prompt)
            dash_settings.PROMPT_CURSOR[0] = 7
            dash_settings.prompt_input("!")
            dash_settings.prompt_input("\x0f")

        save_config.assert_called_once_with({"review": {"agent": "pi", "prompt": "Review !{url}.\nKeep the summary short."}})
        self.assertEqual(dash_settings.EDIT, ["", ""])

    def test_review_launches_the_configured_agent_with_the_custom_prompt(self):
        settings = {"review": {"agent": "codex", "prompt": "Revisa {url}; conserva {braces}."}}

        def tmux(*args):
            if args[0] == "display":
                return "0"
            if args[0] == "new-window":
                return "%1"
            if args[0] == "split-window":
                return "%2"
            return ""

        with mock.patch.object(dash_common, "read_config", return_value=settings), \
                mock.patch.object(pr_review, "run", return_value='{"url":"https://github.com/acme/shop/pull/42"}'), \
                mock.patch.object(pr_review, "tmux", side_effect=tmux) as tmux_call, \
                mock.patch.object(pr_review, "clone", return_value="/tmp/shop"), \
                mock.patch.object(pr_review, "earlier_review") as earlier, \
                mock.patch.object(pr_review.shutil, "which", side_effect=lambda name: "/bin/codex" if name == "codex" else None):
            self.assertIsNone(pr_review.review("acme/shop", 42))

        commands = [call.args[-1] for call in tmux_call.call_args_list
                    if call.args[0] == "send-keys" and "-l" in call.args]
        self.assertEqual(shlex.split(commands[-1]), [
            "codex", "Revisa https://github.com/acme/shop/pull/42; conserva {braces}.",
        ])
        earlier.assert_not_called()

    def test_deck_config_name_takes_precedence_over_legacy_alias(self):
        with mock.patch.dict(os.environ, {
            "HOME": str(Path.home()),
            "DECK_CONFIG": "~/deck-config.json",
            "DASH_CONFIG": "~/old-dash-config.json",
        }, clear=True):
            self.assertEqual(dash_common.config_path(), str(Path.home() / "deck-config.json"))

    def test_dash_config_name_remains_a_legacy_fallback(self):
        with mock.patch.dict(os.environ, {
            "HOME": str(Path.home()),
            "DASH_CONFIG": "~/old-dash-config.json",
        }, clear=True):
            self.assertEqual(dash_common.config_path(), str(Path.home() / "old-dash-config.json"))

    def test_review_checkout_is_not_the_home_directory_when_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            code = Path(temp) / "code"
            code.mkdir()
            with mock.patch.object(pr_review, "CODE", str(code)), \
                    mock.patch.object(dash_common, "REPO", "acme/main"), \
                    mock.patch.object(dash_common, "MAIN", ""):
                self.assertIsNone(pr_review.clone("other/project"))

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
