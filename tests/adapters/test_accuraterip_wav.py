"""Adapter tests for AccurateRip temporary WAV assembly."""

import importlib
import io
import subprocess
import sys
import tempfile
import unittest
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
