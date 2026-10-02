"""Tests for disc-layout parsing, reconciliation, and selection."""

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SRC_PATH = Path(__file__).parent.parent / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from redumper_cdda import layout, workflow


class LayoutCoverageTests(unittest.TestCase):
    def setUp(self):
        self.module = layout
        self.workflow = workflow

    @staticmethod
    def descriptor(number, lba, control=0):
        return bytes([0, control, number, 0]) + lba.to_bytes(4, "big")

    def toc(self, first, last, descriptors):
        body = b"".join(descriptors)
        return (len(body) + 2).to_bytes(2, "big") + bytes([first, last]) + body

    def assert_toc_error(self, data, message):
        with self.assertRaisesRegex(RuntimeError, message):
            self.module.parse_mmc_toc(data)

    def test_mmc_toc_validation_failures(self):
        self.assert_toc_error(b"\0\0\0", "shorter than")
        self.assert_toc_error(b"\0\x02\x01\x01", "no track layout")
        self.assert_toc_error(b"\0\x0a\x01\x01", "truncated")
        self.assert_toc_error(
            b"\0\x0b\x01\x01" + b"x" * 9,
            "malformed descriptors",
        )
        self.assert_toc_error(
            self.toc(0, 1, [self.descriptor(1, 0), self.descriptor(0xAA, 10)]),
            "invalid track range",
        )
        self.assert_toc_error(
            self.toc(1, 1, [self.descriptor(0xAA, 10), self.descriptor(0xAA, 20)]),
            "multiple lead-out",
        )
        self.assert_toc_error(
            self.toc(1, 1, [self.descriptor(2, 0), self.descriptor(0xAA, 10)]),
            "missing track",
        )
        self.assert_toc_error(
            self.toc(1, 1, [self.descriptor(1, 0)]),
            "no lead-out",
        )
        self.assert_toc_error(
            self.toc(
                1,
                1,
                [self.descriptor(1, 0), self.descriptor(1, 1), self.descriptor(0xAA, 10)],
            ),
            "duplicate",
        )
        self.assert_toc_error(
            self.toc(1, 1, [self.descriptor(1, 10), self.descriptor(0xAA, 10)]),
            "non-positive",
        )

    def test_read_mmc_toc_success_and_errors(self):
        toc = self.toc(1, 1, [self.descriptor(1, 0), self.descriptor(0xAA, 10)])
        process = mock.Mock(returncode=0)
        with (
            mock.patch.object(self.workflow.subprocess, "Popen", return_value=process) as popen,
            mock.patch.object(self.workflow, "communicate_with_process", return_value=(toc, b"")),
            redirect_stdout(io.StringIO()) as output,
        ):
            tracks = self.workflow.read_mmc_toc("/dev/test", verbose=True)
        self.assertEqual(tracks[0]["length"], 10)
        self.assertIn("MMC READ TOC", output.getvalue())
        self.assertIn("--readonly", popen.call_args.args[0])

        for stderr, expected in ((b"failure", "failure"), (b"", "MMC READ TOC failed")):
            process = mock.Mock(returncode=1)
            with (
                mock.patch.object(self.workflow.subprocess, "Popen", return_value=process),
                mock.patch.object(
                    self.workflow, "communicate_with_process", return_value=(b"", stderr)
                ),
                self.assertRaisesRegex(RuntimeError, expected),
            ):
                self.workflow.read_mmc_toc("/dev/test")

    def test_cdparanoia_parse_and_read(self):
        text = "header\n 1.  75 [00:01.00]  150 [00:02.00]\n"
        self.assertEqual(self.module.parse_cdparanoia_toc(text)[0]["end"], 225)
        with self.assertRaisesRegex(RuntimeError, "Could not parse"):
            self.module.parse_cdparanoia_toc("garbage")

        process = mock.Mock(returncode=0)
        with (
            mock.patch.object(self.workflow.subprocess, "Popen", return_value=process),
            mock.patch.object(
                self.workflow,
                "communicate_with_process",
                return_value=("", text),
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            tracks = self.workflow.read_disc_toc("/dev/test", verbose=True)
        self.assertEqual(tracks[0]["number"], 1)
        self.assertIn("cdparanoia", output.getvalue())

        for stdout, stderr in (("out", "err"), ("", "")):
            process = mock.Mock(returncode=1)
            with (
                mock.patch.object(self.workflow.subprocess, "Popen", return_value=process),
                mock.patch.object(
                    self.workflow,
                    "communicate_with_process",
                    return_value=(stdout, stderr),
                ),
                self.assertRaisesRegex(RuntimeError, "cdparanoia -Q failed"),
            ):
                self.workflow.read_disc_toc("/dev/test")

    def test_layout_and_selection_error_paths(self):
        audio = {
            "number": 1, "kind": "audio", "control": 0,
            "begin": 0, "end": 10, "length": 10,
            "length_msf": "00:00.10", "begin_msf": "00:00.00",
        }
        data = dict(audio, kind="data", control=4)
        with self.assertRaisesRegex(RuntimeError, "which tracks are audio"):
            self.module.reconcile_disc_layout([audio], [])

        with (
            mock.patch.object(self.workflow, "read_mmc_toc", return_value=[audio]),
            mock.patch.object(self.workflow, "read_disc_toc", return_value=[audio]) as read_audio,
        ):
            self.assertEqual(self.workflow.read_disc_layout("dev"), [audio])
        read_audio.assert_called_once()

        with self.assertRaisesRegex(RuntimeError, "Available tracks"):
            self.module.find_disc_track([audio], 2)
        with self.assertRaisesRegex(RuntimeError, "only supported"):
            self.module.find_requested_track([data], 0)
        with self.assertRaisesRegex(Exception, "track must be"):
            self.module.parse_track_selection("bad")
        with self.assertRaisesRegex(RuntimeError, "No audio"):
            self.module.resolve_track_selection([], {"start": None, "end": None})
        with self.assertRaisesRegex(RuntimeError, "Invalid track range"):
            self.module.resolve_track_selection([audio], {"start": 2, "end": 1})
        track_three = dict(audio, number=3, begin=20, end=30)
        with self.assertRaisesRegex(RuntimeError, "Audio track 2"):
            self.module.resolve_track_selection(
                [audio, track_three], {"start": 2, "end": None}
            )

        track_zero_source = dict(audio, begin=5, end=15, length=10)
        selected = self.module.resolve_track_selection(
            [track_zero_source], {"start": 0, "end": None}
        )
        self.assertEqual([track["number"] for track in selected], [0, 1])

        with self.assertRaisesRegex(RuntimeError, "No tracks"):
            self.module.resolve_disc_selection([], {"start": None, "end": None}, True)
        with self.assertRaisesRegex(RuntimeError, "Invalid track range"):
            self.module.resolve_disc_selection([audio], {"start": 2, "end": 1}, True)
        with self.assertRaisesRegex(RuntimeError, "Track 2"):
            self.module.resolve_disc_selection([audio], {"start": 1, "end": 2}, True)
        selected = self.module.resolve_disc_selection(
            [track_zero_source], {"start": 0, "end": 1}, True
        )
        self.assertEqual([track["number"] for track in selected], [0, 1])


if __name__ == "__main__":
    unittest.main()
