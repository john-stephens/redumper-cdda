"""Forced partial-image splitting and immutable source resolution."""

from ..domain.errors import IntegrityStatusError, SplitError
from ..domain.events import LifecycleEvent
from ..domain.outputs import (
    OmittedOutput,
    OutputKind,
    ResolvedOutput,
    SplitResult,
    VerificationTrack,
)
from ..ports.acquisition import IntegrityParser, RedumperPort, StateInspector


class SplitService:
    def __init__(
        self,
        redumper: RedumperPort,
        integrity_parser: IntegrityParser,
        state_inspector: StateInspector,
        audio_resolver,
        data_resolver,
        reporter,
    ):
        self._redumper = redumper
        self._integrity_parser = integrity_parser
        self._state_inspector = state_inspector
        self._audio_resolver = audio_resolver
        self._data_resolver = data_resolver
        self._reporter = reporter

    def split(
        self,
        plan,
        request,
        acquisition,
        before,
        changed_files,
    ):
        self._reporter.publish(LifecycleEvent("split_started"))
        result = self._redumper.split(plan)
        self._reporter.publish(
            LifecycleEvent("split_finished", result.returncode == 0)
        )
        if result.returncode != 0:
            raise SplitError(
                "redumper could not split the partial dump even with "
                f"--force-split.\nExit status: {result.returncode}\n"
                "No output file was created."
            )

        output_plans = plan.outputs
        try:
            output_plans, omitted = self._filter_errors(
                plan, request, acquisition, result.output, output_plans
            )
        except IntegrityStatusError as exc:
            raise IntegrityStatusError(
                f"{exc}\nCould not safely identify which track outputs "
                "contain unresolved SCSI/C2 errors.\n"
                "No output file was created."
            ) from exc
        if not output_plans:
            raise IntegrityStatusError(
                "every selected track contains unresolved SCSI/C2 errors; "
                "no output file was created."
            )

        changed = changed_files(before)
        resolved, verification = self._resolve_sources(
            plan, output_plans, changed, request.accuraterip
        )
        return SplitResult(resolved, omitted, verification)

    def _filter_errors(self, plan, request, acquisition, split_output, outputs):
        if not (
            acquisition.media_errors.has_data_errors
            and request.abort_on_skip
            and not request.single_file
        ):
            return outputs, ()
        offsets = self._integrity_parser.write_offsets(split_output)
        errors_by_track = self._state_inspector.inspect(
            plan.workdir / f"{plan.image_name}.state",
            plan.selection.tracks,
            offsets,
        )
        clean = []
        omitted = []
        for output in outputs:
            errors = errors_by_track[output.track.number]
            if errors.has_data_errors:
                omitted.append(OmittedOutput(output, errors))
            else:
                clean.append(output)
        return tuple(clean), tuple(omitted)

    def _resolve_sources(self, plan, output_plans, changed, accuraterip):
        track_zero_sectors = (
            plan.selection.first_track.length_sectors
            if plan.selection.first_track.number == 0
            else None
        )
        outputs = []
        verification = []
        for output in output_plans:
            if output.kind is OutputKind.DATA:
                source = self._data_resolver.resolve(
                    plan.workdir,
                    plan.image_name,
                    changed,
                    output.track.number,
                )
                outputs.append(
                    ResolvedOutput(
                        output,
                        data_source=source,
                        cue_path=source.cue_path,
                        pregap_skipped=source.start_sector,
                    )
                )
                continue
            segments = []
            cue_path = None
            pregap = None
            for track in output.component_tracks:
                component, cue_path, skipped = self._audio_resolver.resolve(
                    plan.workdir,
                    plan.image_name,
                    changed,
                    track.number,
                    track.length_sectors,
                    track_zero_sectors=track_zero_sectors,
                )
                segments.extend(component)
                if accuraterip and track.number != 0:
                    verification.append(VerificationTrack(track, component))
                if pregap is None:
                    pregap = skipped
            outputs.append(
                ResolvedOutput(output, tuple(segments), None, cue_path, pregap)
            )
        return tuple(outputs), tuple(verification)
