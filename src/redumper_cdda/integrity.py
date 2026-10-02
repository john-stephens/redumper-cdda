"""Redumper media-error parsing and state-file inspection."""

import re
from pathlib import Path


# redumper stores one byte of state per stereo sample. Its CD image address
# space begins at -10:00:00, or LBA -45150.
REDUMPER_LBA_START = -45150
SAMPLES_PER_SECTOR = 588
REDUMPER_ERROR_SKIP = 0
REDUMPER_ERROR_C2 = 1
REDUMPER_MAX_STATE = 4


def parse_media_errors(output):
    """Parse the last redumper ``media errors:`` block."""

    pattern = re.compile(
        r"media errors\s*:\s*"
        r".*?"
        r"SCSI\s*:\s*(\d+)"
        r".*?"
        r"C2\s*:\s*(\d+)"
        r".*?"
        r"Q\s*:\s*(\d+)",
        re.IGNORECASE | re.DOTALL,
    )
    matches = list(pattern.finditer(output))

    if not matches:
        return None

    match = matches[-1]
    return {
        "SCSI": int(match.group(1)),
        "C2": int(match.group(2)),
        "Q": int(match.group(3)),
    }


def print_media_errors(errors):
    print()
    print("Media error status")
    print("------------------")
    print(f"SCSI: {errors['SCSI']}")
    print(f"C2:   {errors['C2']}")
    print(f"Q:    {errors['Q']}")


def data_errors_present(errors):
    """Return whether SCSI or C2 errors affect extracted data."""

    return errors["SCSI"] != 0 or errors["C2"] != 0


def should_abort_on_errors(errors, abort_on_skip):
    return abort_on_skip and data_errors_present(errors)


def parse_split_write_offsets(output):
    """Return redumper's logical-LBA-to-state sample offsets."""

    disc_matches = re.findall(
        r"^\s*disc write offset\s*:\s*([+-]?\d+)\s*$",
        output,
        re.IGNORECASE | re.MULTILINE,
    )

    if not disc_matches:
        raise RuntimeError(
            "Could not determine redumper's split write offset."
        )

    lines = output.splitlines()
    shift_offsets = []
    shift_marker_found = False

    for index, line in enumerate(lines):
        if not re.search(
            r"offset shift correction applied\s*:",
            line,
            re.IGNORECASE,
        ):
            continue

        shift_marker_found = True

        for offset_line in lines[index + 1:]:
            match = re.match(
                r"^\s*LBA\s*:\s*(-?\d+)\s*,\s*"
                r"offset\s*:\s*([+-]?\d+)\s*$",
                offset_line,
                re.IGNORECASE,
            )

            if match:
                shift_offsets.append(
                    (int(match.group(1)), int(match.group(2)))
                )
            elif shift_offsets or offset_line.strip():
                break

        break

    if shift_offsets:
        if shift_offsets != sorted(shift_offsets):
            raise RuntimeError(
                "redumper reported unsorted offset-shift boundaries."
            )
        return shift_offsets

    if shift_marker_found:
        raise RuntimeError(
            "Could not parse redumper's offset-shift boundaries."
        )

    return [(0, int(disc_matches[-1]))]


def state_offset_for_lba(offsets, lba):
    offset = offsets[0][1]

    for offset_lba, candidate in offsets:
        if offset_lba > lba:
            break
        offset = candidate

    return offset


def inspect_track_media_errors(state_path, tracks, offsets):
    """Count unresolved redumper states in each logical track range."""

    state_path = Path(state_path)

    if not state_path.is_file():
        raise RuntimeError(
            f"redumper state file was not found: {state_path.name}"
        )

    transition_lbas = [lba for lba, _offset in offsets[1:]]
    results = {}

    with state_path.open("rb") as state_file:
        for track in tracks:
            lba = track["begin"]
            logical_end_lba = track["end"]
            scsi_samples = 0
            c2_samples = 0
            scsi_sectors = 0
            c2_sectors = 0

            while lba < logical_end_lba:
                next_transition = next(
                    (
                        boundary
                        for boundary in transition_lbas
                        if boundary > lba
                    ),
                    logical_end_lba,
                )
                sector_count = min(
                    1024,
                    logical_end_lba - lba,
                    next_transition - lba,
                )
                sample_offset = state_offset_for_lba(offsets, lba)
                file_sample = (
                    (lba - REDUMPER_LBA_START)
                    * SAMPLES_PER_SECTOR
                    + sample_offset
                )

                if file_sample < 0:
                    raise RuntimeError(
                        "redumper state offset precedes the state file."
                    )

                expected_samples = sector_count * SAMPLES_PER_SECTOR
                state_file.seek(file_sample)
                states = state_file.read(expected_samples)

                if len(states) != expected_samples:
                    raise RuntimeError(
                        "redumper state file is shorter than the "
                        "selected logical track range."
                    )

                if states and max(states) > REDUMPER_MAX_STATE:
                    raise RuntimeError(
                        "redumper state file contains an unknown state."
                    )

                scsi_samples += states.count(REDUMPER_ERROR_SKIP)
                c2_samples += states.count(REDUMPER_ERROR_C2)

                for sector_start in range(
                    0,
                    len(states),
                    SAMPLES_PER_SECTOR,
                ):
                    sector = states[
                        sector_start:sector_start + SAMPLES_PER_SECTOR
                    ]
                    if REDUMPER_ERROR_SKIP in sector:
                        scsi_sectors += 1
                    if REDUMPER_ERROR_C2 in sector:
                        c2_sectors += 1

                lba += sector_count

            results[track["number"]] = {
                "SCSI": scsi_samples,
                "C2": c2_samples,
                "SCSI sectors": scsi_sectors,
                "C2 sectors": c2_sectors,
            }

    return results
