# redumper-cdda

## Overview

`redumper-cdda` makes it convenient to extract a single CD track or a
contiguous track range without giving up redumper's preservation-oriented
acquisition. redumper performs the low-level read, error detection, retry, and
refinement work; the script adds track selection, boundary validation, concise
progress reporting, and ready-to-use WAV or ISO output.

This is useful when a full-disc image is unnecessary but accurate sector
boundaries and redumper's handling of drive offsets and read errors still
matter. Each selection is acquired as one bounded redumper dump, including
mixed audio/data ranges when requested.

## Features

-   Extracts one track or a contiguous track range with a single redumper dump.
-   Supports hidden audio in Track 0 when a positive Track 1 pregap exists.
-   Produces one numbered WAV or ISO per track by default, with optional
    combined WAV output and customizable filenames.
-   Optionally extracts data tracks as validated, mountable `trackNN.iso` files.
-   Verifies selected audio tracks against the AccurateRip database by default.
-   Refines errors and can omit only affected per-track outputs when strict
    abort-on-error behavior is requested.

## Requirements

-   Linux / Python 3
-   `sg_raw` from sg3-utils
-   `cdparanoia`
-   `redumper`
-   Optical drive exposed as a SCSI generic device such as `/dev/sg4`
-   A drive listed in redumper's
    [drive-support documentation](https://github.com/superg/redumper#drive-support)
    is recommended for accurate CD dumping. The installed redumper executable
    can also list recommended models with `redumper --list-recommended-drives`.
-   [ARver](https://pypi.org/project/ARver/) 1.5 or newer. It is installed as
    a required Python dependency. AccurateRip verification also requires an
    internet connection.

## Installation

Install the command in an isolated environment with
[pipx](https://pipx.pypa.io/):

``` bash
pipx install redumper-cdda
```

From an existing checkout, use `pipx install .`. The installation creates the
`redumper-cdda` command on the user path. System tools such as redumper,
cdparanoia, and `sg_raw` remain external requirements.

## Usage

``` bash
redumper-cdda /dev/sg4        # Full disc (all audio tracks)
redumper-cdda /dev/sg4 -      # Explicit full-disc selection
redumper-cdda /dev/sg4 2
```

Typical optional controls:

``` bash
redumper-cdda /dev/sg4 2 --retries=100 --refine-passes=3
```

Track selections may be a single track or a contiguous range:

``` bash
redumper-cdda /dev/sg4 -      # Track 1 through the final track
redumper-cdda /dev/sg4 2      # Track 2
redumper-cdda /dev/sg4 1-3    # Tracks 1 through 3
redumper-cdda /dev/sg4 -3     # Tracks 1 through 3
redumper-cdda /dev/sg4 3-     # Track 3 through the final track
redumper-cdda /dev/sg4 0-3    # Track 0 through Track 3
```

Omitting the selection defaults to `-`, which selects the full disc from
Track 1 through the final numbered track. An open-start range such as `-3`
also begins at Track 1 and does not include Track 0. Track 0 must be requested
explicitly. A multi-track selection is read by one redumper dump covering the
full contiguous LBA range; it is not implemented as separate per-track dumps.

Open-ended ranges automatically omit data tracks. Fully bounded ranges
are strict: every numbered track in `N-M` must be audio. For example, if
Track 1 is data, `-3` extracts audio Tracks 2 and 3, while `1-3` fails.
The physical range is still read once; data-track payload is excluded
when the AUDIO segments are assembled into WAV output.

Use `-d` or `--include-data` to include data tracks. The default separate-file
mode supports mixed or multi-track selections containing data. One explicitly
named data track may also be extracted with `--single-file`:

``` bash
redumper-cdda /dev/sg4 1 --include-data
redumper-cdda /dev/sg4 1-3 --include-data
redumper-cdda /dev/sg4 -3 -d
redumper-cdda /dev/sg4 -d  # Full disc, including data tracks
```

With `--include-data`, ranges include both audio and data tracks. Audio files
are named `PREFIXNN.wav` and data files are named `PREFIXNN.iso`, where the
default prefix is `track`. Without this option, the established audio-only
range rules remain unchanged.

By default, each selected track is written separately as `trackNN.wav` or
`trackNN.iso`. Use `-s` or `--single-file` to combine a multi-track audio range
into one `track.wav` while retaining the same single dump:

``` bash
redumper-cdda /dev/sg4 1-3 --single-file
```

`--output=PATH` sets the `--single-file` output filename. It applies to a
combined audio WAV, a single audio track, or a single explicitly selected
data-track ISO.

Use `-p` or `--prefix` to replace the default `track` prefix on automatically
named files:

``` bash
redumper-cdda /dev/sg4 1-3 --prefix album
# Writes album01.wav, album02.wav, and album03.wav
```

Normal output is concise, with a track summary and single-line progress
showing the current track and percentage during dumping and refinement.
Use `-v` or `--verbose` to show executed
commands, complete redumper output, exact ranges, split segments, and
the full integrity summary:

``` bash
redumper-cdda /dev/sg4 2 --verbose
```

Use `-q` or `--quiet` to suppress routine output. Errors are still
written to standard error and the exit status still indicates success
or failure:

``` bash
redumper-cdda /dev/sg4 2 --quiet
```

Use `-X` or `--abort-on-skip` to prevent unresolved SCSI/C2 errors from being
written. In the default separate-file mode, clean tracks are retained and only
affected track files are omitted; the command exits nonzero to report the
omissions. With `--single-file`, any unresolved SCSI/C2 error rejects the one
combined output, so no output file is created.

By default, the script calculates ARv1 and ARv2 checksums with ARver and
compares selected audio tracks with the AccurateRip database automatically:

``` bash
redumper-cdda /dev/sg4
redumper-cdda /dev/sg4 2
```

AccurateRip verification uses the complete MMC disc layout to identify the
pressing even when only part of the disc is selected. Track 0 and data tracks
are not tracked by AccurateRip. A database miss, network failure, or checksum
mismatch does not delete completed output, and AccurateRip results do not
replace the separate redumper SCSI/C2 integrity status.

Use `--no-accuraterip` to disable AccurateRip verification even when ARver is
installed:

``` bash
redumper-cdda /dev/sg4 2 --no-accuraterip
```

Each run creates a unique temporary workspace for redumper's dump, state,
BIN, and CUE files. The workspace is removed after success, failure, or
interruption. Completed WAV and ISO files are written outside that workspace.
They are first written to temporary sibling files and committed only after
the entire permitted output set succeeds. With `--abort-on-skip`, tracks that
contain unresolved SCSI/C2 errors are excluded from that set.

For every audio track, the default output is `trackNN.wav`. With
`--single-file`, a multi-track range defaults to `track.wav`. Use
`--output=PATH` to choose a different single-file name and location.

For one data track selected with `--include-data`, the default output is
`trackNN.iso`, or `PREFIXNN.iso` when `--prefix` is used.

To inspect the complete audio/data track layout without dumping anything,
omit the track number and use:

``` bash
redumper-cdda /dev/sg4 --show-layout
```

## Extraction details

See [EXTRACTION.md](EXTRACTION.md) for the sector-boundary model, pregap
semantics, endpoint padding, PCM and ISO assembly, SCSI/C2 refinement,
hardware validation, and extraction invariants.
