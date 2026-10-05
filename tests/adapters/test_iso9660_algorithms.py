"""Adapter tests for raw-sector conversion and ISO9660 validation."""

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
    def test_mode2_form1_and_form2_return_the_first_logical_block(self):
        sector = bytearray(self.module.SECTOR_SIZE)
        sector[:12] = b"\0" + b"\xff" * 10 + b"\0"
        sector[15] = 2
        sector[16:20] = b"\0\0\0\0"
        sector[20:24] = b"\0\0\0\0"
        payload = bytes(index % 251 for index in range(2324))
        sector[24:24 + len(payload)] = payload

        self.assertEqual(
            self.module.extract_iso_payload(bytes(sector), "MODE2/2352"),
            payload[:self.module.ISO_SECTOR_SIZE],
        )

        sector[18] |= 0x20
        sector[22] = sector[18]
        self.assertEqual(
            self.module.extract_iso_payload(bytes(sector), "MODE2/2352"),
            payload[:self.module.ISO_SECTOR_SIZE],
        )

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

    def make_directory_record(self, extent, size, flags, identifier):
        length = 33 + len(identifier) + (len(identifier) % 2 == 0)
        record = bytearray(length)
        record[0] = length
        record[2:10] = (
            extent.to_bytes(4, "little") + extent.to_bytes(4, "big")
        )
        record[10:18] = size.to_bytes(4, "little") + size.to_bytes(4, "big")
        record[25] = flags
        record[28:32] = b"\x01\x00\x00\x01"
        record[32] = len(identifier)
        record[33:33 + len(identifier)] = identifier
        return record

    def test_rebases_multisession_iso_metadata(self):
        base = 100
        sectors = 30
        image = bytearray(sectors * self.module.ISO_SECTOR_SIZE)
        pvd = self.make_pvd(volume=base + sectors, root_extent=base + 20)
        pvd[132:140] = (12).to_bytes(4, "little") + (12).to_bytes(4, "big")
        pvd[140:144] = (base + 19).to_bytes(4, "little")
        pvd[148:152] = (base + 22).to_bytes(4, "big")
        svd = bytearray(pvd)
        svd[0] = 2
        svd[144:148] = (base + 19).to_bytes(4, "little")
        svd[152:156] = (base + 22).to_bytes(4, "big")
        terminator = bytearray(self.module.ISO_SECTOR_SIZE)
        terminator[0] = 255
        terminator[1:6] = b"CD001"
        terminator[6] = 1
        for sector, descriptor in ((16, pvd), (17, svd), (18, terminator)):
            start = sector * self.module.ISO_SECTOR_SIZE
            image[start:start + self.module.ISO_SECTOR_SIZE] = descriptor

        little_path = b"\x01\0" + (base + 20).to_bytes(4, "little") + b"\x01\0\0\0"
        big_path = b"\x01\0" + (base + 20).to_bytes(4, "big") + b"\0\x01\0\0"
        image[19 * 2048:19 * 2048 + 10] = little_path
        image[22 * 2048:22 * 2048 + 10] = big_path
        records = b"".join(
            (
                self.make_directory_record(base + 20, 2048, 2, b"\0"),
                self.make_directory_record(base + 20, 2048, 2, b"\1"),
                self.make_directory_record(base + 23, 2048, 2, b"DIR"),
                self.make_directory_record(base + 21, 4, 0, b"FILE;1"),
            )
        )
        image[20 * 2048:20 * 2048 + len(records)] = records
        image[21 * 2048:21 * 2048 + 4] = b"data"
        child = self.make_directory_record(base + 23, 2048, 2, b"\0")
        image[23 * 2048:23 * 2048 + len(child)] = child

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.iso"
            path.write_bytes(image)
            self.module.rebase_iso9660_session(path, base, sectors)

            result = path.read_bytes()
            descriptor = result[16 * 2048:17 * 2048]
            self.assertEqual(int.from_bytes(descriptor[80:84], "little"), sectors)
            self.assertEqual(int.from_bytes(descriptor[158:162], "little"), 20)
            self.assertEqual(int.from_bytes(descriptor[140:144], "little"), 19)
            self.assertEqual(int.from_bytes(result[19 * 2048 + 2:19 * 2048 + 6], "little"), 20)
            root = result[20 * 2048:21 * 2048]
            self.assertEqual(int.from_bytes(root[2:6], "little"), 20)
            third = root[68:]
            self.assertEqual(int.from_bytes(third[2:6], "little"), 23)
            self.assertEqual(
                self.module.read_iso9660_volume_size(path, sectors), sectors
            )

    def test_multisession_rebase_validation_failures(self):
        with self.assertRaisesRegex(RuntimeError, "outside"):
            self.module._relative_lba(9, 10, 20)

        with self.assertRaisesRegex(RuntimeError, "Short.*path"):
            self.module._rebase_path_table(io.BytesIO(), 0, 1, "little", 10, 20)
        self.module._rebase_path_table(io.BytesIO(), 0, 0, "little", 10, 20)
        padded = io.BytesIO(b"\0\0")
        self.module._rebase_path_table(padded, 0, 2, "little", 10, 20)
        self.assertEqual(padded.getvalue(), b"\0\0")
        with self.assertRaisesRegex(RuntimeError, "Malformed.*path"):
            self.module._rebase_path_table(
                io.BytesIO(b"\0x"), 0, 2, "little", 10, 20
            )
        with self.assertRaisesRegex(RuntimeError, "Malformed.*path"):
            self.module._rebase_path_table(
                io.BytesIO(b"\x08" + b"\0" * 8), 0, 9, "little", 10, 20
            )

        with self.assertRaisesRegex(RuntimeError, "Short.*directory"):
            self.module._rebase_directories(
                io.BytesIO(), ((10, 1),), 10, 20
            )
        malformed = bytearray(34)
        malformed[0] = 33
        with self.assertRaisesRegex(RuntimeError, "Malformed.*directory"):
            self.module._rebase_directories(
                io.BytesIO(malformed), ((10, 34),), 10, 20
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.iso"

            def write(descriptor, sectors=20, terminator=True):
                data = bytearray(sectors * 2048)
                data[16 * 2048:17 * 2048] = descriptor
                if terminator and sectors > 17:
                    end = bytearray(2048)
                    end[0] = 255
                    end[1:6] = b"CD001"
                    end[6] = 1
                    data[17 * 2048:18 * 2048] = end
                path.write_bytes(data)

            path.write_bytes(bytes(16 * 2048))
            with self.assertRaisesRegex(RuntimeError, "Short.*descriptor"):
                self.module.rebase_iso9660_session(path, 10, 17)

            invalid = bytearray(2048)
            write(invalid)
            with self.assertRaisesRegex(RuntimeError, "invalid.*descriptor"):
                self.module.rebase_iso9660_session(path, 10, 20)

            unknown = bytearray(2048)
            unknown[0] = 3
            unknown[1:6] = b"CD001"
            unknown[6] = 1
            write(unknown)
            with self.assertRaisesRegex(RuntimeError, "no supported"):
                self.module.rebase_iso9660_session(path, 10, 20)

            cases = []
            pvd = self.make_pvd(volume=30, root_extent=28)
            pvd[84:88] = (31).to_bytes(4, "big")
            cases.append((pvd, "volume-space"))
            pvd = self.make_pvd(volume=30, root_extent=28)
            pvd[136:140] = (1).to_bytes(4, "big")
            cases.append((pvd, "path-table"))
            pvd = self.make_pvd(volume=30, root_extent=28)
            pvd[162:166] = (29).to_bytes(4, "big")
            cases.append((pvd, "root directory"))
            for descriptor, message in cases:
                with self.subTest(message=message):
                    write(descriptor)
                    with self.assertRaisesRegex(RuntimeError, message):
                        self.module.rebase_iso9660_session(path, 10, 20)

            write(self.make_pvd(volume=10, root_extent=18))
            with self.assertRaisesRegex(RuntimeError, "rebased volume"):
                self.module.rebase_iso9660_session(path, 10, 20, 5)

            relative = self.make_pvd(volume=20, root_extent=18)
            write(relative)
            self.module.rebase_iso9660_session(path, 10, 20, 20)
            self.assertEqual(
                path.read_bytes()[16 * 2048:17 * 2048], relative
            )

            pvd = self.make_pvd(volume=27, root_extent=26)
            write(pvd, sectors=17, terminator=False)
            with mock.patch.object(self.module, "_rebase_directories"):
                self.module.rebase_iso9660_session(path, 10, 17)

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
            data_track["track_sectors"] = 2
            with (
                mock.patch.object(self.module, "read_iso9660_volume_size", return_value=1),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.module.data_track_to_iso(data_track, root / "ok.iso", verbose=True)
            self.assertIn("ISO conversion", output.getvalue())
            self.assertEqual(
                (root / "ok.iso").stat().st_size, self.module.ISO_SECTOR_SIZE
            )
            with mock.patch.object(
                self.module, "read_iso9660_volume_size", return_value=1
            ):
                self.module.data_track_to_iso(data_track, root / "quiet.iso")
