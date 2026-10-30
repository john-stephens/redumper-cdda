"""Typed output planning and transactional output creation."""

from pathlib import Path

from ..domain.errors import OutputError, PlanningError
from ..domain.outputs import CompletedOutput, OutputKind, OutputPlan
from ..domain.disc import TrackKind
from ..ports.output import OutputWriter


class OutputPlanner:
    def __init__(self, output_directory=None):
        self._directory = (
            Path.cwd() if output_directory is None else Path(output_directory)
        )

    def create(self, selection, request):
        tracks = selection.tracks
        spans = selection.spans
        if not request.single_file:
            return tuple(
                self._separate(span, request.prefix) for span in spans
            )
        if len(tracks) > 1 and any(
            track.kind is TrackKind.DATA for track in tracks
        ):
            raise PlanningError(
                "Data tracks can only be combined with other tracks as separate files."
            )
        first = tracks[0]
        if first.kind is TrackKind.DATA:
            path = self._requested_or_default(
                request.output, f"{request.prefix}{first.number:02d}.iso"
            )
            return (
                OutputPlan(
                    first,
                    OutputKind.DATA,
                    (),
                    spans[0].length_sectors,
                    path,
                    (spans[0],),
                ),
            )
        path = self._requested_or_default(
            request.output,
            (
                f"{request.prefix}{first.number:02d}.wav"
                if len(tracks) == 1
                else f"{request.prefix}.wav"
            ),
        )
        return (
            OutputPlan(
                None,
                OutputKind.AUDIO,
                tracks,
                sum(span.length_sectors for span in spans),
                path,
                spans,
            ),
        )

    def _separate(self, span, prefix):
        track = span.track
        kind = (
            OutputKind.DATA if track.kind is TrackKind.DATA else OutputKind.AUDIO
        )
        extension = "iso" if kind is OutputKind.DATA else "wav"
        return OutputPlan(
            track,
            kind,
            (track,) if kind is OutputKind.AUDIO else (),
            span.length_sectors,
            (self._directory / f"{prefix}{track.number:02d}.{extension}").resolve(),
            (span,),
        )

    def _requested_or_default(self, requested, default_name):
        return (
            Path(requested).expanduser().resolve()
            if requested is not None
            else (self._directory / default_name).resolve()
        )


class OutputTransaction:
    def __init__(self, writers: dict[OutputKind, OutputWriter], reporter=None):
        self._writers = writers
        self._reporter = reporter

    def create(self, outputs):
        paths = [resolved.plan.output_path for resolved in outputs]
        if len(set(paths)) != len(paths):
            raise OutputError("Output paths must be unique.")
        records = [self._record(resolved) for resolved in outputs]
        try:
            self._clear_stale(records)
            for resolved, temporary, _backup in records:
                self._writers[resolved.plan.kind].write(
                    resolved,
                    temporary,
                    verbose=(self._reporter.verbose if self._reporter else False),
                )
            self._commit(records)
        except BaseException:
            self._rollback(records)
            raise
        return tuple(
            CompletedOutput(resolved.plan, resolved.plan.output_path)
            for resolved, _temporary, _backup in records
        )

    @staticmethod
    def _record(resolved):
        path = resolved.plan.output_path
        return (
            resolved,
            path.with_name(f".{path.name}.part"),
            path.with_name(f".{path.name}.backup"),
        )

    @staticmethod
    def _clear_stale(records):
        for _resolved, temporary, backup in records:
            for path in (temporary, backup):
                if path.exists():
                    path.unlink()

    @staticmethod
    def _commit(records):
        for resolved, temporary, backup in records:
            final = resolved.plan.output_path
            if final.exists():
                final.replace(backup)
            temporary.replace(final)
        for _resolved, _temporary, backup in records:
            if backup.exists():
                try:
                    backup.unlink()
                except OSError:
                    pass

    @staticmethod
    def _rollback(records):
        for resolved, temporary, backup in reversed(records):
            final = resolved.plan.output_path
            if backup.exists():
                if final.exists():
                    final.unlink()
                backup.replace(final)
            if temporary.exists():
                temporary.unlink()
