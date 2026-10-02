"""Tests for generated CUE parsing and split-BIN discovery."""

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC_PATH = Path(__file__).parent.parent / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from redumper_cdda import cue


class CueCoverageTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.reload(cue)

    def test_snapshot_and_changed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(self.module.snapshot_files(root / "missing"), {})
            self.assertEqual(self.module.changed_files(root / "missing", {}), [])
            (root / "one.bin").write_bytes(b"a")
            (root / "subdir").mkdir()
            before = self.module.snapshot_files(root)
            self.assertEqual(len(before), 1)
            (root / "one.bin").write_bytes(b"ab")
            (root / "two.bin").write_bytes(b"b")
            changed = self.module.changed_files(root, before)
            self.assertEqual({path.name for path in changed}, {"one.bin", "two.bin"})
            self.assertEqual(self.module.changed_files(root, self.module.snapshot_files(root)), [])

    def test_snapshot_ignores_stat_errors(self):
        file_path = mock.Mock()
        file_path.is_file.return_value = True
        file_path.stat.side_effect = OSError
        directory = mock.Mock()
        directory.exists.return_value = True
        directory.iterdir.return_value = [file_path]
        self.assertEqual(self.module.snapshot_files(directory), {})
        self.assertEqual(self.module.changed_files(directory, {}), [])

    def test_text_and_cue_parsing_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cp1252 = root / "legacy.cue"
            cp1252.write_bytes("REM café".encode("cp1252"))
            self.assertIn("café", self.module.read_text_file(cp1252))

            cue = root / "disc.cue"
            cue.write_text(
                "\nINDEX 01 00:00:00\n"
                "FILE \"one.bin\" BINARY\n"
                "TRACK 01 AUDIO\n"
                "INDEX 00 00:00:00\n"
                "INDEX 01 00:00:01\n",
                encoding="utf-8",
            )
            parsed = self.module.parse_generated_cue(cue)
            self.assertEqual(parsed[0]["file"], "one.bin")
            self.assertEqual(parsed[0]["indexes"][1], 1)

            cue.write_text("TRACK 01 AUDIO\nTITLE unknown\n", encoding="utf-8")
            self.assertEqual(self.module.parse_generated_cue(cue)[0]["indexes"], {})

            no_file = root / "no-file.cue"
            no_file.write_text("TRACK 01 AUDIO\n", encoding="utf-8")
            self.assertIsNone(self.module.parse_generated_cue(no_file)[0]["file"])

            self.assertFalse(self.module.valid_audio_bin(root / "missing.bin"))
            with self.assertRaisesRegex(RuntimeError, "no INDEX 01"):
                self.module.get_index01_offset({"number": 1, "indexes": {}})

    def test_find_generated_cues_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cue = root / "image-result.cue"
            cue.write_text("", encoding="utf-8")
            found = self.module.find_generated_cues(root, "image", [])
            self.assertEqual(found, [cue])

    def test_audio_segment_discovery_failures(self):
        cue = Path("disc.cue")
        with mock.patch.object(self.module, "find_generated_cues", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "generated CUE"):
                self.module.identify_generated_audio_segments(Path("."), "x", [], 1, 1)

        cases = [
            mock.Mock(side_effect=ValueError("bad cue")),
            mock.Mock(return_value=[{"number": 2, "type": "AUDIO", "file": "x", "indexes": {1: 0}}]),
            mock.Mock(return_value=[{"number": 1, "type": "AUDIO", "file": "x", "indexes": {}}]),
        ]
        for parser in cases:
            with self.subTest(parser=parser):
                with (
                    mock.patch.object(self.module, "find_generated_cues", return_value=[cue]),
                    mock.patch.object(self.module, "parse_generated_cue", parser),
                    self.assertRaisesRegex(RuntimeError, "Could not identify enough"),
                ):
                    self.module.identify_generated_audio_segments(Path("."), "x", [], 1, 1)

        track = {"number": 1, "type": "AUDIO", "file": None, "indexes": {1: 0}}
        with (
            mock.patch.object(self.module, "find_generated_cues", return_value=[cue]),
            mock.patch.object(self.module, "parse_generated_cue", return_value=[track]),
            self.assertRaisesRegex(RuntimeError, "additional sectors"),
        ):
            self.module.identify_generated_audio_segments(Path("."), "x", [], 1, 1)

        break_variants = [
            [{"number": 1, "type": "MODE1/2352", "file": "x", "indexes": {1: 0}}],
            [{"number": 1, "type": "AUDIO", "file": "x", "indexes": {1: 0}}],
        ]
        for index, tracks in enumerate(break_variants):
            with self.subTest(index=index):
                with (
                    mock.patch.object(self.module, "find_generated_cues", return_value=[cue]),
                    mock.patch.object(self.module, "parse_generated_cue", return_value=tracks),
                    mock.patch.object(self.module, "valid_audio_bin", return_value=False),
                    self.assertRaisesRegex(RuntimeError, "Could not identify enough"),
                ):
                    self.module.identify_generated_audio_segments(Path("."), "x", [], 1, 1)

        track = {"number": 1, "type": "AUDIO", "file": "x", "indexes": {1: 2}}
        with (
            mock.patch.object(self.module, "find_generated_cues", return_value=[cue]),
            mock.patch.object(self.module, "parse_generated_cue", return_value=[track]),
            mock.patch.object(self.module, "valid_audio_bin", return_value=True),
            mock.patch.object(self.module, "bin_sector_count", return_value=1),
            self.assertRaisesRegex(RuntimeError, "additional sectors"),
        ):
            self.module.identify_generated_audio_segments(Path("."), "x", [], 1, 1)

        audio = {"number": 1, "type": "AUDIO", "file": "x", "indexes": {1: 0}}
        data = {"number": 2, "type": "MODE1/2352", "file": "y", "indexes": {1: 0}}
        with (
            mock.patch.object(self.module, "find_generated_cues", return_value=[cue]),
            mock.patch.object(self.module, "parse_generated_cue", return_value=[audio, data]),
            mock.patch.object(self.module, "valid_audio_bin", return_value=True),
            mock.patch.object(self.module, "bin_sector_count", return_value=1),
            self.assertRaisesRegex(RuntimeError, "additional sectors"),
        ):
            self.module.identify_generated_audio_segments(Path("."), "x", [], 1, 2)

        following_audio = dict(audio, number=2, file="y")
        with (
            mock.patch.object(self.module, "find_generated_cues", return_value=[cue]),
            mock.patch.object(
                self.module, "parse_generated_cue", return_value=[audio, following_audio]
            ),
            mock.patch.object(self.module, "valid_audio_bin", return_value=True),
            mock.patch.object(self.module, "bin_sector_count", return_value=1),
            self.assertRaisesRegex(RuntimeError, "additional sectors"),
        ):
            self.module.identify_generated_audio_segments(
                Path("."), "x", [], 0, 2, track_zero_sectors=0
            )

        with (
            mock.patch.object(self.module, "find_generated_cues", return_value=[cue]),
            mock.patch.object(self.module, "parse_generated_cue", return_value=[audio]),
            mock.patch.object(self.module, "valid_audio_bin", return_value=True),
            mock.patch.object(self.module, "bin_sector_count", return_value=0),
            self.assertRaisesRegex(RuntimeError, "additional sectors"),
        ):
            self.module.identify_generated_audio_segments(Path("."), "x", [], 1, 1)

    def test_data_track_discovery_failures(self):
        cue = Path("disc.cue")
        with mock.patch.object(self.module, "find_generated_cues", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "generated CUE"):
                self.module.identify_generated_data_track(Path("."), "x", [], 1)

        variants = [
            [],
            [{"number": 1, "type": "AUDIO", "file": "x", "indexes": {1: 0}}],
            [{"number": 1, "type": "MODE0/2352", "file": "x", "indexes": {1: 0}}],
            [{"number": 1, "type": "MODE1/2352", "file": "x", "indexes": {}}],
            [{"number": 1, "type": "MODE1/2352", "file": None, "indexes": {1: 0}}],
            [
                {"number": 1, "type": "MODE1/2352", "file": "x", "indexes": {1: 0}},
                {"number": 2, "type": "AUDIO", "file": "x", "indexes": {1: 0}},
            ],
        ]
        for tracks in variants:
            with self.subTest(tracks=tracks):
                with (
                    mock.patch.object(self.module, "find_generated_cues", return_value=[cue]),
                    mock.patch.object(self.module, "parse_generated_cue", return_value=tracks),
                    self.assertRaisesRegex(RuntimeError, "Could not identify split data"),
                ):
                    self.module.identify_generated_data_track(Path("."), "x", [], 1)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cue_path = root / "disc.cue"
            cue_path.write_text("", encoding="utf-8")
            track = {
                "number": 1, "type": "MODE1/2352", "file": "data.bin",
                "indexes": {1: 1},
            }
            with (
                mock.patch.object(self.module, "find_generated_cues", return_value=[cue_path]),
                mock.patch.object(self.module, "parse_generated_cue", return_value=[track]),
                self.assertRaisesRegex(RuntimeError, "Could not read data BIN"),
            ):
                self.module.identify_generated_data_track(root, "x", [], 1)
            for contents, message in ((b"", "invalid size"), (b"x", "invalid size")):
                (root / "data.bin").write_bytes(contents)
                with (
                    mock.patch.object(self.module, "find_generated_cues", return_value=[cue_path]),
                    mock.patch.object(self.module, "parse_generated_cue", return_value=[track]),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    self.module.identify_generated_data_track(root, "x", [], 1)
            (root / "data.bin").write_bytes(bytes(self.module.SECTOR_SIZE))
            with (
                mock.patch.object(self.module, "find_generated_cues", return_value=[cue_path]),
                mock.patch.object(self.module, "parse_generated_cue", return_value=[track]),
                self.assertRaisesRegex(RuntimeError, "outside its BIN"),
            ):
                self.module.identify_generated_data_track(root, "x", [], 1)
