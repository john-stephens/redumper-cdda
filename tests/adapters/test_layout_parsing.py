"""Adapter tests for external disc-layout parsing."""

import sys
import unittest
from pathlib import Path

SRC_PATH = Path(__file__).parent.parent / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from redumper_cdda import layout


class LayoutCoverageTests(unittest.TestCase):
    def setUp(self):
        self.module = layout

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

    def test_cdparanoia_parse(self):
        text = "header\n 1.  75 [00:01.00]  150 [00:02.00]\n"
        self.assertEqual(self.module.parse_cdparanoia_toc(text)[0]["end"], 225)
        with self.assertRaisesRegex(RuntimeError, "Could not parse"):
            self.module.parse_cdparanoia_toc("garbage")

    @staticmethod
    def full_descriptor(session, point, minute=0, second=0, frame=0, adr=1):
        return bytes(
            (session, adr << 4, 0, point, 0, 0, 0, 0, minute, second, frame)
        )

    @classmethod
    def full_toc(cls, descriptors):
        body = b"".join(descriptors)
        return (len(body) + 2).to_bytes(2, "big") + b"\x01\x02" + body

    def test_full_toc_applies_per_session_leadout(self):
        standard = self.module.parse_mmc_toc(
            self.toc(
                1,
                2,
                [
                    self.descriptor(1, 0),
                    self.descriptor(2, 200, control=4),
                    self.descriptor(0xAA, 300, control=4),
                ],
            )
        )
        full = self.module.parse_mmc_full_toc(
            self.full_toc(
                [
                    self.full_descriptor(1, 0xB0),
                    self.full_descriptor(1, 1, adr=2),
                    self.full_descriptor(1, 1),
                    self.full_descriptor(1, 0xA2, second=3, frame=25),
                    self.full_descriptor(2, 2),
                    self.full_descriptor(2, 0xA2, second=6),
                ]
            )
        )

        adjusted = self.module.apply_session_boundaries(standard, full)

        self.assertEqual(adjusted[0]["end"], 100)
        self.assertEqual(adjusted[0]["length"], 100)
        self.assertEqual(adjusted[1]["end"], 300)

    def test_full_toc_validation_failures(self):
        parse = self.module.parse_mmc_full_toc
        for data, message in (
            (b"bad", "shorter"),
            (b"\x00\x20\x01\x01", "truncated"),
            (b"\x00\x03\x01\x01x", "malformed"),
            (
                self.full_toc([self.full_descriptor(1, 0xA2)]),
                "no track",
            ),
            (
                self.full_toc([self.full_descriptor(1, 1)]),
                "missing lead-out",
            ),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                RuntimeError, message
            ):
                parse(data)

        conflicting_track = self.full_toc(
            [
                self.full_descriptor(1, 1),
                self.full_descriptor(2, 1),
                self.full_descriptor(1, 0xA2),
                self.full_descriptor(2, 0xA2),
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "multiple sessions"):
            parse(conflicting_track)
        conflicting_leadout = self.full_toc(
            [
                self.full_descriptor(1, 1),
                self.full_descriptor(1, 0xA2, second=1),
                self.full_descriptor(1, 0xA2, second=2),
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "conflicting"):
            parse(conflicting_leadout)

    def test_session_boundary_validation_failures(self):
        tracks = [
            {"number": 1, "begin": 0, "end": 100, "length": 100,
             "length_msf": "00:01:25"},
            {"number": 2, "begin": 100, "end": 200, "length": 100,
             "length_msf": "00:01:25"},
        ]
        with self.assertRaisesRegex(RuntimeError, "missing Track"):
            self.module.apply_session_boundaries(tracks, ({1: 1}, {1: 100}))
        with self.assertRaisesRegex(RuntimeError, "out of order"):
            self.module.apply_session_boundaries(
                tracks, ({1: 2, 2: 1}, {1: 200, 2: 100})
            )
        with self.assertRaisesRegex(RuntimeError, "invalid end"):
            self.module.apply_session_boundaries(
                tracks, ({1: 1, 2: 2}, {1: 101, 2: 200})
            )

if __name__ == "__main__":
    unittest.main()
