import unittest

from redumper_cdda.adapters.mmc import MmcTocReader
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


class MmcTocReaderTests(unittest.TestCase):
    @staticmethod
    def descriptor(number, lba, control=0):
        return bytes([0, control, number, 0]) + lba.to_bytes(4, "big")

    @classmethod
    def toc(cls):
        body = cls.descriptor(1, 150) + cls.descriptor(0xAA, 225)
        return (len(body) + 2).to_bytes(2, "big") + b"\x01\x01" + body

    def test_reads_binary_mmc_toc_with_exact_command(self):
        runner = FakeRunner(CommandResult((), 0, self.toc(), b""))
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
                )
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
            FakeRunner(CommandResult((), 0, b"bad", b"")), RecordingReporter()
        )
        with self.assertRaisesRegex(TocParseError, "shorter than") as caught:
            reader.read("/dev/sr0")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)


if __name__ == "__main__":
    unittest.main()
