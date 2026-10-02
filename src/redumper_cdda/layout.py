"""Pure disc-layout parsing, reconciliation, and track selection."""

import argparse
import re


SECTORS_PER_SECOND = 75


def sectors_to_msf(sectors):
    minutes, remainder = divmod(
        sectors,
        60 * SECTORS_PER_SECOND,
    )
    seconds, frames = divmod(
        remainder,
        SECTORS_PER_SECOND,
    )
    return f"{minutes:02d}:{seconds:02d}:{frames:02d}"


def parse_mmc_toc(data):
    if len(data) < 4:
        raise RuntimeError(
            "MMC READ TOC response is shorter than its header."
        )

    data_length = int.from_bytes(data[0:2], byteorder="big")
    response_length = data_length + 2

    if data_length < 10:
        raise RuntimeError(
            "MMC READ TOC response contains no track layout."
        )
    if response_length > len(data):
        raise RuntimeError("MMC READ TOC response is truncated.")

    descriptor_data = data[4:response_length]
    if len(descriptor_data) % 8 != 0:
        raise RuntimeError(
            "MMC READ TOC response has malformed descriptors."
        )

    first_track = data[2]
    last_track = data[3]
    if not (1 <= first_track <= last_track <= 99):
        raise RuntimeError(
            "MMC READ TOC response has an invalid track range."
        )

    descriptors = {}
    leadout_lba = None

    for offset in range(0, len(descriptor_data), 8):
        descriptor = descriptor_data[offset:offset + 8]
        control = descriptor[1] & 0x0F
        track_number = descriptor[2]
        start_lba = int.from_bytes(descriptor[4:8], byteorder="big")

        if track_number == 0xAA:
            if leadout_lba is not None:
                raise RuntimeError(
                    "MMC READ TOC response contains multiple "
                    "lead-out descriptors."
                )
            leadout_lba = start_lba
            continue

        if not first_track <= track_number <= last_track:
            continue
        if track_number in descriptors:
            raise RuntimeError(
                "MMC READ TOC response contains duplicate "
                f"Track {track_number}."
            )
        descriptors[track_number] = {
            "control": control,
            "begin": start_lba,
        }

    expected_numbers = list(range(first_track, last_track + 1))
    missing_numbers = [
        number for number in expected_numbers if number not in descriptors
    ]
    if missing_numbers:
        missing = ", ".join(str(number) for number in missing_numbers)
        raise RuntimeError(
            "MMC READ TOC response is missing track "
            f"descriptor(s): {missing}."
        )
    if leadout_lba is None:
        raise RuntimeError(
            "MMC READ TOC response has no lead-out descriptor."
        )

    tracks = []
    for index, number in enumerate(expected_numbers):
        descriptor = descriptors[number]
        begin = descriptor["begin"]
        end = (
            descriptors[expected_numbers[index + 1]]["begin"]
            if index + 1 < len(expected_numbers)
            else leadout_lba
        )
        if end <= begin:
            raise RuntimeError(
                "MMC READ TOC response has a non-positive "
                f"length for Track {number}."
            )

        length = end - begin
        kind = "data" if descriptor["control"] & 0x04 else "audio"
        tracks.append(
            {
                "number": number,
                "kind": kind,
                "control": descriptor["control"],
                "length": length,
                "length_msf": sectors_to_msf(length),
                "begin": begin,
                "begin_msf": sectors_to_msf(begin),
                "end": end,
            }
        )

    return tracks


def parse_cdparanoia_toc(text):
    tracks = []
    pattern = re.compile(
        r"^\s*"
        r"(\d+)\.\s+"
        r"(\d+)\s+"
        r"\[(\d+:\d+\.\d+)\]\s+"
        r"(-?\d+)\s+"
        r"\[(\d+:\d+\.\d+)\]"
    )

    for raw_line in text.splitlines():
        match = pattern.match(raw_line)
        if not match:
            continue

        number = int(match.group(1))
        length = int(match.group(2))
        length_msf = match.group(3)
        begin = int(match.group(4))
        begin_msf = match.group(5)
        tracks.append(
            {
                "number": number,
                "length": length,
                "length_msf": length_msf,
                "begin": begin,
                "begin_msf": begin_msf,
                "end": begin + length,
            }
        )

    if not tracks:
        raise RuntimeError(
            "Could not parse any audio tracks from "
            "cdparanoia -Q output."
        )
    return tracks


def reconcile_disc_layout(mmc_tracks, cdparanoia_tracks):
    cdparanoia_by_number = {
        track["number"]: track for track in cdparanoia_tracks
    }
    mmc_audio_numbers = {
        track["number"] for track in mmc_tracks if track["kind"] == "audio"
    }
    cdparanoia_numbers = set(cdparanoia_by_number)

    if mmc_audio_numbers != cdparanoia_numbers:
        raise RuntimeError(
            "MMC and cdparanoia disagree about which tracks "
            "are audio "
            f"(MMC: {sorted(mmc_audio_numbers)}, "
            f"cdparanoia: {sorted(cdparanoia_numbers)})."
        )

    reconciled = []
    for mmc_track in mmc_tracks:
        track = dict(mmc_track)
        if track["kind"] == "audio":
            cdparanoia_track = cdparanoia_by_number[track["number"]]
            differing_fields = [
                field
                for field in ("begin", "end", "length")
                if track[field] != cdparanoia_track[field]
            ]
            if differing_fields:
                values = ", ".join(
                    f"{field}: MMC={track[field]}, "
                    f"cdparanoia={cdparanoia_track[field]}"
                    for field in differing_fields
                )
                raise RuntimeError(
                    "MMC and cdparanoia boundaries disagree "
                    f"for audio Track {track['number']} "
                    f"({values})."
                )

            track.update(cdparanoia_track)
            track["kind"] = "audio"
            track["control"] = mmc_track["control"]
        reconciled.append(track)

    return reconciled


def find_track(tracks, number):
    for track in tracks:
        if track["number"] == number:
            return track
    available = ", ".join(str(track["number"]) for track in tracks)
    raise RuntimeError(
        f"Audio track {number} was not found.\n"
        f"Available audio tracks: {available}"
    )


def find_disc_track(tracks, number):
    for track in tracks:
        if track["number"] == number:
            return track
    available = ", ".join(str(track["number"]) for track in tracks)
    raise RuntimeError(
        f"Track {number} was not found.\n"
        f"Available tracks: {available}"
    )


def find_requested_track(tracks, number):
    if number != 0:
        return find_track(tracks, number)

    track_one = find_track(tracks, 1)
    if track_one.get("kind", "audio") != "audio":
        raise RuntimeError(
            "Track 0 is only supported when Track 1 is audio."
        )

    length = track_one["begin"]
    if length <= 0:
        raise RuntimeError(
            "Track 0 does not exist: Track 1 starts at LBA 0."
        )
    return {
        "number": 0,
        "kind": "audio",
        "length": length,
        "length_msf": sectors_to_msf(length),
        "begin": 0,
        "begin_msf": "00:00.00",
        "end": track_one["begin"],
    }


def parse_track_selection(value):
    if value == "-":
        return {"start": None, "end": None}
    if re.fullmatch(r"\d+", value):
        number = int(value)
        return {"start": number, "end": number}

    match = re.fullmatch(r"(\d*)-(\d*)", value)
    if not match:
        raise argparse.ArgumentTypeError(
            "track must be N, N-M, -M, N-, or -"
        )
    return {
        "start": int(match.group(1)) if match.group(1) else None,
        "end": int(match.group(2)) if match.group(2) else None,
    }


def resolve_track_selection(tracks, selection):
    regular_numbers = [track["number"] for track in tracks]
    if not regular_numbers:
        raise RuntimeError("No audio tracks are available.")

    explicit_start = selection["start"]
    explicit_end = selection["end"]
    start = 1 if explicit_start is None else explicit_start
    end = max(regular_numbers) if explicit_end is None else explicit_end
    if start > end:
        raise RuntimeError(f"Invalid track range {start}-{end}.")

    if explicit_start is not None and explicit_end is not None:
        selected = [
            find_requested_track(tracks, number)
            for number in range(start, end + 1)
        ]
    else:
        explicit_number = (
            explicit_start if explicit_start is not None else explicit_end
        )
        if explicit_number is not None:
            find_requested_track(tracks, explicit_number)
        selected = [
            track for track in tracks if start <= track["number"] <= end
        ]
        if start == 0:
            selected.insert(0, find_requested_track(tracks, 0))
    return selected


def resolve_disc_selection(disc_tracks, selection, include_data=False):
    if not include_data:
        audio_tracks = [
            track
            for track in disc_tracks
            if track.get("kind", "audio") == "audio"
        ]
        return resolve_track_selection(audio_tracks, selection)

    regular_numbers = [track["number"] for track in disc_tracks]
    if not regular_numbers:
        raise RuntimeError("No tracks are available.")

    explicit_start = selection["start"]
    explicit_end = selection["end"]
    start = 1 if explicit_start is None else explicit_start
    end = max(regular_numbers) if explicit_end is None else explicit_end
    if start > end:
        raise RuntimeError(f"Invalid track range {start}-{end}.")

    selected = []
    for number in range(start, end + 1):
        if number == 0:
            selected.append(find_requested_track(disc_tracks, 0))
            continue
        selected.append(find_disc_track(disc_tracks, number))
    return selected


def format_track_numbers(selected_tracks):
    first = selected_tracks[0]["number"]
    last = selected_tracks[-1]["number"]
    if first == last:
        return f"{first:02d}"
    return f"{first:02d}-{last:02d}"


def validate_data_output_mode(selected_tracks, include_data, single_file):
    if (
        include_data
        and single_file
        and (
            len(selected_tracks) != 1
            or selected_tracks[0].get("kind", "audio") != "data"
        )
    ):
        raise RuntimeError(
            "With --single-file, --include-data requires one "
            "explicit data track."
        )
