"""Tests for raw data-sector conversion and ISO9660 validation."""

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

from redumper_cdda import iso9660


class Iso9660CoverageTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.reload(iso9660)

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

    def test_iso_payload_validation(self):
        with self.assertRaisesRegex(RuntimeError, "Short MODE1/2048"):
            self.module.extract_iso_payload(b"", "MODE1/2048")
        with self.assertRaisesRegex(RuntimeError, "Short MODE1/2352"):
            self.module.extract_iso_payload(b"", "MODE1/2352")
        raw = bytearray(self.module.SECTOR_SIZE)
        with self.assertRaisesRegex(RuntimeError, "sync pattern"):
            self.module.extract_iso_payload(raw, "MODE1/2352")
        raw[:12] = b"\0" + b"\xff" * 10 + b"\0"
        with self.assertRaisesRegex(RuntimeError, "wrong mode"):
            self.module.extract_iso_payload(raw, "MODE1/2352")
        raw[15] = 2
        raw[16:20] = b"abcd"
        raw[20:24] = b"wxyz"
        with self.assertRaisesRegex(RuntimeError, "mismatched subheaders"):
            self.module.extract_iso_payload(raw, "MODE2/2352")
        raw[15] = 1
        payload = self.module.extract_iso_payload(raw, "MODE1/2352")
        self.assertEqual(len(payload), self.module.ISO_SECTOR_SIZE)
        raw[15] = 0
        with self.assertRaisesRegex(RuntimeError, "wrong mode"):
            self.module.extract_iso_payload(raw, "MODE2/2352")
        with self.assertRaisesRegex(RuntimeError, "Unsupported"):
            self.module.extract_iso_payload(raw, "MODE3/2352")

    def test_iso9660_validation_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.iso"
            path.write_bytes(bytes(16 * self.module.ISO_SECTOR_SIZE))
            with self.assertRaisesRegex(RuntimeError, "too short"):
                self.module.read_iso9660_volume_size(path, 16)

            def write(descriptor, sectors=20):
                data = bytearray(sectors * self.module.ISO_SECTOR_SIZE)
                data[16 * self.module.ISO_SECTOR_SIZE:17 * self.module.ISO_SECTOR_SIZE] = descriptor
                path.write_bytes(data)

            invalid = bytearray(self.module.ISO_SECTOR_SIZE)
            write(invalid)
            with self.assertRaisesRegex(RuntimeError, "invalid.*descriptor"):
                self.module.read_iso9660_volume_size(path, 20)

            terminator = bytearray(self.module.ISO_SECTOR_SIZE)
            terminator[0] = 255
            terminator[1:6] = b"CD001"
            terminator[6] = 1
            write(terminator)
            with self.assertRaisesRegex(RuntimeError, "no ISO9660 primary"):
                self.module.read_iso9660_volume_size(path, 20)

            supplementary = bytearray(terminator)
            supplementary[0] = 2
            data = bytearray(20 * self.module.ISO_SECTOR_SIZE)
            data[16 * self.module.ISO_SECTOR_SIZE:17 * self.module.ISO_SECTOR_SIZE] = supplementary
            pvd = self.make_pvd()
            data[17 * self.module.ISO_SECTOR_SIZE:18 * self.module.ISO_SECTOR_SIZE] = pvd
            path.write_bytes(data)
            self.assertEqual(self.module.read_iso9660_volume_size(path, 20), 20)

            data = bytearray(20 * self.module.ISO_SECTOR_SIZE)
            for sector in range(16, 20):
                data[
                    sector * self.module.ISO_SECTOR_SIZE:
                    (sector + 1) * self.module.ISO_SECTOR_SIZE
                ] = supplementary
            path.write_bytes(data)
            with self.assertRaisesRegex(RuntimeError, "no ISO9660 primary"):
                self.module.read_iso9660_volume_size(path, 20)

            path.write_bytes(bytes(16 * self.module.ISO_SECTOR_SIZE))
            with self.assertRaisesRegex(RuntimeError, "Short ISO9660"):
                self.module.read_iso9660_volume_size(path, 17)

            cases = []
            pvd = self.make_pvd(); pvd[84:88] = (21).to_bytes(4, "big")
            cases.append((pvd, "volume-space"))
            cases.append((self.make_pvd(block_size=1024), "block size"))
            cases.append((self.make_pvd(volume=21), "requires more"))
            pvd = self.make_pvd(); pvd[156] = 0
            cases.append((pvd, "root directory record"))
            pvd = self.make_pvd(); pvd[162:166] = (19).to_bytes(4, "big")
            cases.append((pvd, "root directory extent"))
            cases.append((self.make_pvd(root_extent=19, root_size=4096), "outside the volume"))
            for descriptor, message in cases:
                with self.subTest(message=message):
                    write(descriptor)
                    with self.assertRaisesRegex(RuntimeError, message):
                        self.module.read_iso9660_volume_size(path, 20)

    def test_data_track_short_read_and_verbose_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data.bin"
            source.write_bytes(b"short")
            data_track = {
                "track": 1, "track_type": "MODE1/2048",
                "sector_size": self.module.ISO_SECTOR_SIZE,
                "start_sector": 0, "sectors": 1, "path": source,
            }
            with self.assertRaisesRegex(RuntimeError, "Unexpected end"):
                self.module.data_track_to_iso(data_track, root / "bad.iso")

            source.write_bytes(bytes(self.module.ISO_SECTOR_SIZE))
            with (
                mock.patch.object(self.module, "read_iso9660_volume_size", return_value=1),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.module.data_track_to_iso(data_track, root / "ok.iso", verbose=True)
            self.assertIn("ISO conversion", output.getvalue())
