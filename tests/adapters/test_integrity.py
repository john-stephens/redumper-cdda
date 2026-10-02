"""Adapter tests for redumper media-error and state-file handling."""

import io
from contextlib import redirect_stdout
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC_PATH = Path(__file__).parent.parent / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from redumper_cdda import integrity


class IntegrityCoverageTests(unittest.TestCase):
    def test_media_error_last_block_and_default_offset(self):
        text = (
            "media errors: SCSI: 1 C2: 2 Q: 3\n"
            "media errors: SCSI: 4 C2: 5 Q: 6\n"
        )
        self.assertEqual(
            self.module.parse_media_errors(text), {"SCSI": 4, "C2": 5, "Q": 6}
        )
        self.assertEqual(
            self.module.parse_split_write_offsets("disc write offset: 48"),
            [(0, 48)],
        )

    def test_media_error_progress_status_after_refine(self):
        output = (
            "media errors:\n  SCSI: 0 samples\n  C2: 733 samples\n  Q: 1177\n"
            "- [  3%] LBA: 181000/196042, "
            "errors: { SCSIs: 0, C2s: 727, Q: 1177 }\n"
            "- [ 85%] LBA: 193819/196042, "
            "errors: { SCSIs: 0, C2s: 310, Q: 1175 }, retry: 100\n"
            "correction statistics:\n"
            "  SCSI: 0 samples\n  C2: 423 samples\n  Q: 2 sectors\n"
        )
        self.assertEqual(
            self.module.parse_media_errors(output),
            {"SCSI": 0, "C2": 310, "Q": 1175},
        )

        completed_after_last_progress = (
            "[ 2%] errors: { SCSIs: 0, C2s: 57, Q: 1680 }\n"
            "[66%] errors: { SCSIs: 0, C2s: 32, Q: 1680 }\n"
            "correction statistics:\n"
            "  SCSI: 0 samples\n  C2: 57 samples\n  Q: 0 sectors\n"
        )
        self.assertEqual(
            self.module.parse_media_errors(completed_after_last_progress),
            {"SCSI": 0, "C2": 0, "Q": 1680},
        )

        final_summary = output + (
            "media errors:\n  SCSI: 0 samples\n  C2: 12 samples\n  Q: 9\n"
        )
        self.assertEqual(
            self.module.parse_media_errors(final_summary),
            {"SCSI": 0, "C2": 12, "Q": 9},
        )

    def test_correction_statistics_are_not_current_status(self):
        output = (
            "correction statistics:\n"
            "  SCSI: 0 samples\n  C2: 423 samples\n  Q: 2 sectors\n"
        )
        self.assertIsNone(self.module.parse_media_errors(output))

    def test_state_file_success_counts_samples_and_sectors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disc.state"
            first = bytes([0]) + bytes([2]) * 587
            second = bytes([1]) + bytes([2]) * 587
            path.write_bytes(first + second)
            tracks = [{"number": 1, "begin": -45150, "end": -45148}]
            result = self.module.inspect_track_media_errors(
                path, tracks, [(-45150, 0), (-45149, 0)]
            )
            self.assertEqual(result[1]["SCSI"], 1)
            self.assertEqual(result[1]["C2"], 1)
            self.assertEqual(result[1]["SCSI sectors"], 1)
            self.assertEqual(result[1]["C2 sectors"], 1)
    def setUp(self):
        self.module = importlib.reload(integrity)

    def test_media_error_empty_and_printing(self):
        self.assertIsNone(self.module.parse_media_errors("nothing useful"))
        with redirect_stdout(io.StringIO()) as output:
            self.module.print_media_errors({"SCSI": 1, "C2": 2, "Q": 3})
        self.assertIn("SCSI: 1", output.getvalue())

    def test_split_offset_errors_and_state_offset_boundary(self):
        with self.assertRaisesRegex(RuntimeError, "write offset"):
            self.module.parse_split_write_offsets("no offset")
        with self.assertRaisesRegex(RuntimeError, "unsorted"):
            self.module.parse_split_write_offsets(
                "disc write offset: 0\n"
                "offset shift correction applied:\n"
                "LBA: 10, offset: 1\nLBA: 5, offset: 2\n"
            )
        with self.assertRaisesRegex(RuntimeError, "shift boundaries"):
            self.module.parse_split_write_offsets(
                "disc write offset: 0\n"
                "offset shift correction applied:\nnot an offset\n"
            )
        self.assertEqual(
            self.module.parse_split_write_offsets(
                "disc write offset: 4\n"
                "offset shift correction applied:\n\n"
                "LBA: 0, offset: 7\n"
            ),
            [(0, 7)],
        )
        self.assertEqual(
            self.module.state_offset_for_lba([(0, 4), (10, 8)], 5),
            4,
        )

    def test_state_file_validation_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            missing = directory / "missing.state"
            with self.assertRaisesRegex(RuntimeError, "was not found"):
                self.module.inspect_track_media_errors(
                    missing, [{"number": 1, "begin": 0, "end": 1}], [(0, 0)]
                )

            state = directory / "track.state"
            state.write_bytes(b"")
            with self.assertRaisesRegex(RuntimeError, "precedes"):
                self.module.inspect_track_media_errors(
                    state,
                    [{"number": 1, "begin": self.module.REDUMPER_LBA_START - 1,
                      "end": self.module.REDUMPER_LBA_START}],
                    [(0, 0)],
                )

            with self.assertRaisesRegex(RuntimeError, "shorter"):
                self.module.inspect_track_media_errors(
                    state,
                    [{"number": 1, "begin": self.module.REDUMPER_LBA_START,
                      "end": self.module.REDUMPER_LBA_START + 1}],
                    [(0, 0)],
                )

            state.write_bytes(
                bytes([self.module.REDUMPER_MAX_STATE + 1])
                * self.module.SAMPLES_PER_SECTOR
            )
            with self.assertRaisesRegex(RuntimeError, "unknown state"):
                self.module.inspect_track_media_errors(
                    state,
                    [{"number": 1, "begin": self.module.REDUMPER_LBA_START,
                      "end": self.module.REDUMPER_LBA_START + 1}],
                    [(0, 0)],
                )
