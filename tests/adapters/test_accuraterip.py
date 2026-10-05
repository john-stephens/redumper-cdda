import unittest
from pathlib import Path
from types import SimpleNamespace

from redumper_cdda.adapters.accuraterip import AccurateRipVerifier, NullVerifier
from redumper_cdda.domain.disc import DiscLayout, Track, TrackKind
from redumper_cdda.domain.errors import VerificationError
from redumper_cdda.domain.outputs import AudioSegment, SplitResult, VerificationTrack


class AccurateRipVerifierTests(unittest.TestCase):
    def plan(self, tracks, selected=None):
        disc = DiscLayout(tuple(tracks), tracks[-1].end_lba)
        return SimpleNamespace(
            disc=disc,
            selection=SimpleNamespace(tracks=tuple(selected or tracks)),
        )

    def verifier(self, function):
        return AccurateRipVerifier(function)

    def test_prepares_supported_layout_and_returns_typed_report(self):
        track = Track(1, TrackKind.AUDIO, 0, 0, 10)
        plan = self.plan([track])
        captured = []

        def verify(disc, requested, workdir):
            captured.append((disc, requested, workdir))
            return {"disc_id": "id", "results": [{"track": 1}]}

        verifier = self.verifier(verify)
        verifier.prepare(plan)
        segment = AudioSegment(Path("audio.bin"), 1, 0, 10, 10)
        split = SplitResult((), verification_tracks=(VerificationTrack(track, (segment,)),))
        report = verifier.verify(plan, split, Path("/work"))
        self.assertEqual(report.disc_id, "id")
        self.assertEqual(report.results[0]["track"], 1)
        self.assertEqual(captured[0][1][0]["segments"][0]["sectors"], 10)

    def test_translates_layout_selection_and_lookup_failures(self):
        data = Track(1, TrackKind.DATA, 4, 0, 10)
        audio = Track(2, TrackKind.AUDIO, 0, 10, 20)
        data_verifier = self.verifier(lambda *_args: None)
        data_verifier.prepare(self.plan([data, audio], selected=[data]))
        self.assertFalse(data_verifier.enabled)

        trailing_audio = Track(3, TrackKind.AUDIO, 0, 20, 30)
        with self.assertRaisesRegex(VerificationError, "does not support"):
            self.verifier(lambda *_args: None).prepare(
                self.plan([audio, data, trailing_audio], selected=[audio])
            )

        plan = self.plan([audio])
        verifier = self.verifier(
            lambda *_args: (_ for _ in ()).throw(RuntimeError("network"))
        )
        with self.assertRaisesRegex(VerificationError, "network"):
            verifier.verify(plan, SplitResult(()), Path("/work"))

    def test_null_verifier_is_noop(self):
        verifier = NullVerifier()
        self.assertIsNone(verifier.prepare(object()))
        self.assertIsNone(verifier.verify(object(), object(), Path("/work")))


if __name__ == "__main__":
    unittest.main()
