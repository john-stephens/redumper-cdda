import unittest
import tempfile
from pathlib import Path

from redumper_cdda.adapters.mmc import MmcTocReader
from redumper_cdda.domain.errors import LayoutError, TocParseError
from redumper_cdda.ports.process import CommandResult
from tests.contracts.fakes import RecordingReporter


class FakeRunner:
    def __init__(self, *results):
        self.results = iter(results)
        self.calls = []

    def capture(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return next(self.results)


class MmcTocReaderTests(unittest.TestCase):
    @staticmethod
    def descriptor(number, lba, control=0):
        return bytes([0, control, number, 0]) + lba.to_bytes(4, "big")

    @classmethod
    def toc(cls):
        body = cls.descriptor(1, 150) + cls.descriptor(0xAA, 225)
        return (len(body) + 2).to_bytes(2, "big") + b"\x01\x01" + body

    @staticmethod
    def full_toc():
        track = bytes((1, 0x10, 0, 1, 0, 0, 0, 0, 0, 4, 0))
        leadout = bytes((1, 0x10, 0, 0xA2, 0, 0, 0, 0, 0, 5, 0))
        body = track + leadout
        return (len(body) + 2).to_bytes(2, "big") + b"\x01\x01" + body

    def test_reads_binary_mmc_toc_with_exact_command(self):
        runner = FakeRunner(
            CommandResult((), 0, self.toc(), b""),
            CommandResult((), 0, self.full_toc(), b""),
        )
        reporter = RecordingReporter()
        reader = MmcTocReader(runner, reporter)

        layout = reader.read("/dev/sr0")

        self.assertEqual(layout.track(1).begin_lba, 150)
        self.assertEqual(layout.track(1).end_lba, 225)
        self.assertEqual(reporter.events[0].name, "layout_read")
        self.assertEqual(
            runner.calls,
            [
                (
                    [
                        "sg_raw", "--readonly", "--binary", "--request=804",
                        "/dev/sr0", "43", "00", "00", "00", "00", "00",
                        "00", "03", "24", "00",
                    ],
                    {"text": False, "merge_stderr": False},
                ),
                (
                    [
                        "sg_raw", "--readonly", "--binary", "--request=4096",
                        "/dev/sr0", "43", "02", "02", "00", "00", "00",
                        "01", "10", "00", "00",
                    ],
                    {"text": False, "merge_stderr": False},
                ),
            ],
        )

    def test_reports_tool_failure_with_and_without_details(self):
        for stderr, suffix in ((b"device error\n", "\n\ndevice error"), (b"", "")):
            with self.subTest(stderr=stderr):
                reader = MmcTocReader(
                    FakeRunner(CommandResult((), 2, b"", stderr)),
                    RecordingReporter(),
                )
                with self.assertRaises(LayoutError) as caught:
                    reader.read("/dev/sr0")
                self.assertEqual(str(caught.exception), "MMC READ TOC failed" + suffix)

    def test_translates_invalid_toc_to_typed_error(self):
        reader = MmcTocReader(
            FakeRunner(
                CommandResult((), 0, b"bad", b""),
                CommandResult((), 0, self.full_toc(), b""),
            ),
            RecordingReporter(),
        )
        with self.assertRaisesRegex(TocParseError, "shorter than") as caught:
            reader.read("/dev/sr0")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    def test_reports_full_toc_failure(self):
        reader = MmcTocReader(
            FakeRunner(
                CommandResult((), 0, self.toc(), b""),
                CommandResult((), 2, b"", b"full failed"),
            ),
            RecordingReporter(),
        )
        with self.assertRaisesRegex(LayoutError, "(?s)format 2 failed.*full failed"):
            reader.read("/dev/sr0")

    def test_reads_binary_tocs_from_files_without_running_sg_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            toc_path = Path(directory) / "disc.toc"
            full_toc_path = Path(directory) / "disc.fulltoc"
            toc_path.write_bytes(self.toc())
            full_toc_path.write_bytes(self.full_toc())
            runner = FakeRunner()
            reporter = RecordingReporter()

            layout = MmcTocReader(
                runner, reporter, toc_path, full_toc_path
            ).read("-")

            self.assertEqual(layout.track(1).begin_lba, 150)
            self.assertEqual(layout.track(1).end_lba, 225)
            self.assertEqual(runner.calls, [])
            self.assertIn("files", reporter.events[0].values)

    def test_file_read_and_parse_failures_are_typed(self):
        reader = MmcTocReader(
            FakeRunner(), RecordingReporter(), Path("/missing/disc.toc"),
            Path("/missing/disc.fulltoc"),
        )
        with self.assertRaisesRegex(LayoutError, "Could not read MMC") as caught:
            reader.read("-")
        self.assertIsInstance(caught.exception.__cause__, OSError)

        with tempfile.TemporaryDirectory() as directory:
            toc_path = Path(directory) / "disc.toc"
            full_toc_path = Path(directory) / "disc.fulltoc"
            toc_path.write_bytes(b"bad")
            full_toc_path.write_bytes(self.full_toc())
            reader = MmcTocReader(
                FakeRunner(), RecordingReporter(), toc_path, full_toc_path
            )
            with self.assertRaisesRegex(TocParseError, "shorter than"):
                reader.read("-")


if __name__ == "__main__":
    unittest.main()
