"""Tests for redumper media-error and state-file integrity handling."""

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
