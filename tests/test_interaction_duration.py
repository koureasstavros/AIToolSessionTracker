import unittest

import session_token_viewer


class InteractionDurationTests(unittest.TestCase):
    def test_turn_duration_is_derived_from_raw_request_and_response(self) -> None:
        session = {
            "turns": [{
                "raw": [
                    '{"timestamp":"2026-09-29T10:00:00Z","type":"request"}',
                    '{"timestamp":"2026-09-29T10:00:02.500Z","type":"response"}',
                ],
                "invocations": [{}],
            }],
        }

        session_token_viewer.apply_interaction_durations(session)

        self.assertEqual(session["turns"][0]["activeDurationMs"], 2500)
        self.assertEqual(session["turns"][0]["invocations"][0]["activeDurationMs"], 2500)
        self.assertEqual(session["activeDurationMs"], 2500)

    def test_turn_duration_sums_multiple_invocations(self) -> None:
        session = {"turns": [{"invocations": [{"activeDurationMs": 120}, {"active_duration_ms": 880}]}]}

        session_token_viewer.apply_interaction_durations(session)

        self.assertEqual(session["turns"][0]["activeDurationMs"], 1000)
        self.assertEqual(session["activeDurationMs"], 1000)

    def test_idle_duration_is_the_gap_between_interactions(self) -> None:
        session = {"turns": [
            {"raw": ['{"timestamp":"2026-09-29T10:00:00Z"}', '{"timestamp":"2026-09-29T10:00:01Z"}'], "invocations": []},
            {"raw": ['{"timestamp":"2026-09-29T10:00:04Z"}', '{"timestamp":"2026-09-29T10:00:05Z"}'], "invocations": []},
        ]}

        session_token_viewer.apply_interaction_durations(session)

        self.assertEqual(session["turns"][1]["idleDurationMs"], 3000)
        self.assertEqual(session["idleDurationMs"], 3000)

    def test_invocation_idle_duration_is_gap_within_turn(self) -> None:
        session = {"turns": [{"invocations": [
            {"startTime": "2026-09-29T10:00:00Z", "endTime": "2026-09-29T10:00:01Z"},
            {"startTime": "2026-09-29T10:00:03Z", "endTime": "2026-09-29T10:00:04Z"},
        ]}]}

        session_token_viewer.apply_interaction_durations(session)

        self.assertEqual(session["turns"][0]["invocations"][1]["idleDurationMs"], 2000)

    def test_format_duration_uses_readable_units(self) -> None:
        self.assertEqual(session_token_viewer.format_duration(450), "450 ms")
        self.assertEqual(session_token_viewer.format_duration(2500), "2.5 s")
        self.assertEqual(session_token_viewer.format_duration(125000), "2m 5s")
        self.assertEqual(session_token_viewer.format_duration(3_600_000), "1h")
        self.assertEqual(session_token_viewer.format_duration(86_400_000 * 2), "2d")
        self.assertEqual(session_token_viewer.format_duration(86_400_000 * 10), "1w 3d")
        self.assertEqual(session_token_viewer.format_duration(86_400_000 * 365), "1y")


if __name__ == "__main__":
    unittest.main()