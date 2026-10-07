"""Forced partial-image splitting and immutable source resolution."""

from math import ceil

from ..domain.disc import TrackKind
from ..domain.errors import IntegrityStatusError, SplitError
from ..domain.events import LifecycleEvent
from ..domain.integrity import MediaErrors
from ..domain.outputs import (
    AudioSegment,
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

        verification_offsets = (
            self._integrity_parser.write_offsets(result.output)
            if request.accuraterip
            else None
        )
        changed = changed_files(before)
        resolved, verification = self._resolve_sources(
            plan, output_plans, changed, request.accuraterip,
            verification_offsets,
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

    def _resolve_sources(
        self, plan, output_plans, changed, accuraterip, verification_offsets=None
    ):
        track_zero_sectors = (
            plan.selection.first_track.length_sectors
            if plan.selection.first_track.number == 0
            else None
        )
        outputs = []
        resolved_audio = {}

        def resolve_audio(track):
            if track.number not in resolved_audio:
                resolved_audio[track.number] = self._audio_resolver.resolve(
                    plan.workdir,
                    plan.image_name,
                    changed,
                    track.number,
                    track.length_sectors,
                    track_zero_sectors=track_zero_sectors,
                )
            return resolved_audio[track.number]

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
                component, cue_path, skipped = resolve_audio(track)
                segments.extend(component)
                if pregap is None:
                    pregap = skipped
            outputs.append(
                ResolvedOutput(output, tuple(segments), None, cue_path, pregap)
            )

        verification = []
        if accuraterip:
            selected_audio = tuple(
                track
                for track in plan.selection.tracks
                if track.number != 0 and track.kind is TrackKind.AUDIO
            )
            selected_indexes = {
                track.number: index for index, track in enumerate(selected_audio)
            }
            verification_tracks = (
                track
                for output in output_plans
                if output.kind is OutputKind.AUDIO
                for track in output.component_tracks
                if track.number != 0
            )
            for track in verification_tracks:
                component, _cue_path, _skipped = resolve_audio(track)
                write_offset = (
                    verification_offsets.offset_for_lba(track.begin_lba)
                    if verification_offsets is not None
                    else 0
                )
                preceding = ()
                following = ()
                index = selected_indexes[track.number]
                if write_offset > 0 and index > 0:
                    neighbor = selected_audio[index - 1]
                    if (
                        verification_offsets.offset_for_lba(neighbor.begin_lba)
                        == write_offset
                    ):
                        preceding = resolve_audio(neighbor)[0]
                elif write_offset < 0 and index + 1 < len(selected_audio):
                    neighbor = selected_audio[index + 1]
                    if (
                        verification_offsets.offset_for_lba(neighbor.begin_lba)
                        == write_offset
                    ):
                        following = resolve_audio(neighbor)[0]
                elif write_offset < 0:
                    last = component[-1]
                    tail_start = last.start_sector + last.sectors
                    tail_sectors = min(
                        last.bin_sectors - tail_start,
                        ceil(-write_offset / 588),
                    )
                    if tail_sectors > 0:
                        following = (
                            AudioSegment(
                                last.path,
                                last.track_number,
                                tail_start,
                                tail_sectors,
                                last.bin_sectors,
                            ),
                        )
                verification.append(
                    VerificationTrack(
                        track,
                        component,
                        write_offset,
                        preceding,
                        following,
                    )
                )
        return tuple(outputs), tuple(verification)
