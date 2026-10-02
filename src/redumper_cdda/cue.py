"""Generated CUE parsing and exact split-BIN range discovery."""

import re


SECTOR_SIZE = 2352
ISO_SECTOR_SIZE = 2048
SECTORS_PER_SECOND = 75


def snapshot_files(directory):
    result = {}

    if not directory.exists():
        return result

    for path in directory.iterdir():
        if not path.is_file():
            continue

        try:
            stat = path.stat()
        except OSError:
            continue

        result[path.resolve()] = (stat.st_size, stat.st_mtime_ns)

    return result


def changed_files(directory, before):
    result = []

    if not directory.exists():
        return result

    for path in directory.iterdir():
        if not path.is_file():
            continue

        resolved = path.resolve()

        try:
            stat = path.stat()
        except OSError:
            continue

        current = (stat.st_size, stat.st_mtime_ns)

        if resolved not in before or before[resolved] != current:
            result.append(path)

    return result


def msf_to_sectors(minutes, seconds, frames):
    return (
        minutes * 60 * SECTORS_PER_SECOND
        + seconds * SECTORS_PER_SECOND
        + frames
    )


def read_text_file(path):
    try:
        return path.read_text(encoding="utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp1252", errors="strict")


def parse_generated_cue(cue_path):
    text = read_text_file(cue_path)
    tracks = []
    current_file = None
    current_track = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        match = re.match(r'^FILE\s+"([^"]+)"\s+(\S+)', line, re.IGNORECASE)

        if match:
            current_file = match.group(1)
            continue

        match = re.match(r"^TRACK\s+(\d+)\s+(\S+)", line, re.IGNORECASE)

        if match:
            current_track = {
                "number": int(match.group(1)),
                "type": match.group(2).upper(),
                "file": current_file,
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
            index_number = int(match.group(1))
            current_track["indexes"][index_number] = msf_to_sectors(
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
            )

    return tracks


def find_generated_cues(workdir, image_name, changed):
    changed_cues = [path for path in changed if path.suffix.lower() == ".cue"]

    if changed_cues:
        return sorted(
            changed_cues,
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )

    candidates = [
        path
        for path in workdir.glob("*.cue")
        if image_name.lower() in path.name.lower()
    ]
    return sorted(
        candidates,
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )


def valid_audio_bin(path):
    try:
        size = path.stat().st_size
    except OSError:
        return False

    return size > 0 and size % SECTOR_SIZE == 0


def bin_sector_count(path):
    return path.stat().st_size // SECTOR_SIZE


def get_index01_offset(cue_track):
    indexes = cue_track["indexes"]

    if 1 not in indexes:
        raise RuntimeError(
            f"Track {cue_track['number']:02d} "
            "has no INDEX 01 in the generated CUE."
        )

    return indexes[1]


def identify_generated_audio_segments(
    workdir,
    image_name,
    changed,
    requested_track,
    expected_sectors,
    track_zero_sectors=None,
):
    generated_cues = find_generated_cues(workdir, image_name, changed)

    if not generated_cues:
        raise RuntimeError("Could not find redumper's generated CUE.")

    errors = []

    for cue_path in generated_cues:
        try:
            cue_tracks = parse_generated_cue(cue_path)
        except Exception as exc:
            errors.append(f"{cue_path}: {exc}")
            continue

        start_index = None
        cue_track_number = 1 if requested_track == 0 else requested_track

        for index, cue_track in enumerate(cue_tracks):
            if (
                cue_track["number"] == cue_track_number
                and cue_track["type"] == "AUDIO"
            ):
                start_index = index
                break

        if start_index is None:
            errors.append(
                f"{cue_path}: Track {cue_track_number:02d} not found as AUDIO"
            )
            continue

        selected_track = cue_tracks[start_index]

        try:
            selected_start_sector = get_index01_offset(selected_track)
        except Exception as exc:
            errors.append(f"{cue_path}: {exc}")
            continue

        expected_index01 = (
            track_zero_sectors
            if track_zero_sectors is not None
            else expected_sectors
        )
        if requested_track == 0 and selected_start_sector != expected_index01:
            errors.append(
                f"{cue_path}: Track 1 INDEX 01 is at "
                f"sector {selected_start_sector:,}, expected "
                f"{expected_index01:,} from cdparanoia"
            )
            continue

        segments = []
        remaining = expected_sectors

        for relative_index, cue_track in enumerate(cue_tracks[start_index:]):
            if cue_track["type"] != "AUDIO":
                break

            file_name = cue_track["file"]

            if not file_name:
                break

            path = cue_path.parent / file_name

            if not valid_audio_bin(path):
                break

            total_available = bin_sector_count(path)

            if requested_track == 0 and relative_index > 0:
                break

            if requested_track == 0:
                start_sector = 0
            elif relative_index == 0:
                start_sector = selected_start_sector
            else:
                start_sector = 0

            if start_sector > total_available:
                break

            available = total_available - start_sector
            take = min(available, remaining)

            if take > 0:
                segments.append(
                    {
                        "path": path,
                        "track": cue_track["number"],
                        "start_sector": start_sector,
                        "sectors": take,
                        "bin_sectors": total_available,
                    }
                )
                remaining -= take

            if remaining == 0:
                return (
                    segments,
                    cue_path,
                    0 if requested_track == 0 else selected_start_sector,
                )

        errors.append(f"{cue_path}: {remaining:,} additional sectors required")

    raise RuntimeError(
        "Could not identify enough split AUDIO data for the requested range.\n"
        + "\n".join(errors)
    )


def data_track_sector_size(track_type):
    sizes = {
        "MODE1/2352": SECTOR_SIZE,
        "MODE2/2352": SECTOR_SIZE,
        "MODE1/2048": ISO_SECTOR_SIZE,
    }

    if track_type not in sizes:
        raise RuntimeError(f"Unsupported data-track mode {track_type}.")

    return sizes[track_type]


def identify_generated_data_track(
    workdir,
    image_name,
    changed,
    requested_track,
):
    generated_cues = find_generated_cues(workdir, image_name, changed)

    if not generated_cues:
        raise RuntimeError("Could not find redumper's generated CUE.")

    errors = []

    for cue_path in generated_cues:
        try:
            cue_tracks = parse_generated_cue(cue_path)
            selected_track = next(
                (
                    track
                    for track in cue_tracks
                    if track["number"] == requested_track
                ),
                None,
            )

            if selected_track is None:
                raise RuntimeError(f"Track {requested_track:02d} was not found")

            if selected_track["type"] == "AUDIO":
                raise RuntimeError(
                    f"Track {requested_track:02d} is AUDIO, not data"
                )

            sector_size = data_track_sector_size(selected_track["type"])
            start_sector = get_index01_offset(selected_track)
            file_name = selected_track["file"]

            if not file_name:
                raise RuntimeError(f"Track {requested_track:02d} has no BIN file")

            shared_tracks = [
                track["number"]
                for track in cue_tracks
                if track is not selected_track and track["file"] == file_name
            ]

            if shared_tracks:
                raise RuntimeError(
                    f"Track {requested_track:02d} shares its BIN with another track"
                )

            path = cue_path.parent / file_name

            try:
                file_size = path.stat().st_size
            except OSError as exc:
                raise RuntimeError(f"Could not read data BIN {path}: {exc}") from exc

            if file_size <= 0 or file_size % sector_size != 0:
                raise RuntimeError(f"Data BIN {path} has an invalid size")

            total_sectors = file_size // sector_size

            if start_sector >= total_sectors:
                raise RuntimeError(
                    f"Track {requested_track:02d} INDEX 01 lies outside its BIN"
                )

            return {
                "cue_path": cue_path,
                "path": path,
                "track": requested_track,
                "track_type": selected_track["type"],
                "sector_size": sector_size,
                "start_sector": start_sector,
                "sectors": total_sectors - start_sector,
            }

        except Exception as exc:
            errors.append(f"{cue_path}: {exc}")

    raise RuntimeError(
        "Could not identify split data for the requested track.\n"
        + "\n".join(errors)
    )
