"""Production composition root."""

from functools import partial

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
from .domain.outputs import OutputKind
from .integrity import inspect_track_media_errors, parse_media_errors, parse_split_write_offsets
from .iso9660 import data_track_to_iso


def create_application(reporter):
    runner = SubprocessRunner()
    layout_provider = ReconciledLayoutProvider(
        MmcTocReader(runner, reporter),
        CdparanoiaTocReader(runner, reporter),
    )
    redumper = RedumperClient(RedumperProcessExecutor(runner, reporter))
    integrity = RedumperIntegrityParser(parse_media_errors, parse_split_write_offsets)
    cue_parser = CueSheetParser()
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
                OutputKind.AUDIO: WaveOutputWriter(),
                OutputKind.DATA: IsoOutputWriter(data_track_to_iso),
            },
            reporter,
        ),
        verifier_factory=_verifier,
        workspace_factory=TemporaryWorkspaceFactory(reporter),
        reporter=reporter,
    )


def _verifier(enabled):
    if not enabled:
        return NullVerifier()
    try:
        library = load_accuraterip_library()
    except RuntimeError:
        return NullVerifier()
    return AccurateRipVerifier(partial(verify_with_accuraterip, library=library))
