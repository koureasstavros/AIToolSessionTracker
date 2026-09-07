import unittest

from session_token_viewer import derived_conversation_name


class SessionNamingTests(unittest.TestCase):
    def test_plain_prompt_wins_over_slash_command(self) -> None:
        records = [
            {"role": "user", "content": "/review src/app.py"},
            {"role": "user", "content": "Fix the login flow"},
        ]
        self.assertEqual(derived_conversation_name(records, "fallback"), "Fix the login flow")

    def test_slash_command_is_kept_when_no_plain_prompt_exists(self) -> None:
        records = [{"role": "user", "content": "/review src/app.py"}]
        self.assertEqual(derived_conversation_name(records, "fallback"), "/review src/app.py")

    def test_generated_messages_are_skipped_and_wrapper_is_unwrapped(self) -> None:
        records = [
            {"role": "user", "content": "<local-command-stdout>ignored</local-command-stdout>"},
            {"role": "user", "content": "<objective>Improve the dashboard</objective>"},
        ]
        self.assertEqual(derived_conversation_name(records, "fallback"), "Improve the dashboard")


if __name__ == "__main__":
    unittest.main()
