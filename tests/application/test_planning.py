import unittest
from pathlib import Path

from redumper_cdda.adapters.redumper import RedumperCommandFactory
from redumper_cdda.application.planning import ExtractionPlanner
from redumper_cdda.application.output import OutputPlanner
from redumper_cdda.domain.disc import DiscLayout, Track, TrackKind
from redumper_cdda.domain.errors import PlanningError, SelectionError
from redumper_cdda.domain.extraction import ExtractionRequest, TrackSelection


class ExtractionPlannerTests(unittest.TestCase):
    def setUp(self):
        self.planner = ExtractionPlanner(
            output_planner=OutputPlanner(),
            command_factory=RedumperCommandFactory,
        )

    @staticmethod
    def track(number, kind=TrackKind.AUDIO, begin=None, end=None):
        begin = number * 100 if begin is None else begin
        end = begin + 100 if end is None else end
        return Track(number, kind, 4 if kind is TrackKind.DATA else 0, begin, end)

    @staticmethod
    def request(selection, **changes):
        values = {
            "device": "/dev/sg-test",
            "selection": selection,
            "include_data": False,
            "single_file": False,
            "output": None,
            "prefix": "track",
            "retries": 100,
            "refine_passes": 3,
            "abort_on_skip": False,
            "accuraterip": False,
        }
        values.update(changes)
        return ExtractionRequest(**values)

    def audio_disc(self):
        tracks = tuple(self.track(number) for number in range(1, 5))
        return DiscLayout(tracks, tracks[-1].end_lba)

    def test_resolves_all_five_selection_forms(self):
        disc = self.audio_disc()
        cases = (
            (TrackSelection(), [1, 2, 3, 4]),
            (TrackSelection(2, 2), [2]),
            (TrackSelection(1, 3), [1, 2, 3]),
            (TrackSelection(None, 3), [1, 2, 3]),
            (TrackSelection(3, None), [3, 4]),
        )
        for requested, expected in cases:
            with self.subTest(requested=requested):
                resolved = self.planner.resolve(disc, requested)
                self.assertEqual(
                    [track.number for track in resolved.tracks], expected
                )

    def test_audio_ranges_omit_data_but_bounded_ranges_are_strict(self):
        tracks = (
            self.track(1, TrackKind.DATA),
            self.track(2),
            self.track(3),
            self.track(4, TrackKind.DATA),
        )
        disc = DiscLayout(tracks, tracks[-1].end_lba)

        self.assertEqual(
            [
                track.number
                for track in self.planner.resolve(
                    disc, TrackSelection(None, 3)
                ).tracks
            ],
            [2, 3],
        )
        with self.assertRaisesRegex(SelectionError, "Audio track 1"):
            self.planner.resolve(disc, TrackSelection(1, 3))
        with self.assertRaisesRegex(SelectionError, "Audio track 4"):
            self.planner.resolve(disc, TrackSelection(2, 4))

    def test_include_data_requires_every_number_and_keeps_all_types(self):
        tracks = (self.track(1, TrackKind.DATA), self.track(2))
        disc = DiscLayout(tracks, tracks[-1].end_lba)
        resolved = self.planner.resolve(
            disc, TrackSelection(), include_data=True
        )
        self.assertEqual(resolved.tracks, tracks)

        missing = DiscLayout((self.track(1), self.track(3)), 400)
        with self.assertRaisesRegex(SelectionError, "Track 2 was not found"):
            self.planner.resolve(
                missing, TrackSelection(1, 3), include_data=True
            )

    def test_invalid_and_empty_selections_fail_closed(self):
        data = self.track(1, TrackKind.DATA)
        data_disc = DiscLayout((data,), data.end_lba)
        with self.assertRaisesRegex(SelectionError, "No audio tracks"):
            self.planner.resolve(data_disc, TrackSelection())
        with self.assertRaisesRegex(SelectionError, "Invalid track range 3-2"):
            self.planner.resolve(self.audio_disc(), TrackSelection(3, 2))
        with self.assertRaisesRegex(SelectionError, "Invalid track range 3-2"):
            self.planner.resolve(
                self.audio_disc(), TrackSelection(3, 2), include_data=True
            )

    def test_track_zero_is_explicit_audio_before_track_one(self):
        first = self.track(1, begin=150, end=250)
        second = self.track(2, begin=250, end=350)
        disc = DiscLayout((first, second), 350)
        selected = self.planner.resolve(disc, TrackSelection(0, 1))

        track_zero = selected.first_track
        self.assertEqual(track_zero.number, 0)
        self.assertEqual(track_zero.begin_lba, 0)
        self.assertEqual(track_zero.end_lba, 150)
        self.assertEqual(track_zero.length_msf, "00:02:00")
        self.assertEqual(track_zero.begin_msf, "00:00.00")
        self.assertEqual(
            [track.number for track in self.planner.resolve(
                disc, TrackSelection(0, None)
            ).tracks],
            [0, 1, 2],
        )

    def test_track_zero_rejects_invalid_track_one_layouts(self):
        zero_start = self.track(1, begin=0, end=100)
        with self.assertRaisesRegex(SelectionError, "starts at LBA 0"):
            self.planner.resolve(
                DiscLayout((zero_start,), 100), TrackSelection(0, 0)
            )

        data = self.track(1, TrackKind.DATA, begin=100, end=200)
        with self.assertRaisesRegex(SelectionError, "only supported"):
            self.planner.resolve(
                DiscLayout((data,), 200),
                TrackSelection(0, 0),
                include_data=True,
            )

        second = self.track(2)
        with self.assertRaisesRegex(SelectionError, "Audio track 1"):
            self.planner.resolve(
                DiscLayout((second,), second.end_lba), TrackSelection(0, 0)
            )

    def test_create_builds_one_padded_plan_and_exact_commands(self):
        first = self.track(1, begin=100, end=110)
        second = self.track(2, TrackKind.DATA, begin=110, end=130)
        disc = DiscLayout((first, second), 130)
        plan = self.planner.create(
            self.request(
                TrackSelection(1, 2), include_data=True, prefix="album"
            ),
            disc,
            Path("/tmp/work"),
        )

        self.assertEqual(plan.logical_start_lba, 100)
        self.assertEqual(plan.logical_end_lba, 130)
        self.assertEqual(plan.dump_start_lba, 100)
        self.assertEqual(plan.dump_end_lba, 131)
        self.assertEqual(plan.expected_sectors, 30)
        self.assertEqual(plan.image_name, "tracks01-02")
        self.assertIn("--lba-start=100", plan.dump_command)
        self.assertIn("--lba-end=131", plan.dump_command)
        self.assertIn("--lba-start=100", plan.refine_command)
        self.assertIn("--lba-end=131", plan.refine_command)
        self.assertIn("--filesystem-trim", plan.split_command)
        self.assertEqual(
            [output.output_path.name for output in plan.outputs],
            ["album01.wav", "album02.iso"],
        )

    def test_create_single_track_name_and_output_mode_validation(self):
        audio = self.track(1)
        audio_disc = DiscLayout((audio,), audio.end_lba)
        plan = self.planner.create(
            self.request(TrackSelection(1, 1)),
            audio_disc,
            Path("/tmp/work"),
        )
        self.assertEqual(plan.image_name, "track01")
        self.assertNotIn("--filesystem-trim", plan.split_command)

        with self.assertRaisesRegex(PlanningError, "one explicit data track"):
            self.planner.create(
                self.request(
                    TrackSelection(1, 1), include_data=True, single_file=True
                ),
                audio_disc,
                Path("/tmp/work"),
            )

        first = self.track(1, TrackKind.DATA)
        second = self.track(2, TrackKind.DATA)
        data_disc = DiscLayout((first, second), second.end_lba)
        with self.assertRaisesRegex(PlanningError, "one explicit data track"):
            self.planner.create(
                self.request(
                    TrackSelection(1, 2), include_data=True, single_file=True
                ),
                data_disc,
                Path("/tmp/work"),
            )

        valid = self.planner.create(
            self.request(
                TrackSelection(1, 1), include_data=True, single_file=True
            ),
            data_disc,
            Path("/tmp/work"),
        )
        self.assertEqual(valid.outputs[0].kind.value, "data")


if __name__ == "__main__":
    unittest.main()
