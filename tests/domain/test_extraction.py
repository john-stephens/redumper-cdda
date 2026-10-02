import unittest
from pathlib import Path

from redumper_cdda.domain.disc import DiscLayout, Track, TrackKind
from redumper_cdda.domain.errors import DomainModelError
from redumper_cdda.domain.extraction import (
    AcquisitionResult,
    ExtractionPlan,
    ExtractionResult,
    ResolvedSelection,
    SectorRange,
)
from redumper_cdda.domain.integrity import MediaErrors


class ExtractionDomainTests(unittest.TestCase):
    def setUp(self):
        self.first = Track(1, TrackKind.AUDIO, 0, 100, 110)
        self.second = Track(2, TrackKind.DATA, 4, 110, 130)
        self.disc = DiscLayout((self.first, self.second), 130)

    def make_plan(self, physical=None, selected=None):
        selected = selected or (self.first, self.second)
        return ExtractionPlan(
            disc=self.disc,
            selection=ResolvedSelection(selected),
            workdir=Path("/tmp/work"),
            image_name="tracks01-02",
            logical_range=SectorRange(100, selected[-1].end_lba),
            physical_range=physical or SectorRange(100, selected[-1].end_lba + 1),
            outputs=(),
            dump_command=["dump"],
            refine_command=["refine"],
            split_command=["split"],
        )

    def test_sector_range_and_selection_values(self):
        sector_range = SectorRange(10, 20)
        self.assertEqual(sector_range.sectors, 10)
        self.assertEqual(sector_range.with_end_padding(), SectorRange(10, 21))
        self.assertEqual(sector_range.with_end_padding(0), sector_range)

        selection = ResolvedSelection((self.first, self.second))
        self.assertIs(selection.first_track, self.first)
        self.assertIs(selection.last_track, self.second)
        self.assertEqual(selection.output_sectors, 30)

        with self.assertRaisesRegex(DomainModelError, "positive"):
            SectorRange(10, 10)
        with self.assertRaisesRegex(DomainModelError, "negative"):
            sector_range.with_end_padding(-1)
        with self.assertRaisesRegex(DomainModelError, "cannot be empty"):
            ResolvedSelection(())

    def test_plan_properties_and_results(self):
        plan = self.make_plan()
        self.assertEqual(plan.track_label, "01-02")
        self.assertEqual(plan.logical_start_lba, 100)
        self.assertEqual(plan.logical_end_lba, 130)
        self.assertEqual(plan.dump_start_lba, 100)
        self.assertEqual(plan.dump_end_lba, 131)
        self.assertEqual(plan.expected_sectors, 30)

        single = self.make_plan(
            physical=SectorRange(100, 111),
            selected=(self.first,),
        )
        self.assertEqual(single.track_label, "01")

        acquisition = AcquisitionResult(MediaErrors(1, 2, 3), 4)
        self.assertEqual(acquisition.errors, {"SCSI": 1, "C2": 2, "Q": 3})
        result = ExtractionResult(plan, acquisition, (), None)
        self.assertIs(result.plan, plan)

    def test_plan_rejects_mismatched_physical_range(self):
        with self.assertRaisesRegex(DomainModelError, "start together"):
            self.make_plan(physical=SectorRange(99, 131))
        with self.assertRaisesRegex(DomainModelError, "endpoint sector"):
            self.make_plan(physical=SectorRange(100, 130))
