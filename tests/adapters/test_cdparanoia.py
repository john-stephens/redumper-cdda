import unittest
import tempfile
from pathlib import Path

from redumper_cdda.adapters.cdparanoia import CdparanoiaTocReader
from redumper_cdda.domain.errors import LayoutError, TocParseError
from redumper_cdda.ports.process import CommandResult
from tests.contracts.fakes import RecordingReporter


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def capture(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self.result


class CdparanoiaTocReaderTests(unittest.TestCase):
    def test_reads_combined_text_output_with_exact_command(self):
        result = CommandResult(
            (), 0, "cdparanoia header\n", " 1.  75 [00:01.00]  150 [00:02.00]\n"
        )
        runner = FakeRunner(result)
        reporter = RecordingReporter()
        layout = CdparanoiaTocReader(runner, reporter).read("/dev/sr0")

        self.assertEqual(layout.tracks[0].end_lba, 225)
        self.assertEqual(reporter.events[0].name, "layout_read")
        self.assertEqual(
            runner.calls,
            [
                (
                    ["cdparanoia", "-Q", "-d", "/dev/sr0"],
                    {"text": True, "merge_stderr": False},
                )
            ],
        )

    def test_reports_tool_failure_with_and_without_details(self):
        cases = (
            ("standard out\n", "standard error\n", "\n\nstandard out\nstandard error"),
            ("", "", ""),
        )
        for stdout, stderr, suffix in cases:
            with self.subTest(stdout=stdout, stderr=stderr):
                reader = CdparanoiaTocReader(
                    FakeRunner(CommandResult((), 2, stdout, stderr)),
                    RecordingReporter(),
                )
                with self.assertRaises(LayoutError) as caught:
                    reader.read("/dev/sr0")
                self.assertEqual(str(caught.exception), "cdparanoia -Q failed" + suffix)

    def test_translates_invalid_toc_to_typed_error(self):
        reader = CdparanoiaTocReader(
            FakeRunner(CommandResult((), 0, None, None)), RecordingReporter()
        )
        with self.assertRaisesRegex(TocParseError, "Could not parse") as caught:
            reader.read("/dev/sr0")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    def test_reads_captured_toc_from_file_without_running_cdparanoia(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cdparanoia.txt"
            path.write_text(
                " 1.  75 [00:01.00]  150 [00:02.00]\n",
                encoding="utf-8",
            )
            runner = FakeRunner(None)
            reporter = RecordingReporter()

            layout = CdparanoiaTocReader(runner, reporter, path).read("-")

            self.assertEqual(layout.tracks[0].end_lba, 225)
            self.assertEqual(runner.calls, [])
            self.assertIn("file", reporter.events[0].values)

    def test_file_read_failure_is_typed(self):
        reader = CdparanoiaTocReader(
            FakeRunner(None), RecordingReporter(), Path("/missing/toc.txt")
        )
        with self.assertRaisesRegex(LayoutError, "Could not read cdparanoia") as caught:
            reader.read("-")
        self.assertIsInstance(caught.exception.__cause__, OSError)


if __name__ == "__main__":
    unittest.main()
