"""Forced partial-image splitting and immutable source resolution."""

from ..domain.errors import IntegrityStatusError, SplitError
from ..domain.events import LifecycleEvent
from ..domain.integrity import MediaErrors
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
        imported_errors = None
        errors_by_track = None
        try:
            if acquisition is None:
                errors_by_track = self._inspect_errors(plan, result.output)
                imported_errors = MediaErrors(
                    sum(item.scsi_samples for item in errors_by_track.values()),
                    sum(item.c2_samples for item in errors_by_track.values()),
                    None,
                )
                self._reporter.publish(
                    LifecycleEvent(
                        "media_errors",
                        {"errors": imported_errors, "pass_number": None},
                    )
                )
                self._reporter.publish(
                    LifecycleEvent(
                        "warning",
                        "Q error status is unavailable for an existing dump",
                    )
                )
                self._apply_single_file_policy(request, imported_errors)
                if imported_errors.has_data_errors and not request.abort_on_skip:
                    self._reporter.publish(
                        LifecycleEvent(
                            "warning",
                            "writing output from the existing dump with unresolved "
                            f"SCSI={imported_errors.scsi}, "
                            f"C2={imported_errors.c2} samples",
                        )
                    )
            output_plans, omitted = self._filter_errors(
                plan,
                request,
                (
                    imported_errors
                    if acquisition is None
                    else acquisition.media_errors
                ),
                result.output,
                output_plans,
                errors_by_track,
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
        return SplitResult(resolved, omitted, verification, imported_errors)

    def _filter_errors(
        self, plan, request, media_errors, split_output, outputs, errors_by_track=None
    ):
        if not (
            media_errors.has_data_errors
            and request.abort_on_skip
            and not request.single_file
        ):
            return outputs, ()
        if errors_by_track is None:
            errors_by_track = self._inspect_errors(plan, split_output)
        clean = []
        omitted = []
        for output in outputs:
            errors = errors_by_track[output.track.number]
            if errors.has_data_errors:
                omitted.append(OmittedOutput(output, errors))
            else:
                clean.append(output)
        return tuple(clean), tuple(omitted)

    def _inspect_errors(self, plan, split_output):
        offsets = self._integrity_parser.write_offsets(split_output)
        return self._state_inspector.inspect(
            plan.workdir / f"{plan.image_name}.state",
            plan.selection.tracks,
            offsets,
        )

    @staticmethod
    def _apply_single_file_policy(request, errors):
        if errors.has_data_errors and request.abort_on_skip and request.single_file:
            raise IntegrityStatusError(
                "SCSI/C2 errors remain in the existing dump.\n"
                f"Remaining SCSI error samples: {errors.scsi}\n"
                f"Remaining C2 error samples:   {errors.c2}\n"
                "No output file was created because --abort-on-skip was "
                "specified with --single-file."
            )

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
