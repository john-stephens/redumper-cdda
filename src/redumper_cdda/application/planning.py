"""Track selection and bounded extraction planning."""

from ..domain.disc import Track, TrackKind
from ..domain.errors import PlanningError, SelectionError
from ..domain.extraction import ExtractionPlan, ResolvedSelection, SectorRange


END_PADDING_SECTORS = 1


class ExtractionPlanner:
    """Resolve a request into one auditable logical and physical range."""

    def __init__(self, output_planner, command_factory):
        self._output_planner = output_planner
        self._command_factory = command_factory

    def resolve(self, disc, requested, include_data=False):
        available = disc.tracks if include_data else disc.audio_tracks()
        if not available:
            message = (
                "No tracks are available."
                if include_data
                else "No audio tracks are available."
            )
            raise SelectionError(message)

        regular_numbers = [track.number for track in available]
        explicit_start = requested.start
        explicit_end = requested.end
        start = 1 if explicit_start is None else explicit_start
        end = max(regular_numbers) if explicit_end is None else explicit_end
        if start > end:
            raise SelectionError(f"Invalid track range {start}-{end}.")

        if include_data:
            selected = tuple(
                self._requested_track(disc.tracks, number, audio_only=False)
                for number in range(start, end + 1)
            )
        elif explicit_start is not None and explicit_end is not None:
            selected = tuple(
                self._requested_track(available, number, audio_only=True)
                for number in range(start, end + 1)
            )
        else:
            explicit_number = (
                explicit_start
                if explicit_start is not None
                else explicit_end
            )
            if explicit_number is not None:
                self._requested_track(available, explicit_number, audio_only=True)
            selected = tuple(
                track for track in available if start <= track.number <= end
            )
            if start == 0:
                selected = (
                    self._requested_track(available, 0, audio_only=True),
                    *selected,
                )

        return ResolvedSelection(selected)

    def create(self, request, disc, workdir):
        selection = self.resolve(
            disc,
            request.selection,
            include_data=request.include_data,
        )
        self._validate_output_mode(request, selection)

        logical_range = SectorRange(
            selection.first_track.begin_lba,
            selection.last_track.end_lba,
        )
        physical_range = logical_range.with_end_padding(END_PADDING_SECTORS)
        image_name = self._image_name(selection)
        outputs = self._output_planner.create(selection, request)

        commands = self._command_factory(
            device=request.device,
            workdir=workdir,
            image_name=image_name,
            retries=request.retries,
        )
        has_data = any(
            track.kind is TrackKind.DATA for track in selection.tracks
        )
        return ExtractionPlan(
            disc=disc,
            selection=selection,
            workdir=workdir,
            image_name=image_name,
            logical_range=logical_range,
            physical_range=physical_range,
            outputs=outputs,
            dump_command=commands.dump(physical_range),
            refine_command=commands.refine(physical_range),
            split_command=commands.split(include_data=has_data),
        )

    def _requested_track(self, tracks, number, audio_only):
        if number == 0:
            return self._track_zero(tracks)

        for track in tracks:
            if track.number == number:
                return track
        available = ", ".join(str(track.number) for track in tracks)
        label = "Audio track" if audio_only else "Track"
        suffix = "audio tracks" if audio_only else "tracks"
        raise SelectionError(
            f"{label} {number} was not found.\nAvailable {suffix}: {available}"
        )

    def _track_zero(self, tracks):
        track_one = None
        for track in tracks:
            if track.number == 1:
                track_one = track
                break
        if track_one is None:
            available = ", ".join(str(track.number) for track in tracks)
            raise SelectionError(
                "Audio track 1 was not found.\n"
                f"Available audio tracks: {available}"
            )
        if track_one.kind is not TrackKind.AUDIO:
            raise SelectionError(
                "Track 0 is only supported when Track 1 is audio."
            )
        if track_one.begin_lba <= 0:
            raise SelectionError(
                "Track 0 does not exist: Track 1 starts at LBA 0."
            )
        return Track(
            number=0,
            kind=TrackKind.AUDIO,
            control=0,
            begin_lba=0,
            end_lba=track_one.begin_lba,
            length_msf=self._sectors_to_msf(track_one.begin_lba),
            begin_msf="00:00.00",
        )

    @staticmethod
    def _validate_output_mode(request, selection):
        if (
            request.include_data
            and request.single_file
            and (
                len(selection.tracks) != 1
                or selection.first_track.kind is not TrackKind.DATA
            )
        ):
            raise PlanningError(
                "With --single-file, --include-data requires one "
                "explicit data track."
            )

    @staticmethod
    def _image_name(selection):
        first = selection.first_track.number
        last = selection.last_track.number
        return (
            f"track{first:02d}"
            if first == last
            else f"tracks{first:02d}-{last:02d}"
        )

    @staticmethod
    def _sectors_to_msf(sectors):
        minutes, remainder = divmod(sectors, 60 * 75)
        seconds, frames = divmod(remainder, 75)
        return f"{minutes:02d}:{seconds:02d}:{frames:02d}"
