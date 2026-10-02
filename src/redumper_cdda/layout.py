"""Pure disc-layout parsing, reconciliation, and track selection."""

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
