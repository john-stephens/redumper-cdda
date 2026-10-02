"""Generated CUE parsing and exact split-source resolution adapters."""

import re

from ..domain.errors import CueError
from ..domain.outputs import AudioSegment, CueTrack, DataTrackSource


SECTOR_SIZE = 2352
ISO_SECTOR_SIZE = 2048
SECTORS_PER_SECOND = 75


class CueSheetParser:
    def read_text(self, path):
        try:
            return path.read_text(encoding="utf-8-sig", errors="strict")
        except UnicodeDecodeError:
            return path.read_text(encoding="cp1252", errors="strict")

    def parse(self, cue_path):
        tracks = []
        current_file = None
        current_track = None
        for raw_line in self.read_text(cue_path).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = re.match(
                r'^FILE\s+"([^"]+)"\s+(\S+)', line, re.IGNORECASE
            )
            if match:
                current_file = match.group(1)
                continue
            match = re.match(r"^TRACK\s+(\d+)\s+(\S+)", line, re.IGNORECASE)
            if match:
                current_track = {
                    "number": int(match.group(1)),
                    "track_type": match.group(2).upper(),
                    "file_name": current_file,
                    "indexes": {},
                }
                tracks.append(current_track)
                continue
            if current_track is None:
                continue
            match = re.match(
                r"^INDEX\s+(\d+)\s+(\d+):(\d+):(\d+)",
                line,
                re.IGNORECASE,
            )
            if match:
                current_track["indexes"][int(match.group(1))] = self.msf_to_sectors(
                    int(match.group(2)),
                    int(match.group(3)),
                    int(match.group(4)),
                )
        return tuple(
            CueTrack(
                track["number"],
                track["track_type"],
                track["file_name"],
                tuple(track["indexes"].items()),
            )
            for track in tracks
        )

    @staticmethod
    def msf_to_sectors(minutes, seconds, frames):
        return (
            minutes * 60 * SECTORS_PER_SECOND
            + seconds * SECTORS_PER_SECOND
            + frames
        )


class CueLocator:
    def locate(self, workdir, image_name, changed):
        changed_cues = [
            path for path in changed if path.suffix.lower() == ".cue"
        ]
        if changed_cues:
            candidates = changed_cues
        else:
            candidates = [
                path
                for path in workdir.glob("*.cue")
                if image_name.lower() in path.name.lower()
            ]
        return tuple(
            sorted(
                candidates,
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        )


class AudioSegmentResolver:
    def __init__(self, locator, parser, bin_sectors=None, index01=None):
        self._locator = locator
        self._parser = parser
        self._bin_sectors = bin_sectors or self._audio_bin_sectors
        self._index01_reader = index01 or self._index01

    def resolve(
        self,
        workdir,
        image_name,
        changed,
        requested_track,
        expected_sectors,
        track_zero_sectors=None,
    ):
        generated_cues = self._locator.locate(workdir, image_name, changed)
        if not generated_cues:
            raise CueError("Could not find redumper's generated CUE.")
        errors = []
        for cue_path in generated_cues:
            try:
                return self._resolve_cue(
                    cue_path,
                    requested_track,
                    expected_sectors,
                    track_zero_sectors,
                )
            except Exception as exc:
                errors.append(f"{cue_path}: {exc}")
        raise CueError(
            "Could not identify enough split AUDIO data for the requested range.\n"
            + "\n".join(errors)
        )

    def _resolve_cue(
        self, cue_path, requested_track, expected_sectors, track_zero_sectors
    ):
        cue_tracks = self._parser.parse(cue_path)
        cue_track_number = 1 if requested_track == 0 else requested_track
        start_index = next(
            (
                index
                for index, track in enumerate(cue_tracks)
                if track.number == cue_track_number
                and track.track_type == "AUDIO"
            ),
            None,
        )
        if start_index is None:
            raise CueError(
                f"Track {cue_track_number:02d} not found as AUDIO"
            )
        selected_start = self._index01_reader(cue_tracks[start_index])
        expected_index01 = (
            track_zero_sectors
            if track_zero_sectors is not None
            else expected_sectors
        )
        if requested_track == 0 and selected_start != expected_index01:
            raise CueError(
                f"Track 1 INDEX 01 is at sector {selected_start:,}, "
                f"expected {expected_index01:,} from cdparanoia"
            )
        segments = []
        remaining = expected_sectors
        for relative_index, cue_track in enumerate(cue_tracks[start_index:]):
            if cue_track.track_type != "AUDIO" or not cue_track.file_name:
                break
            path = cue_path.parent / cue_track.file_name
            total_available = self._bin_sectors(path)
            if total_available is None:
                break
            if requested_track == 0 and relative_index > 0:
                break
            start_sector = (
                0
                if requested_track == 0 or relative_index > 0
                else selected_start
            )
            if start_sector > total_available:
                break
            take = min(total_available - start_sector, remaining)
            if take > 0:
                segments.append(
                    AudioSegment(
                        path,
                        cue_track.number,
                        start_sector,
                        take,
                        total_available,
                    )
                )
                remaining -= take
            if remaining == 0:
                return (
                    tuple(segments),
                    cue_path,
                    0 if requested_track == 0 else selected_start,
                )
        raise CueError(f"{remaining:,} additional sectors required")

    @staticmethod
    def _audio_bin_sectors(path):
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size <= 0 or size % SECTOR_SIZE != 0:
            return None
        return size // SECTOR_SIZE

    @staticmethod
    def _index01(cue_track):
        sector = cue_track.index(1)
        if sector is None:
            raise CueError(
                f"Track {cue_track.number:02d} has no INDEX 01 "
                "in the generated CUE."
            )
        return sector


class DataTrackResolver:
    def __init__(self, locator, parser, sector_size=None, index01=None):
        self._locator = locator
        self._parser = parser
        self._sector_size = sector_size or self.sector_size
        self._index01_reader = index01 or AudioSegmentResolver._index01

    def resolve(self, workdir, image_name, changed, requested_track):
        generated_cues = self._locator.locate(workdir, image_name, changed)
        if not generated_cues:
            raise CueError("Could not find redumper's generated CUE.")
        errors = []
        for cue_path in generated_cues:
            try:
                return self._resolve_cue(cue_path, requested_track)
            except Exception as exc:
                errors.append(f"{cue_path}: {exc}")
        raise CueError(
            "Could not identify split data for the requested track.\n"
            + "\n".join(errors)
        )

    def _resolve_cue(self, cue_path, requested_track):
        cue_tracks = self._parser.parse(cue_path)
        selected = next(
            (track for track in cue_tracks if track.number == requested_track),
            None,
        )
        if selected is None:
            raise CueError(f"Track {requested_track:02d} was not found")
        if selected.track_type == "AUDIO":
            raise CueError(f"Track {requested_track:02d} is AUDIO, not data")
        sector_size = self._sector_size(selected.track_type)
        start_sector = self._index01_reader(selected)
        if not selected.file_name:
            raise CueError(f"Track {requested_track:02d} has no BIN file")
        if any(
            track is not selected and track.file_name == selected.file_name
            for track in cue_tracks
        ):
            raise CueError(
                f"Track {requested_track:02d} shares its BIN with another track"
            )
        path = cue_path.parent / selected.file_name
        try:
            file_size = path.stat().st_size
        except OSError as exc:
            raise CueError(f"Could not read data BIN {path}: {exc}") from exc
        if file_size <= 0 or file_size % sector_size != 0:
            raise CueError(f"Data BIN {path} has an invalid size")
        total_sectors = file_size // sector_size
        if start_sector >= total_sectors:
            raise CueError(
                f"Track {requested_track:02d} INDEX 01 lies outside its BIN"
            )
        return DataTrackSource(
            cue_path,
            path,
            requested_track,
            selected.track_type,
            sector_size,
            start_sector,
            total_sectors - start_sector,
        )

    @staticmethod
    def sector_size(track_type):
        sizes = {
            "MODE1/2352": SECTOR_SIZE,
            "MODE2/2352": SECTOR_SIZE,
            "MODE1/2048": ISO_SECTOR_SIZE,
        }
        if track_type not in sizes:
            raise CueError(f"Unsupported data-track mode {track_type}.")
        return sizes[track_type]
