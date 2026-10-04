"""Production composition root."""

from functools import partial
from pathlib import Path

from .accuraterip import load_accuraterip_library, verify_with_accuraterip
from .adapters.accuraterip import AccurateRipVerifier, NullVerifier
from .adapters.cdparanoia import CdparanoiaTocReader
from .adapters.cue import AudioSegmentResolver, CueLocator, CueSheetParser, DataTrackResolver
from .adapters.iso9660 import IsoOutputWriter
from .adapters.layout_provider import ReconciledLayoutProvider
from .adapters.mmc import MmcTocReader
from .adapters.redumper import (
    RedumperClient,
    RedumperCommandFactory,
    RedumperIntegrityParser,
    RedumperProcessExecutor,
    RedumperStateInspector,
)
from .adapters.subprocess_runner import SubprocessRunner
from .adapters.wav import WaveOutputWriter
from .adapters.workspace import TemporaryWorkspaceFactory
from .application.acquisition import AcquisitionService
from .application.output import OutputPlanner, OutputTransaction
from .application.planning import ExtractionPlanner
from .application.splitting import SplitService
from .application.workflow import ExtractionApplication
from .domain.events import LifecycleEvent
from .domain.outputs import OutputKind
from .integrity import inspect_track_media_errors, parse_media_errors, parse_split_write_offsets
from .iso9660 import data_track_to_iso


def create_application(
    reporter,
    existing_dump=None,
    cdparanoia_toc_file=None,
):
    runner = SubprocessRunner()
    mmc_toc_file = None
    mmc_full_toc_file = None
    if existing_dump is not None:
        dump_prefix = Path(existing_dump)
        mmc_toc_file = Path(f"{dump_prefix}.toc")
        mmc_full_toc_file = Path(f"{dump_prefix}.fulltoc")
    layout_provider = ReconciledLayoutProvider(
        MmcTocReader(runner, reporter, mmc_toc_file, mmc_full_toc_file),
        CdparanoiaTocReader(runner, reporter, cdparanoia_toc_file),
    )
    redumper = RedumperClient(RedumperProcessExecutor(runner, reporter))
    integrity = RedumperIntegrityParser(parse_media_errors, parse_split_write_offsets)
    cue_parser = CueSheetParser()
    conversion_output = _conversion_output(reporter)
    return ExtractionApplication(
        layout_provider=layout_provider,
        planner=ExtractionPlanner(OutputPlanner(), RedumperCommandFactory),
        acquirer=AcquisitionService(redumper, integrity, reporter),
        splitter=SplitService(
            redumper,
            integrity,
            RedumperStateInspector(inspect_track_media_errors),
            AudioSegmentResolver(CueLocator(), cue_parser),
            DataTrackResolver(CueLocator(), cue_parser),
            reporter,
        ),
        output_service=OutputTransaction(
            {
                OutputKind.AUDIO: WaveOutputWriter(conversion_output),
                OutputKind.DATA: IsoOutputWriter(
                    partial(data_track_to_iso, output=conversion_output)
                ),
            },
            reporter,
        ),
        verifier_factory=_verifier,
        workspace_factory=TemporaryWorkspaceFactory(reporter),
        reporter=reporter,
    )


def _conversion_output(reporter):
    def output(*args, **options):
        reporter.publish(
            LifecycleEvent(
                "conversion_output",
                {"args": args, "options": options},
            )
        )

    return output


def _verifier(enabled):
    if not enabled:
        return NullVerifier()
    try:
        library = load_accuraterip_library()
    except RuntimeError:
        return NullVerifier()
    return AccurateRipVerifier(partial(verify_with_accuraterip, library=library))
