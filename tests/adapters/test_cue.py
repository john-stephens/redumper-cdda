import tempfile
import unittest
from pathlib import Path
from unittest import mock

from redumper_cdda.adapters.cue import (
    AudioSegmentResolver,
    CueLocator,
    CueSheetParser,
    DataTrackResolver,
    ISO_SECTOR_SIZE,
    SECTOR_SIZE,
)
from redumper_cdda.domain.errors import CueError
from redumper_cdda.domain.outputs import CueTrack


class CueAdapterTests(unittest.TestCase):
    class Locator:
        def __init__(self, paths):
            self.paths = paths

        def locate(self, *_args):
            return self.paths

    class Parser:
        def __init__(self, tracks):
            self.tracks = tracks

        def parse(self, _path):
            return self.tracks

    def test_real_cues_resolve_to_typed_sources_at_index01(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_bin = root / "audio.bin"
            audio_bin.write_bytes(bytes(2 * SECTOR_SIZE))
            audio_cue = root / "audio.cue"
            audio_cue.write_text(
                'FILE "audio.bin" BINARY\n'
                '  TRACK 01 AUDIO\n'
                '    INDEX 00 00:00:00\n'
                '    INDEX 01 00:00:01\n',
                encoding="utf-8",
            )
            locator = CueLocator()
            parser = CueSheetParser()
            segments, cue_path, skipped = AudioSegmentResolver(
                locator, parser
            ).resolve(root, "audio", [audio_cue], 1, 1)
            self.assertEqual(cue_path, audio_cue)
            self.assertEqual(segments[0].start_sector, 1)
            self.assertEqual(skipped, 1)

            data_bin = root / "data.bin"
            data_bin.write_bytes(bytes(2 * SECTOR_SIZE))
            data_cue = root / "data.cue"
            data_cue.write_text(
                'FILE "data.bin" BINARY\n'
                '  TRACK 02 MODE1/2352\n'
                '    INDEX 01 00:00:01\n',
                encoding="utf-8",
            )
            source = DataTrackResolver(locator, parser).resolve(
                root, "data", [data_cue], 2
            )
            self.assertEqual(source.start_sector, 1)
            self.assertEqual(source.sectors, 1)

    def test_parser_blank_unknown_and_encoding_fallback(self):
        parser = CueSheetParser()
        path = mock.Mock()
        path.read_text.side_effect = (UnicodeDecodeError("x", b"", 0, 1, "x"), "\nREM x\nINDEX 01 00:00:01\n")
        self.assertEqual(parser.parse(path), ())
        self.assertEqual(path.read_text.call_count, 2)
        self.assertEqual(parser.msf_to_sectors(1, 2, 3), 4653)
        parser.read_text = lambda _path: 'FILE "x" BINARY\nTRACK 01 AUDIO\nREM ignored\n'
        self.assertEqual(parser.parse(Path("x"))[0].number, 1)

    def test_locator_fallback_and_newest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = root / "disc-old.cue"
            newer = root / "disc-new.cue"
            ignored = root / "other.cue"
            for path in (older, newer, ignored):
                path.write_text("")
            older.touch()
            newer.touch()
            changed = CueLocator().locate(root, "disc", [ignored, newer])
            self.assertEqual(set(changed), {ignored, newer})
            fallback = CueLocator().locate(root, "disc", [])
            self.assertEqual(set(fallback), {older, newer})

    def test_audio_resolution_failures_and_multi_bin_range(self):
        cue = Path("disc.cue")
        empty = AudioSegmentResolver(self.Locator(()), self.Parser(()))
        with self.assertRaisesRegex(CueError, "generated CUE"):
            empty.resolve(Path("."), "disc", [], 1, 1)

        missing = AudioSegmentResolver(self.Locator((cue,)), self.Parser(()))
        with self.assertRaisesRegex(CueError, "not found as AUDIO"):
            missing.resolve(Path("."), "disc", [], 1, 1)

        no_index = CueTrack(1, "AUDIO", "one.bin", ())
        resolver = AudioSegmentResolver(self.Locator((cue,)), self.Parser((no_index,)))
        with self.assertRaisesRegex(CueError, "no INDEX 01"):
            resolver.resolve(Path("."), "disc", [], 1, 1)

        tracks = (
            CueTrack(1, "AUDIO", "one.bin", ((1, 1),)),
            CueTrack(2, "AUDIO", "two.bin", ((1, 0),)),
        )
        resolver = AudioSegmentResolver(
            self.Locator((cue,)), self.Parser(tracks), bin_sectors=lambda _path: 3
        )
        segments, _path, skipped = resolver.resolve(Path("."), "disc", [], 1, 4)
        self.assertEqual([item.sectors for item in segments], [2, 2])
        self.assertEqual(skipped, 1)

        with self.assertRaisesRegex(CueError, "expected 2"):
            resolver.resolve(Path("."), "disc", [], 0, 2, track_zero_sectors=2)

    def test_audio_invalid_sources_and_insufficient_data(self):
        cue = Path("disc.cue")
        cases = (
            (CueTrack(1, "MODE1/2352", "one.bin", ((1, 0),)), lambda _path: 1),
            (CueTrack(1, "AUDIO", None, ((1, 0),)), lambda _path: 1),
            (CueTrack(1, "AUDIO", "one.bin", ((1, 0),)), lambda _path: None),
            (CueTrack(1, "AUDIO", "one.bin", ((1, 2),)), lambda _path: 1),
            (CueTrack(1, "AUDIO", "one.bin", ((1, 1),)), lambda _path: 1),
        )
        for track, size in cases:
            with self.subTest(track=track):
                resolver = AudioSegmentResolver(
                    self.Locator((cue,)), self.Parser((track,)), bin_sectors=size
                )
                with self.assertRaisesRegex(CueError, "additional sectors|not found"):
                    resolver.resolve(Path("."), "disc", [], 1, 1)

        track_zero = (
            CueTrack(1, "AUDIO", "one.bin", ((1, 2),)),
            CueTrack(2, "AUDIO", "two.bin", ((1, 0),)),
        )
        resolver = AudioSegmentResolver(
            self.Locator((cue,)), self.Parser(track_zero), bin_sectors=lambda _path: 2
        )
        with self.assertRaisesRegex(CueError, "additional sectors"):
            resolver.resolve(Path("."), "disc", [], 0, 3, track_zero_sectors=2)

    def test_audio_file_size_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(AudioSegmentResolver._audio_bin_sectors(root / "missing"))
            bad = root / "bad.bin"
            bad.write_bytes(b"x")
            self.assertIsNone(AudioSegmentResolver._audio_bin_sectors(bad))
            empty = root / "empty.bin"
            empty.write_bytes(b"")
            self.assertIsNone(AudioSegmentResolver._audio_bin_sectors(empty))

    def test_data_resolution_failures_and_modes(self):
        cue = Path("disc.cue")
        with self.assertRaisesRegex(CueError, "generated CUE"):
            DataTrackResolver(self.Locator(()), self.Parser(())).resolve(Path("."), "x", [], 1)
        cases = (
            ((), "was not found"),
            ((CueTrack(1, "AUDIO", "x", ((1, 0),)),), "AUDIO"),
            ((CueTrack(1, "MODE1/2352", None, ((1, 0),)),), "no BIN"),
            ((CueTrack(1, "UNKNOWN", "x", ((1, 0),)),), "Unsupported"),
        )
        for tracks, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(CueError, message):
                DataTrackResolver(self.Locator((cue,)), self.Parser(tracks)).resolve(Path("."), "x", [], 1)
        self.assertEqual(DataTrackResolver.sector_size("MODE1/2352"), SECTOR_SIZE)
        self.assertEqual(DataTrackResolver.sector_size("MODE2/2352"), SECTOR_SIZE)
        self.assertEqual(DataTrackResolver.sector_size("MODE1/2048"), ISO_SECTOR_SIZE)

    def test_data_shared_missing_invalid_and_outside_bin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cue = root / "disc.cue"
            selected = CueTrack(1, "MODE1/2048", "data.bin", ((1, 0),))
            shared = CueTrack(2, "MODE1/2048", "data.bin", ((1, 0),))
            with self.assertRaisesRegex(CueError, "shares"):
                DataTrackResolver(self.Locator((cue,)), self.Parser((selected, shared))).resolve(root, "x", [], 1)
            resolver = DataTrackResolver(self.Locator((cue,)), self.Parser((selected,)))
            with self.assertRaisesRegex(CueError, "Could not read"):
                resolver.resolve(root, "x", [], 1)
            data = root / "data.bin"
            data.write_bytes(b"x")
            with self.assertRaisesRegex(CueError, "invalid size"):
                resolver.resolve(root, "x", [], 1)
            data.write_bytes(bytes(ISO_SECTOR_SIZE))
            outside = CueTrack(1, "MODE1/2048", "data.bin", ((1, 1),))
            with self.assertRaisesRegex(CueError, "outside"):
                DataTrackResolver(self.Locator((cue,)), self.Parser((outside,))).resolve(root, "x", [], 1)


if __name__ == "__main__":
    unittest.main()
