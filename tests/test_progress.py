import io
import unittest
from unittest.mock import Mock

from feedian.progress import ProgressReporter


class ProgressReporterTests(unittest.TestCase):
    def test_auto_mode_uses_plain_output_for_a_non_terminal_stream(self) -> None:
        stream = Mock(spec=io.StringIO)
        stream.isatty.return_value = False

        reporter = ProgressReporter("auto", stream=stream)

        self.assertEqual(reporter.mode, "plain")

    def test_plain_mode_reports_task_start_and_completion(self) -> None:
        stream = io.StringIO()
        reporter = ProgressReporter("plain", stream=stream)

        with reporter:
            reporter.start_task("sync: scanning notes", total=2)
            reporter.advance()
            reporter.advance()

        output = stream.getvalue()
        self.assertIn("sync: scanning notes 0/2", output)
        self.assertIn("sync: scanning notes 2/2", output)

    def test_rich_mode_shows_completed_count_when_total_is_unknown(self) -> None:
        stream = io.StringIO()
        reporter = ProgressReporter("rich", stream=stream)

        with reporter:
            reporter.start_task("process: collecting bookmarks")
            reporter.advance(50)

        self.assertIn("50/???", stream.getvalue())

    def test_rich_mode_marks_a_previous_total_as_approximate(self) -> None:
        stream = io.StringIO()
        reporter = ProgressReporter("rich", stream=stream)

        with reporter:
            reporter.start_task("process: collecting bookmarks", total=3163, estimated_total=True)
            reporter.advance(50)

        self.assertIn("50/~3163", stream.getvalue())

    def test_rich_mode_can_preserve_a_completed_phase(self) -> None:
        stream = io.StringIO()
        reporter = ProgressReporter("rich", stream=stream)

        with reporter:
            reporter.start_task("process: collecting bookmarks", total=100)
            reporter.advance(100)
            reporter.start_task("process: syncing items", total=100, preserve_previous=True)
            reporter.advance(100)

        output = stream.getvalue()
        self.assertIn("process: collecting bookmarks", output)
        self.assertIn("process: syncing items", output)
        self.assertIn("100/100", output)

    def test_plain_mode_prints_the_previous_phase_at_transition(self) -> None:
        stream = io.StringIO()
        reporter = ProgressReporter("plain", stream=stream, plain_interval_seconds=60)

        with reporter:
            reporter.start_task("process: collecting bookmarks", total=100)
            reporter.advance(50)
            reporter.start_task("process: syncing items", total=100, preserve_previous=True)

        output = stream.getvalue()
        self.assertIn("process: collecting bookmarks 50/100", output)
        self.assertIn("process: syncing items 0/100", output)

    def test_finish_task_replaces_an_estimate_with_the_actual_total(self) -> None:
        stream = io.StringIO()
        reporter = ProgressReporter("rich", stream=stream)

        with reporter:
            reporter.start_task("process: syncing items", total=110, estimated_total=True)
            reporter.advance(100)
            reporter.finish_task(100)

        output = stream.getvalue()
        self.assertIn("100%", output)
        self.assertIn("100/100", output)
        self.assertNotIn("100/~110", output.splitlines()[-1])

    def test_rich_mode_updates_usage_in_the_active_description(self) -> None:
        stream = io.StringIO()
        reporter = ProgressReporter("rich", stream=stream)

        with reporter:
            reporter.start_task("ingest: creating source notes", total=2)
            reporter.set_description("ingest: in 1,200 | out 150 | $0.002000")
            reporter.advance()

        output = stream.getvalue()
        self.assertIn("in 1,200", output)
        self.assertIn("$0.002000", output)

    def test_rich_mode_can_retain_a_final_static_progress_display(self) -> None:
        stream = io.StringIO()
        reporter = ProgressReporter("rich", stream=stream)

        with reporter:
            reporter.start_task("process: syncing items", total=2)
            reporter.advance(2)
            reporter.finish_task(2)
            reporter.retain_final()

        output = stream.getvalue()
        self.assertEqual(output.count("process: syncing items"), 1)
        self.assertIn("2/2", output)


if __name__ == "__main__":
    unittest.main()
