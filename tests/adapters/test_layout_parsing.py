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

if __name__ == "__main__":
    unittest.main()
