import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from redumper_cdda.application.splitting import SplitService
from redumper_cdda.domain.errors import IntegrityStatusError, SplitError
from redumper_cdda.domain.disc import Track, TrackKind
from redumper_cdda.domain.integrity import MediaErrors, TrackMediaErrors
from redumper_cdda.domain.outputs import AudioSegment, DataTrackSource, OutputKind, OutputPlan
from redumper_cdda.ports.process import CommandResult
from tests.contracts.fakes import RecordingReporter


class SplitServiceTests(unittest.TestCase):
    def service(self, result=None, audio=None, data=None, parser=None, inspector=None):
        redumper = mock.Mock()
        redumper.split.return_value = result or CommandResult((), 0, "split")
        return SplitService(
            redumper, parser or mock.Mock(), inspector or mock.Mock(),
            audio or mock.Mock(), data or mock.Mock(), RecordingReporter(),
        ), redumper

    def test_returns_immutable_resolved_audio_without_mutating_plan(self):
        track = Track(1, TrackKind.AUDIO, 0, 100, 110)
        output_plan = OutputPlan(
            track, OutputKind.AUDIO, (track,), 10, Path("track01.wav")
        )
        plan = SimpleNamespace(
            outputs=(output_plan,),
            selection=SimpleNamespace(first_track=track, tracks=(track,)),
            workdir=Path("/work"),
            image_name="track01",
        )
        request = SimpleNamespace(
            abort_on_skip=False,
            single_file=False,
            accuraterip=True,
        )
        acquisition = SimpleNamespace(media_errors=MediaErrors(0, 0, 0))
        redumper = mock.Mock()
        redumper.split.return_value = CommandResult((), 0, "split")
        resolver = mock.Mock()
        segment = AudioSegment(Path("audio.bin"), 1, 0, 10, 10)
        resolver.resolve.return_value = ((segment,), Path("disc.cue"), 0)
        service = SplitService(
            redumper,
            mock.Mock(),
            mock.Mock(),
            resolver,
            mock.Mock(),
            RecordingReporter(),
        )

        result = service.split(
            plan,
            request,
            acquisition,
            before={},
            changed_files=lambda _before: [Path("disc.cue")],
        )

        self.assertEqual(result.outputs[0].audio_segments, (segment,))
        self.assertEqual(result.verification_tracks[0].track, track)
        redumper.split.assert_called_once_with(plan)

    def test_split_failure_and_integrity_parser_failure(self):
        plan = SimpleNamespace(outputs=())
        request = SimpleNamespace(abort_on_skip=False, single_file=False)
        acquisition = SimpleNamespace(media_errors=MediaErrors(0, 0, 0))
        service, _redumper = self.service(CommandResult((), 2, "bad"))
        with self.assertRaisesRegex(SplitError, "Exit status: 2"):
            service.split(plan, request, acquisition, {}, lambda _before: [])

        track = Track(1, TrackKind.AUDIO, 0, 0, 10)
        output = OutputPlan(track, OutputKind.AUDIO, (track,), 10, Path("x"))
        plan = SimpleNamespace(
            outputs=(output,), workdir=Path("/work"), image_name="track01",
            selection=SimpleNamespace(tracks=(track,), first_track=track),
        )
        request = SimpleNamespace(abort_on_skip=True, single_file=False, accuraterip=False)
        acquisition = SimpleNamespace(media_errors=MediaErrors(1, 0, 0))
        parser = mock.Mock()
        parser.write_offsets.side_effect = IntegrityStatusError("offsets")
        service, _redumper = self.service(parser=parser)
        with self.assertRaisesRegex(IntegrityStatusError, "safely identify"):
            service.split(plan, request, acquisition, {}, lambda _before: [])

    def test_strict_filter_omits_bad_tracks_and_rejects_all_bad(self):
        first = Track(1, TrackKind.AUDIO, 0, 0, 10)
        second = Track(2, TrackKind.AUDIO, 0, 10, 20)
        outputs = tuple(
            OutputPlan(track, OutputKind.AUDIO, (track,), 10, Path(f"{track.number}"))
            for track in (first, second)
        )
        plan = SimpleNamespace(
            outputs=outputs, workdir=Path("/work"), image_name="tracks01-02",
            selection=SimpleNamespace(tracks=(first, second), first_track=first),
        )
        request = SimpleNamespace(abort_on_skip=True, single_file=False, accuraterip=False)
        acquisition = SimpleNamespace(media_errors=MediaErrors(1, 0, 0))
        parser = mock.Mock()
        parser.write_offsets.return_value = object()
        inspector = mock.Mock()
        clean = TrackMediaErrors(0, 0, 0, 0)
        dirty = TrackMediaErrors(1, 0, 1, 0)
        inspector.inspect.return_value = {1: clean, 2: dirty}
        audio = mock.Mock()
        audio.resolve.return_value = ((AudioSegment(Path("x"), 1, 0, 10, 10),), Path("x.cue"), 0)
        service, _redumper = self.service(audio=audio, parser=parser, inspector=inspector)
        result = service.split(plan, request, acquisition, {}, lambda _before: [])
        self.assertEqual(len(result.outputs), 1)
        self.assertEqual(result.omitted[0].plan.track.number, 2)

        inspector.inspect.return_value = {1: dirty, 2: dirty}
        with self.assertRaisesRegex(IntegrityStatusError, "every selected"):
            service.split(plan, request, acquisition, {}, lambda _before: [])

    def test_resolves_data_track_and_track_zero_audio(self):
        zero = Track(0, TrackKind.AUDIO, 0, 0, 5)
        data_track = Track(2, TrackKind.DATA, 4, 5, 10)
        audio_plan = OutputPlan(zero, OutputKind.AUDIO, (zero,), 5, Path("zero.wav"))
        data_plan = OutputPlan(data_track, OutputKind.DATA, (), 5, Path("two.iso"))
        plan = SimpleNamespace(
            outputs=(audio_plan, data_plan), workdir=Path("/work"), image_name="tracks00-02",
            selection=SimpleNamespace(tracks=(zero, data_track), first_track=zero),
        )
        request = SimpleNamespace(abort_on_skip=False, single_file=False, accuraterip=True)
        acquisition = SimpleNamespace(media_errors=MediaErrors(0, 0, 0))
        audio = mock.Mock()
        audio.resolve.return_value = ((AudioSegment(Path("zero.bin"), 1, 0, 5, 5),), Path("disc.cue"), 0)
        data = mock.Mock()
        source = DataTrackSource(Path("disc.cue"), Path("data.bin"), 2, "MODE1/2352", 2352, 1, 5)
        data.resolve.return_value = source
        service, _redumper = self.service(audio=audio, data=data)
        result = service.split(plan, request, acquisition, {}, lambda _before: [Path("disc.cue")])
        self.assertEqual(result.outputs[1].data_source, source)
        self.assertEqual(result.outputs[1].pregap_skipped, 1)
        self.assertEqual(result.verification_tracks, ())
        self.assertEqual(audio.resolve.call_args.kwargs["track_zero_sectors"], 5)

    def test_combined_audio_tracks_capture_first_pregap(self):
        first = Track(1, TrackKind.AUDIO, 0, 0, 5)
        second = Track(2, TrackKind.AUDIO, 0, 5, 10)
        output = OutputPlan(None, OutputKind.AUDIO, (first, second), 10, Path("all.wav"))
        plan = SimpleNamespace(
            outputs=(output,), workdir=Path("/work"), image_name="tracks01-02",
            selection=SimpleNamespace(tracks=(first, second), first_track=first),
        )
        request = SimpleNamespace(abort_on_skip=False, single_file=True, accuraterip=True)
        acquisition = SimpleNamespace(media_errors=MediaErrors(0, 0, 0))
        audio = mock.Mock()
        audio.resolve.side_effect = (
            ((AudioSegment(Path("one"), 1, 2, 5, 7),), Path("one.cue"), 2),
            ((AudioSegment(Path("two"), 2, 0, 5, 5),), Path("two.cue"), 0),
        )
        service, _redumper = self.service(audio=audio)
        result = service.split(plan, request, acquisition, {}, lambda _before: [])
        self.assertEqual(result.outputs[0].pregap_skipped, 2)
        self.assertEqual(result.outputs[0].cue_path, Path("two.cue"))
        self.assertEqual(len(result.verification_tracks), 2)

    def test_existing_dump_integrity_is_read_from_state_after_split(self):
        track = Track(1, TrackKind.AUDIO, 0, 0, 10)
        output = OutputPlan(track, OutputKind.AUDIO, (track,), 10, Path("one.wav"))
        plan = SimpleNamespace(
            outputs=(output,), workdir=Path("/work"), image_name="track01",
            selection=SimpleNamespace(tracks=(track,), first_track=track),
        )
        request = SimpleNamespace(
            abort_on_skip=False, single_file=False, accuraterip=False
        )
        parser = mock.Mock()
        offsets = object()
        parser.write_offsets.return_value = offsets
        inspector = mock.Mock()
        inspector.inspect.return_value = {1: TrackMediaErrors(2, 3, 1, 1)}
        audio = mock.Mock()
        audio.resolve.return_value = (
            (AudioSegment(Path("one.bin"), 1, 0, 10, 10),),
            Path("one.cue"),
            0,
        )
        reporter = RecordingReporter()
        redumper = mock.Mock()
        redumper.split.return_value = CommandResult((), 0, "disc write offset: 0")
        service = SplitService(
            redumper, parser, inspector, audio, mock.Mock(), reporter
        )

        result = service.split(plan, request, None, {}, lambda _before: [])

        self.assertEqual(result.media_errors, MediaErrors(2, 3, None))
        inspector.inspect.assert_called_once_with(
            Path("/work/track01.state"), (track,), offsets
        )
        warnings = [
            event.values for event in reporter.events if event.name == "warning"
        ]
        self.assertEqual(len(warnings), 1)
        self.assertIn("SCSI=2, C2=3", warnings[0])

    def test_existing_dump_applies_both_strict_error_policies(self):
        track = Track(1, TrackKind.AUDIO, 0, 0, 10)
        output = OutputPlan(track, OutputKind.AUDIO, (track,), 10, Path("one.wav"))
        plan = SimpleNamespace(
            outputs=(output,), workdir=Path("/work"), image_name="track01",
            selection=SimpleNamespace(tracks=(track,), first_track=track),
        )
        parser = mock.Mock()
        parser.write_offsets.return_value = object()
        inspector = mock.Mock()
        inspector.inspect.return_value = {1: TrackMediaErrors(1, 0, 1, 0)}
        service, _redumper = self.service(parser=parser, inspector=inspector)

        single = SimpleNamespace(
            abort_on_skip=True, single_file=True, accuraterip=False
        )
        with self.assertRaisesRegex(IntegrityStatusError, "single-file"):
            service.split(plan, single, None, {}, lambda _before: [])

        separate = SimpleNamespace(
            abort_on_skip=True, single_file=False, accuraterip=False
        )
        with self.assertRaisesRegex(IntegrityStatusError, "every selected"):
            service.split(plan, separate, None, {}, lambda _before: [])


if __name__ == "__main__":
    unittest.main()
