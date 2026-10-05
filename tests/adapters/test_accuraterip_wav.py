"""Adapter tests for AccurateRip temporary WAV assembly."""

import importlib
import io
import subprocess
import sys
import tempfile
import unittest
import wave
from argparse import Namespace
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock

SRC_PATH = Path(__file__).parent.parent / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from redumper_cdda import outputs


class OutputCoverageTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.reload(outputs)

    def make_pvd(self, volume=20, block_size=2048, root_extent=18, root_size=2048):
        pvd = bytearray(self.module.ISO_SECTOR_SIZE)
        pvd[0] = 1
        pvd[1:6] = b"CD001"
        pvd[6] = 1
        pvd[80:84] = volume.to_bytes(4, "little")
        pvd[84:88] = volume.to_bytes(4, "big")
        pvd[128:130] = block_size.to_bytes(2, "little")
        pvd[130:132] = block_size.to_bytes(2, "big")
        pvd[156] = 34
        pvd[158:162] = root_extent.to_bytes(4, "little")
        pvd[162:166] = root_extent.to_bytes(4, "big")
        pvd[166:170] = root_size.to_bytes(4, "little")
        pvd[170:174] = root_size.to_bytes(4, "big")
        pvd[181] = 2
        return pvd

    def test_wav_conversion_validation_and_verbose_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "audio.bin"
            source.write_bytes(bytes(self.module.SECTOR_SIZE))
            segment = {
                "path": source, "track": 1, "start_sector": 0,
                "sectors": 1, "bin_sectors": 1,
            }
            with self.assertRaisesRegex(RuntimeError, "sector count"):
                self.module.segments_to_wav([segment], root / "x.wav", 2)
            with redirect_stdout(io.StringIO()) as output:
                self.module.segments_to_wav([segment], root / "x.wav", 1, verbose=True)
            self.assertIn("WAV conversion", output.getvalue())

            source.write_bytes(b"short")
            with self.assertRaisesRegex(RuntimeError, "Unexpected end"):
                self.module.segments_to_wav([segment], root / "bad.wav", 1)

    def test_wav_conversion_compensates_for_split_write_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def make_segment(name, first_frame):
                path = root / name
                frames = b"".join(
                    value.to_bytes(4, "little")
                    for value in range(
                        first_frame,
                        first_frame + self.module.FRAMES_PER_SECTOR,
                    )
                )
                path.write_bytes(frames)
                return {
                    "path": path,
                    "track": 1,
                    "start_sector": 0,
                    "sectors": 1,
                    "bin_sectors": 1,
                }

            previous = make_segment("previous.bin", 0)
            current = make_segment("current.bin", self.module.FRAMES_PER_SECTOR)
            following = make_segment(
                "following.bin", self.module.FRAMES_PER_SECTOR * 2
            )

            negative = root / "negative.wav"
            self.module.segments_to_wav(
                [current], negative, 1, write_offset=-2,
                following_segments=[following],
            )
            with wave.open(str(negative), "rb") as stream:
                payload = stream.readframes(self.module.FRAMES_PER_SECTOR)
            self.assertEqual(int.from_bytes(payload[:4], "little"), 590)
            self.assertEqual(int.from_bytes(payload[-4:], "little"), 1177)

            positive = root / "positive.wav"
            self.module.segments_to_wav(
                [current], positive, 1, write_offset=2,
                preceding_segments=[previous],
            )
            with wave.open(str(positive), "rb") as stream:
                payload = stream.readframes(self.module.FRAMES_PER_SECTOR)
            self.assertEqual(int.from_bytes(payload[:4], "little"), 586)
            self.assertEqual(int.from_bytes(payload[-4:], "little"), 1173)

            with self.assertRaisesRegex(RuntimeError, "Insufficient adjacent"):
                self.module.segments_to_wav(
                    [current], root / "missing.wav", 1, write_offset=-1
                )

            destination = mock.Mock()
            self.module._write_segment_window(
                destination,
                [previous, current],
                self.module.FRAMES_PER_SECTOR,
                1,
            )
            self.assertEqual(
                int.from_bytes(destination.writeframesraw.call_args.args[0], "little"),
                self.module.FRAMES_PER_SECTOR,
            )
            with self.assertRaisesRegex(RuntimeError, "Insufficient adjacent"):
                self.module._write_segment_window(destination, [], 0, 1)
