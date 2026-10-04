import unittest

from redumper_cdda.domain.errors import DomainModelError
from redumper_cdda.domain.integrity import (
    MediaErrors,
    TrackMediaErrors,
    WriteOffsetMap,
)


class IntegrityDomainTests(unittest.TestCase):
    def test_media_errors(self):
        self.assertFalse(MediaErrors(0, 0, 3).has_data_errors)
        self.assertFalse(MediaErrors(0, 0, None).has_data_errors)
        self.assertTrue(MediaErrors(1, 0, 0).has_data_errors)
        self.assertTrue(MediaErrors(0, 1, 0).has_data_errors)
        with self.assertRaisesRegex(DomainModelError, "negative"):
            MediaErrors(-1, 0, 0)

        self.assertFalse(TrackMediaErrors(0, 0, 0, 0).has_data_errors)
        self.assertTrue(TrackMediaErrors(1, 0, 1, 0).has_data_errors)
        self.assertTrue(TrackMediaErrors(0, 1, 0, 1).has_data_errors)

    def test_write_offset_map(self):
        offsets = WriteOffsetMap(((0, 4), (10, 8)))
        self.assertEqual(offsets.offset_for_lba(-1), 4)
        self.assertEqual(offsets.offset_for_lba(5), 4)
        self.assertEqual(offsets.offset_for_lba(10), 8)

        with self.assertRaisesRegex(DomainModelError, "cannot be empty"):
            WriteOffsetMap(())
        with self.assertRaisesRegex(DomainModelError, "sorted"):
            WriteOffsetMap(((10, 1), (0, 2)))
