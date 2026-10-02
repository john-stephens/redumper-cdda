import unittest

from redumper_cdda.adapters.layout_provider import ReconciledLayoutProvider
from redumper_cdda.domain.disc import AudioLayout, DiscLayout, Track, TrackKind
from redumper_cdda.domain.errors import LayoutMismatchError
from redumper_cdda.ports import layout as layout_ports


class FakeReader:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def read(self, device):
        self.calls.append(device)
        return self.value


class ReconciledLayoutProviderTests(unittest.TestCase):
    @staticmethod
    def track(number, kind, begin, end, control=0, length_msf="", begin_msf=""):
        return Track(number, kind, control, begin, end, length_msf, begin_msf)

    def test_data_only_layout_does_not_query_cdparanoia(self):
        self.assertTrue(hasattr(layout_ports, "LayoutProvider"))
        data = self.track(1, TrackKind.DATA, 0, 100, control=4)
        mmc = DiscLayout((data,), 100)
        mmc_reader = FakeReader(mmc)
        audio_reader = FakeReader(None)

        result = ReconciledLayoutProvider(mmc_reader, audio_reader).read("/dev/sr0")

        self.assertIs(result, mmc)
        self.assertEqual(mmc_reader.calls, ["/dev/sr0"])
        self.assertEqual(audio_reader.calls, [])

    def test_reconciles_audio_boundaries_and_preserves_data(self):
        mmc_audio = self.track(1, TrackKind.AUDIO, 0, 100, control=1)
        data = self.track(2, TrackKind.DATA, 100, 200, control=4)
        audio = self.track(
            1, TrackKind.AUDIO, 0, 100,
            length_msf="00:01.25", begin_msf="00:00.00",
        )
        audio_reader = FakeReader(AudioLayout((audio,)))

        result = ReconciledLayoutProvider(
            FakeReader(DiscLayout((mmc_audio, data), 200)), audio_reader
        ).read("drive")

        self.assertEqual(audio_reader.calls, ["drive"])
        self.assertEqual(result.tracks[0].control, 1)
        self.assertEqual(result.tracks[0].length_msf, "00:01.25")
        self.assertIs(result.tracks[1], data)
        self.assertEqual(result.lead_out_lba, 200)

    def test_rejects_audio_track_set_disagreement(self):
        mmc = DiscLayout(
            (self.track(1, TrackKind.AUDIO, 0, 100),), 100
        )
        audio = AudioLayout((self.track(2, TrackKind.AUDIO, 0, 100),))
        with self.assertRaisesRegex(LayoutMismatchError, "which tracks are audio"):
            ReconciledLayoutProvider(FakeReader(mmc), FakeReader(audio)).read("drive")

    def test_rejects_every_boundary_disagreement(self):
        mmc = DiscLayout(
            (self.track(1, TrackKind.AUDIO, 10, 100),), 100
        )
        audio = AudioLayout(
            (self.track(1, TrackKind.AUDIO, 11, 103),)
        )
        with self.assertRaises(LayoutMismatchError) as caught:
            ReconciledLayoutProvider(FakeReader(mmc), FakeReader(audio)).read("drive")
        self.assertIn("begin: MMC=10, cdparanoia=11", str(caught.exception))
        self.assertIn("end: MMC=100, cdparanoia=103", str(caught.exception))
        self.assertIn("length: MMC=90, cdparanoia=92", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
