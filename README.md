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
-   Can keep refining a difficult disc until all SCSI and C2 errors are gone.
-   Can save complete diagnostic output to a log while keeping the terminal
    display concise.

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

To reuse an existing redumper dump, pass the shared path prefix without any
extension:

``` bash
redumper-cdda /dev/sg4 2 --existing-dump=/archive/disc
```

For example, `/archive/disc` identifies files such as `disc.scram`,
`disc.state`, `disc.subcode`, and `disc.toc`. The required primary dump files
are copied into the unique temporary workspace, so the supplied dump remains
read-only. The command skips both `redumper dump` and `redumper refine`, then
continues with one `redumper split --force-split` and the normal transactional
output and AccurateRip phases. The dump must cover the requested logical range
and its one-sector endpoint padding. Layout still requires MMC and cdparanoia
data, either read from the optical disc or supplied through the file-backed TOC
options described below.

For an existing dump, unresolved SCSI/C2 state is determined from the copied
`.state` file using the write-offset mapping reported by split. The same
default and `--abort-on-skip` policies apply.

### File-backed TOCs and offline debugging

When `--existing-dump` is supplied, layout acquisition automatically reads the
matching redumper `.toc` and `.fulltoc` files. Only cdparanoia's captured text
output needs a separate option:

``` bash
redumper-cdda --show-layout \
  --existing-dump=/archive/disc \
  --cdparanoia-toc-file=cdparanoia.txt
```

For `/archive/disc`, the application reads `/archive/disc.toc` as MMC READ TOC
format 0 and `/archive/disc.fulltoc` as MMC full TOC format 2.
`--cdparanoia-toc-file` accepts captured combined text output from
`cdparanoia -Q`. All three inputs pass through the same parsers and strict
MMC/cdparanoia reconciliation as live reads.

#### Capturing TOC files for later offline use

Redumper produces the required binary `.toc` and `.fulltoc` files as part of
its normal dump. For example, a dump with this image prefix:

``` bash
redumper dump \
  --drive=/dev/sg4 \
  --image-path=/archive \
  --image-name=disc
```

produces `/archive/disc.toc` and `/archive/disc.fulltoc` alongside the other
dump artifacts. Keep them unchanged; `--existing-dump=/archive/disc` locates
them automatically.

Capture cdparanoia's complete textual query output, including stderr, because
different cdparanoia builds may write the track table to different streams:

``` bash
cdparanoia -Q -d /dev/sg4 > cdparanoia.txt 2>&1
```

Keep `cdparanoia.txt` with the redumper dump. The captured inputs can be checked
through the normal reconciliation path without the disc:

``` bash
redumper-cdda --show-layout \
  --existing-dump=/archive/disc \
  --cdparanoia-toc-file=cdparanoia.txt
```

For a completely media-free extraction, omit the device and supply the existing
dump prefix plus the captured cdparanoia TOC:

``` bash
redumper-cdda 2 \
  --existing-dump=/archive/disc \
  --cdparanoia-toc-file=/archive/cdparanoia.txt
```

The track selection remains optional, so omitting `2` selects the normal
full-disc range. Offline extraction still requires `redumper` for splitting,
while offline layout display does not require redumper, `sg_raw`, or
cdparanoia. File read errors, malformed TOCs, and reconciliation disagreements
fail closed.

### Building the physical-test corpus

`scripts/capture_physical_test_data.py` captures the media-dependent inputs
for `PHYSICAL_TESTS.md`. Run it once for each disc profile while that disc is
inserted:

``` bash
./scripts/capture_physical_test_data.py regular-audio /dev/sg4
./scripts/capture_physical_test_data.py track0-pregap /dev/sg4
./scripts/capture_physical_test_data.py data-first /dev/sg4
./scripts/capture_physical_test_data.py data-last /dev/sg4
./scripts/capture_physical_test_data.py data-only /dev/sg4
```

Each invocation first captures and reconciles the live MMC and cdparanoia
layouts. It then performs all applicable redumper reads for that profile before
the disc is changed. Dumps are scenario-sized: their LBA ranges match the
selection in the physical test plan and include the required one-sector ending
padding. Packaging variants that have the same physical range share one dump;
invalid cases that must fail before acquisition do not create a dump. The
script does not refine the captured data. For `regular-audio` and `data-last`,
it performs one disposable automatic-offset split on a copy of the applicable
dump, records the result in `write-offset-probe.log` and the manifest, then
removes all derived probe files.

For those offset-gated profiles, the complete or mixed probe scenario is
captured first and checked immediately. An unsuitable disc therefore fails
before any of the remaining scenario dumps are started. The complete range is
intentional: a smaller audio/data range can produce a different inferred
offset and is not accepted as a substitute for the capture being validated.

The `regular-audio` capture must be clean. Offline validation copies its
single-track and all-tracks dumps, fabricates an unresolved C2 state in each
disposable copy, and exercises both the default warned-output policy and the
strict per-track/single-file policies. Captured PCM is never modified and the
source corpus remains hash-identical; redumper may replace the flagged sample
when splitting the disposable copy. A separate damaged-disc capture is not
required.

The `data-last` profile uses a clean enhanced CD whose final track is data. It
automatically uses the final two audio tracks as the AccurateRip target and
its following alignment track. Offline validation fabricates one C2 state in
a disposable copy of the alignment track's `.state` file, confirms strict
mode omits that track, and still requires the retained target to verify using
the unchanged adjacent PCM. The fabricated state is recorded and never
written to the captured source corpus.

The `regular-audio` capture is accepted only when every automatic split region
has zero write offset. The `data-last` capture requires a negative nonzero
automatic write offset needed by the following-track alignment regression.
These checks do not use `--force-offset=0`.

For the data-only profile, cdparanoia is deliberately not queried because the
MMC layout contains no audio tracks; the saved TOC text records that fact and
can still be supplied to the offline CLI, where it is not parsed.

The resulting files are stored below `test_data/PROFILE/`. `manifest.json`
records drive identity, tool/repository versions, commands, statuses, track
layout, selected tracks, and exact logical and physical ranges.
`SHA256SUMS` covers every retained file. A scenario's `existing_dump` field is
the extensionless prefix to pass to `--existing-dump`; use the profile-level
`cdparanoia-toc.txt` with `--cdparanoia-toc-file`.

`test_data/` is ignored by Git because the dump payloads and derived audio or
data can be copyrighted. The script refuses to overwrite an existing profile
directory. Move or remove a previous capture explicitly before rebuilding it.

### Validating captured data without media

After capture, run the complete media-free validation suite with:

``` bash
./scripts/validate_physical_test_data.py
```

The validator discovers profiles and scenarios from their `manifest.json`
files; it does not encode particular discs, track counts, or captured paths.
Profiles with no captured data are reported as skipped. Present but incomplete,
malformed, or nonconforming captures still fail validation.
It verifies `SHA256SUMS` before and after the run, displays every stored layout,
and extracts every scenario through `--existing-dump` and
`--cdparanoia-toc-file`. It confirms that no dump or refine command ran, each
scenario split exactly once, WAV sector counts and formats are exact, ISO9660
outputs are exactly trimmed, single-file packaging matches separate output,
mixed single-file requests fail, clean tracks match across applicable captured
ranges after accounting for redumper's reported disc write offset, and captured
SCSI/C2 fixtures obey both default and strict policies. Audio parity still
requires every shared PCM frame to match exactly; data parity requires an
exact whole-file hash match. Every eligible scenario containing numbered audio
tracks is also checked against AccurateRip. Tracks whose logical ranges have
clean captured SCSI/C2 state must verify, while tracks containing captured SCSI
or C2 errors must report no match. Track 0, data-only scenarios, and data-first
discs (typically games with CDDA tracks) are excluded from that requirement.

Generated WAVs and ISOs use a temporary directory and are removed after the
run. To inspect them, provide a new path with `--keep-work=PATH`. Restrict a
run by repeating `--profile=NAME`; use `--skip-source-hashes` only for a faster
diagnostic run. No optical device, `sg_raw`, or cdparanoia is used. Redumper
remains required for offline splitting, ARver must be installed, and the
AccurateRip checks require network access. When the default repository launcher
is used, the validator runs it with `.venv/bin/python` if that interpreter
exists; otherwise it uses the validator's current Python interpreter.

Use `--log-file=PATH` to retain a consolidated diagnostic log. A prominent
named header separates each test, followed by its commands, working
directories, exit statuses, captured output, and the verbose application logs
that would otherwise be removed with the temporary validation workspace. Each
test ends with an explicit named `TEST RESULT: PASS` or `TEST RESULT: FAIL`
section; standard output also finishes every test-case line with `PASS` or
`FAIL`. These results are separate from the extraction's SCSI/C2 integrity
status:

``` bash
./scripts/validate_physical_test_data.py --log-file=validation.log
```

`--retries` controls how many retries redumper performs for a problem area
within one dump or refinement pass. `--refine-passes` controls how many
additional passes this program may start. These are separate controls.

For a difficult disc, set the number of refinement passes to unlimited. Use
`--refine-passes=0`, or its more readable alias `--refine-forever`, to keep
starting bounded refinement passes until both SCSI and C2 reach zero:

``` bash
redumper-cdda /dev/sg4 2 --retries=100 --refine-passes=0
# Equivalent: redumper-cdda /dev/sg4 2 --retries=100 --refine-forever
```

This can run indefinitely when damage cannot be recovered. Press Ctrl-C to
stop; the temporary workspace will be cleaned up. Only a refine-pass count of
zero means “forever.” A `--retries` value of zero instead tells redumper to
perform no retries within each pass.

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
range rules remain unchanged. For a final-session data track, disc-absolute
ISO9660/Joliet addresses are rebased so the resulting session-sized ISO can be
mounted directly.

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

Normal output is concise, with a track summary and one completed progress line
per track during dumping and refinement. Each line shows the track-relative
percentage and that track's current SCSI and C2 counts.
Use `-v` or `--verbose` to show executed
commands, complete redumper output, exact ranges, split segments, and
the full integrity summary:

``` bash
redumper-cdda /dev/sg4 2 --verbose
```

Use `--log-file=PATH` to write the same complete verbose diagnostics to a
file while retaining the normal concise terminal display:

``` bash
redumper-cdda /dev/sg4 2 --log-file=rip.log
```

The log file is replaced at the start of each run. It is useful when asking
for help because it records the exact commands, redumper messages, refinement
passes, ranges, and final integrity information. It does not contain the
ripped audio or data payload.

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

## Understanding SCSI and C2 counts

CD drives report different kinds of read problems. The two counters shown
during a rip have different meanings:

-   **SCSI** means the drive could not successfully complete a low-level read
    request. This can be caused by severe damage, an unreadable area, a drive
    or connection problem, or the drive refusing the requested read. Any
    remaining SCSI count means some requested audio or data was not recovered
    normally.
-   **C2** means the drive read the sector but reported bytes or samples that
    its internal CD error correction could not fully trust. Scratches, dirt,
    deterioration, and marginal drive/media combinations commonly cause C2
    errors. Refinement rereads these locations and may reduce the count.

The preferred final result is `SCSI=0, C2=0`. By default, output is written
with a clear warning if SCSI or C2 remain after the configured passes. Use
`--abort-on-skip` when you would rather omit affected output, or
`--refine-forever` when you want to keep trying until the SCSI and C2 counts
both reach zero.

By default, the script calculates ARv1 and ARv2 checksums with ARver and
compares selected audio tracks with the AccurateRip database automatically:

``` bash
redumper-cdda /dev/sg4
redumper-cdda /dev/sg4 2
```

AccurateRip verification uses the complete MMC disc layout to identify the
pressing even when only part of the disc is selected. Track 0 and data tracks
are not tracked by AccurateRip. For mixed audio/data selections, checksum-only
WAVs account for redumper's reported split write offset using adjacent audio;
for a final enhanced-CD audio track, any unavailable tail frames are zero-filled
only inside AccurateRip's excluded final five sectors. The completed WAV files
are not shifted or changed. A database miss, network
failure, or checksum mismatch does not delete completed output, and AccurateRip
results do not replace the separate redumper SCSI/C2 integrity status. A
data-only selection is extracted normally and automatically skips AccurateRip;
it does not require an audio track or `--no-accuraterip`.

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

To inspect the complete audio/data track layout without acquiring sectors,
omit the track number and use:

``` bash
redumper-cdda /dev/sg4 --show-layout
```

The command reads and reports the reconciled TOCs only; it does not acquire or
split sectors.

## Extraction details

See [EXTRACTION.md](EXTRACTION.md) for the sector-boundary model, pregap
semantics, endpoint padding, PCM and ISO assembly, SCSI/C2 refinement,
hardware validation, and extraction invariants.
