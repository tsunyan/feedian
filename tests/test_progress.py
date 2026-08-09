import io
import unittest
from unittest.mock import Mock

from raindian.progress import ProgressReporter


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


if __name__ == "__main__":
    unittest.main()
