import unittest

from redumper_cdda.domain.disc import AudioLayout, DiscLayout, Track, TrackKind
from redumper_cdda.domain.errors import DomainModelError, SelectionError


class DiscDomainTests(unittest.TestCase):
    @staticmethod
    def track(number=1, kind=TrackKind.AUDIO, begin=0, end=10):
        return Track(number, kind, 0, begin, end)

    def test_track_and_layout_queries(self):
        audio = self.track()
        data = self.track(2, TrackKind.DATA, 10, 20)
        layout = DiscLayout((audio, data), 20)

        self.assertEqual(audio.length_sectors, 10)
        self.assertIs(layout.track(2), data)
        self.assertEqual(layout.audio_tracks(), (audio,))

        with self.assertRaisesRegex(SelectionError, "Track 3"):
            layout.track(3)

    def test_domain_invariants(self):
        with self.assertRaisesRegex(DomainModelError, "non-positive"):
            self.track(end=0)
        with self.assertRaisesRegex(DomainModelError, "at least one"):
            DiscLayout((), 0)
        with self.assertRaisesRegex(DomainModelError, "lead-out"):
            DiscLayout((self.track(),), 11)

        self.assertEqual(AudioLayout((self.track(),)).tracks[0].number, 1)
        with self.assertRaisesRegex(DomainModelError, "at least one"):
            AudioLayout(())
