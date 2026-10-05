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

        if first_track <= track_number <= last_track:
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


def parse_mmc_full_toc(data):
    """Return track-to-session and per-session lead-out data from format 2."""

    if len(data) < 4:
        raise RuntimeError("MMC full TOC response is shorter than its header.")
    response_length = int.from_bytes(data[0:2], byteorder="big") + 2
    if response_length > len(data):
        raise RuntimeError("MMC full TOC response is truncated.")
    descriptor_data = data[4:response_length]
    if not descriptor_data or len(descriptor_data) % 11:
        raise RuntimeError("MMC full TOC response has malformed descriptors.")

    track_sessions = {}
    session_leadouts = {}
    for offset in range(0, len(descriptor_data), 11):
        descriptor = descriptor_data[offset:offset + 11]
        session = descriptor[0]
        adr = descriptor[1] >> 4
        point = descriptor[3]
        if adr != 1:
            continue
        if 1 <= point <= 99:
            previous = track_sessions.setdefault(point, session)
            if previous != session:
                raise RuntimeError(
                    f"MMC full TOC assigns Track {point} to multiple sessions."
                )
        elif point == 0xA2:
            leadout = (
                (descriptor[8] * 60 + descriptor[9]) * SECTORS_PER_SECOND
                + descriptor[10]
                - 150
            )
            previous = session_leadouts.setdefault(session, leadout)
            if previous != leadout:
                raise RuntimeError(
                    f"MMC full TOC contains conflicting Session {session} lead-outs."
                )

    if not track_sessions:
        raise RuntimeError("MMC full TOC contains no track descriptors.")
    missing_leadouts = set(track_sessions.values()) - set(session_leadouts)
    if missing_leadouts:
        sessions = ", ".join(str(item) for item in sorted(missing_leadouts))
        raise RuntimeError(
            f"MMC full TOC is missing lead-out data for session(s): {sessions}."
        )
    return track_sessions, session_leadouts


def apply_session_boundaries(tracks, full_toc):
    """End a session's final track at that session's own lead-out."""

    track_sessions, session_leadouts = full_toc
    numbers = [track["number"] for track in tracks]
    missing = [number for number in numbers if number not in track_sessions]
    if missing:
        labels = ", ".join(str(number) for number in missing)
        raise RuntimeError(f"MMC full TOC is missing Track(s): {labels}.")

    adjusted = []
    for index, track in enumerate(tracks):
        session = track_sessions[track["number"]]
        next_session = (
            track_sessions[tracks[index + 1]["number"]]
            if index + 1 < len(tracks)
            else session
        )
        end = track["end"]
        if next_session != session:
            if next_session < session:
                raise RuntimeError("MMC full TOC sessions are out of order.")
            end = session_leadouts[session]
        if end <= track["begin"] or (
            index + 1 < len(tracks) and end > tracks[index + 1]["begin"]
        ):
            raise RuntimeError(
                f"MMC full TOC has an invalid end for Track {track['number']}."
            )
        length = end - track["begin"]
        adjusted.append(
            {
                **track,
                "end": end,
                "length": length,
                "length_msf": sectors_to_msf(length),
            }
        )
    return adjusted


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
