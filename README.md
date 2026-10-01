# riptrack-redump

## Overview

`riptrack-redump` makes it convenient to extract a single CD track or a
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
-   Produces combined WAV output or batch-split `trackNN.wav` files.
-   Optionally extracts data tracks as validated, mountable `trackNN.iso` files.
-   Optionally verifies selected audio tracks against the AccurateRip database.
-   Refines errors and supports strict abort-on-error behavior.

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
-   Optional: [ARver](https://pypi.org/project/ARver/) 1.5 or newer and an
    internet connection for `--accuraterip`. Install the Python dependencies
    with `python3 -m pip install -r requirements-accuraterip.txt`.

## Usage

``` bash
./riptrack-redump /dev/sg4        # Full disc (all audio tracks)
./riptrack-redump /dev/sg4 -      # Explicit full-disc selection
./riptrack-redump /dev/sg4 2
```

Typical optional controls:

``` bash
./riptrack-redump /dev/sg4 2 --retries=100 --refine-passes=3
```

Track selections may be a single track or a contiguous range:

``` bash
./riptrack-redump /dev/sg4 -      # Track 1 through the final track
./riptrack-redump /dev/sg4 2      # Track 2
./riptrack-redump /dev/sg4 1-3    # Tracks 1 through 3
./riptrack-redump /dev/sg4 -3     # Tracks 1 through 3
./riptrack-redump /dev/sg4 3-     # Track 3 through the final track
./riptrack-redump /dev/sg4 0-3    # Track 0 through Track 3
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

Use `-d` or `--include-data` to include data tracks. Mixed or multi-track
selections containing data require `-B`/`--batch`; one explicitly named data
track may be extracted without batch mode:

``` bash
./riptrack-redump /dev/sg4 1 --include-data
./riptrack-redump /dev/sg4 1-3 --include-data --batch
./riptrack-redump /dev/sg4 -3 -d -B
./riptrack-redump /dev/sg4 -d -B  # Full disc, including data tracks
```

With `--include-data`, ranges include both audio and data tracks. Audio files
are named `trackNN.wav` and data files are named `trackNN.iso`. Without this
option, the established audio-only range rules remain unchanged.

By default, a multi-track range is written as one `track.wav`. Use
`-B` or `--batch` to package the same single dump as separate
`trackNN.wav` files at the logical track boundaries:

``` bash
./riptrack-redump /dev/sg4 1-3 --batch
```

`--output=PATH` sets the non-batch output filename and cannot be used with
`--batch`. It applies to a combined audio WAV, a single audio track, or a
single explicitly selected data-track ISO.

Normal output is concise, with a track summary and single-line progress
showing the current track and percentage during dumping and refinement.
Use `-v` or `--verbose` to show executed
commands, complete redumper output, exact ranges, split segments, and
the full integrity summary:

``` bash
./riptrack-redump /dev/sg4 2 --verbose
```

Use `-q` or `--quiet` to suppress routine output. Errors are still
written to standard error and the exit status still indicates success
or failure:

``` bash
./riptrack-redump /dev/sg4 2 --quiet
```

Use `--accuraterip` to calculate ARv1 and ARv2 checksums with the ARver Python
library and compare selected audio tracks with the AccurateRip database:

``` bash
./riptrack-redump /dev/sg4 --batch --accuraterip
./riptrack-redump /dev/sg4 2 --accuraterip
```

AccurateRip verification uses the complete MMC disc layout to identify the
pressing even when only part of the disc is selected. Track 0 and data tracks
are not tracked by AccurateRip. A database miss, network failure, or checksum
mismatch does not delete completed output, and AccurateRip results do not
replace the separate redumper SCSI/C2 integrity status.

Each run creates a unique temporary workspace for redumper's dump, state,
BIN, and CUE files. The workspace is removed after success, failure, or
interruption. Completed WAV and ISO files are written outside that workspace.
They are first written to temporary sibling files and committed only after
the entire output set succeeds.

For a single track, the default output is `trackNN.wav`; for a
multi-track range, it is `track.wav`. Use `--output=PATH` to choose a
different non-batch filename and location.

For one data track selected with `--include-data`, the default output is
`trackNN.iso`.

To inspect the complete audio/data track layout without dumping anything,
omit the track number and use:

``` bash
./riptrack-redump /dev/sg4 --show-layout
```

## Extraction details

See [EXTRACTION.md](EXTRACTION.md) for the sector-boundary model, pregap
semantics, endpoint padding, PCM and ISO assembly, SCSI/C2 refinement,
hardware validation, and extraction invariants.
