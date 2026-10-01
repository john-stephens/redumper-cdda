# AGENTS.md

## Mission

Maintain `riptrack-redump`, a Linux utility that extracts one or more
contiguous CD tracks with redumper. It reads the complete track layout
with MMC `READ TOC` and retains cdparanoia as the validated authority for
audio-track boundaries during the MMC migration.

Read `README.md` and `EXTRACTION.md` before changing extraction behavior. The
rules below encode behavior already established through hardware testing and
must be treated as regression constraints.

## Reference documentation

The `ref/` directory contains captured command-line help for the external
tools used by this project:

-   `ref/cdparanoia_help.txt`
-   `ref/redumper_help.txt`

Consult these files when changing command construction, option handling,
or output parsing. Treat them as local reference snapshots; they provide
context but do not override the extraction invariants in this file.

## Non-negotiable extraction invariants

### Disc layout sources

Use MMC `READ TOC` format 0 to enumerate every numbered track, its audio/data
control bit, its INDEX 01 start LBA, and lead-out. Derive each exclusive end
from the next track's start or lead-out. `--show-layout` must display both
audio and data tracks.

For now, also read the audio-only cdparanoia layout. Every MMC audio track must
exist in cdparanoia, every cdparanoia track must be audio in MMC, and their
`begin`, `end`, and `length` values must agree exactly. Fail closed on any
disagreement. Continue to use the reconciled cdparanoia values for audio
extraction until hardware validation explicitly authorizes removing that
dependency.

### Logical range

For Track N where N is 1 or greater:

``` text
logical_start = cdparanoia begin
logical_end   = begin + length
```

`logical_end` is exclusive. Final PCM payload must contain exactly:

``` text
track.length * 2352 bytes
```

Track 0 is the explicit exception used for hidden audio in the pregap
before Track 1:

``` text
logical_start = 0
logical_end   = Track 1 begin
length        = Track 1 begin
```

Fail if Track 1 begins at LBA 0 because no positive-length Track 0
exists. After splitting, Track 1's file-relative INDEX 01 must equal the
Track 0 length reported by cdparanoia. Track 0 output starts at sector
zero of Track 1's AUDIO BIN and stops immediately before INDEX 01.

### Track ranges

Accepted selections are `N`, `N-M`, `-M`, and `N-`. An omitted start
means Track 1, never Track 0. Track 0 must be explicit, such as `0-3`.

Treat cdparanoia's parsed track list as the audio-track set. Fully
bounded `N-M` selections are strict and must fail if any number in the
range is not an audio track. Open-start and open-end selections omit
non-audio tracks automatically, but their explicitly supplied endpoint
must itself be audio. Thus, with data Track 1, `-3` selects audio Tracks
2 and 3 while `1-3` fails.

When `-d` or `--include-data` is active, resolve the selection against the
full MMC layout and include both audio and data tracks. Mixed or multi-track
data selection requires `-B`/`--batch`. Without batch mode,
`--include-data` is valid only for one explicit data-track number. Track 0
remains audio-only and is valid only when Track 1 is audio.

For a resolved contiguous selection:

``` text
logical_start = first selected track begin
logical_end   = last selected track end
physical_sectors = logical_end - logical_start
audio_output_sectors = sum(selected audio track lengths)
data_output_sectors = ISO9660 declared volume size for each data track
```

Perform exactly one initial redumper dump for the entire range and use
the same entire range for refinement. Never implement ranges as one
redumper dump per track. A physical range may cross omitted data tracks;
do not include their payload when assembling WAV output.

Without `--batch`, a multi-track range produces one `track.wav`. With
`-B` or `--batch`, split the already dumped range at the established
logical track boundaries and write `trackNN.wav` files. Batch mode is a
packaging step and must not trigger additional dump, refine, or split
commands.

With `--include-data --batch`, write audio tracks as `trackNN.wav` and data
tracks as `trackNN.iso`. A single explicit data track may produce
`trackNN.iso` without batch mode. `--output` may override one non-batch output
but remains incompatible with batch mode.

### Data-track ISO semantics

Data output begins at the selected CUE track's file-relative INDEX 01,
excluding its INDEX 00 pregap. Require a dedicated split BIN for the selected
data track; fail rather than guessing boundaries in a shared BIN.

When any selected output is data, pass `--filesystem-trim` to the one
`redumper split --force-split` command. Convert supported split modes as
follows:

``` text
MODE1/2352        bytes 16..2064 -> 2048-byte ISO sector
MODE2/2352 Form 1 bytes 24..2072 -> 2048-byte ISO sector
MODE1/2048        copy directly
```

Validate raw sync, mode bytes, duplicated Mode 2 subheaders, and the Form 1
bit. Fail closed for MODE0, Mode 2 Form 2, mixed forms, malformed sectors, or
unknown modes.

Require a valid ISO9660 primary volume descriptor. Its little- and big-endian
volume-space sizes must agree, its logical block size must be 2048, and its
declared volume must fit in the converted track. Truncate the ISO to that
declared volume size. Never create an `.iso` by renaming or directly copying a
2352-byte raw BIN.

### Audio pregap semantics

For Track N where N is 1 or greater, output means:

``` text
Track N INDEX 01
    through
just before Track N+1 INDEX 01
```

For Track 0 specifically, output means LBA 0 through just before Track 1
INDEX 01.

Therefore:

-   exclude selected Track N's INDEX 00 pregap;
-   include Track N+1's INDEX 00 pregap.

Do not equate the desired output with the entirety of redumper's Track-N
BIN. Split BINs may begin at INDEX 00. Parse the generated CUE and begin
the selected BIN at its file-relative INDEX 01. Consume subsequent AUDIO
BINs from sector zero until the exact logical sector count is reached.

### Endpoint padding

The physical redumper read must extend one sector beyond the logical
endpoint:

``` text
dump_start = logical_start
dump_end   = logical_end + 1
```

The end is exclusive.

Never include that extra sector in a WAV or ISO. It exists to provide source
samples for redumper's endpoint/read-offset handling.

This was empirically required: without it, the final sector contained
540 stereo frames of `0x5555`-style fill. With it, the comparison
matched.

### PCM

Never endian-swap redumper split audio. An earlier swap caused
`1 <-> 256`-style corruption.

Write selected split BIN bytes directly as:

``` text
44,100 Hz
16-bit
stereo
PCM
```

Do not normalize, resample, insert silence, perform DSP, or otherwise
alter samples.

### Offset

Never add a manual 48-stereo-frame shift to production output.

A 48-frame relative alignment was observed between redumper and
cdparanoia on the validated drive. After accounting for it only during
comparison, underlying PCM matched 100%. The value is not a universal
production correction.

## Validated fixture

Known Track 2:

``` text
begin:             17385
length:            16925 sectors
logical end:       34310
physical dump end: 34311
PCM payload:       39,807,600 bytes
```

Validated comparison after 48-frame comparison alignment:

``` text
Channel samples compared:  19,903,704
Differing channel samples: 0
Match:                     100.000000000%
```

Preserve this behavior.

## SCSI/C2 policy

The preferred error-free result is:

``` text
SCSI == 0
C2   == 0
```

Q/subchannel errors are currently informational and must not independently
fail extraction unless requirements explicitly change.

The default behavior is to create output even if SCSI or C2 errors remain
after the configured refinement passes. Print the final counts and clearly
identify the result as having unresolved errors.

When `-X` or `--abort-on-skip` is supplied, SCSI and C2 must both be
zero. If either remains nonzero, exit nonzero and create no final output.

### Never use normal split failure as the C2 detector

This is intentionally a partial-disc dump. A normal `redumper split` may
fail simply because most of the disc was never read.

Therefore a split failure says nothing conclusive about SCSI/C2 health
of the requested range.

### Error-state detection

Determine redumper's current media-error counts independently of split
completeness.

The current implementation direction parses status equivalent to:

``` text
media errors:
  SCSI: N
  C2: N
  Q: N
```

Fail closed if SCSI/C2 state cannot be determined reliably.

Prefer a validated structured/state-based redumper mechanism over
human-readable console parsing if one exists. Before replacing the
parser, prove the new method correctly reports unresolved errors for
partial dumps.

## Refinement

Never refine unconditionally.

Required flow:

``` text
initial partial dump
       |
inspect SCSI/C2
       |
       +-- 0/0 --> no refine
       |
       +-- nonzero --> refine partial range
                         |
                      inspect
                         |
                 repeat until 0/0
                    or limit
```

Refinement must use the same physical LBA range as the initial read,
including endpoint padding:

``` bash
redumper refine   --drive=DEVICE   --image-path=PATH   --image-name=NAME   --retries=N   --lba-start=DUMP_START   --lba-end=DUMP_END
```

Never run an unconstrained refine because it may attempt to process the
full disc.

Stop refinement immediately when SCSI and C2 are both zero. If the configured
maximum passes are exhausted with errors remaining, apply the selected policy:
write warned output by default, or exit nonzero and create no output when
`--abort-on-skip` is active.

## Partial splitting

Once SCSI/C2 status has been independently determined and the selected
error policy allows extraction, `redumper split --force-split` may be
used because the image is intentionally incomplete.

`--force-split` is a representation/extraction step, not an integrity
check.

## Failure policy

Fail closed and do not leave misleading WAV or ISO output when:

-   TOC parsing fails;
-   requested track does not exist;
-   redumper dump fails;
-   SCSI/C2 state cannot be determined;
-   SCSI/C2 remain after allowed refinement and `--abort-on-skip` is
    active;
-   force-split fails;
-   CUE cannot be found or parsed;
-   selected audio track lacks INDEX 01;
-   selected data track lacks INDEX 01 or has a shared/unsupported BIN;
-   required BINs are missing/malformed;
-   split data cannot provide exactly the requested logical length;
-   WAV conversion short-reads or otherwise fails;
-   data-sector conversion or ISO9660 validation fails.

If output creation has begun when a failure occurs, remove every incomplete
WAV, ISO, and temporary sibling file.

For batch output, commit final names only after every conversion succeeds. A
failure or interruption must remove the entire new output set so a partial
batch is never presented as complete.

Create redumper's intermediate files in a unique temporary workspace.
Remove that workspace after success, failure, SIGINT, SIGHUP, or SIGTERM.
Stop an active child process before removing its workspace. Keep only
successfully completed final outputs.

## Tests Codex should add/maintain

Prefer unit tests for pure parsing/range functions. Cover:

-   MMC TOC parsing for audio tracks, data tracks, and lead-out;
-   malformed or truncated MMC TOC responses;
-   exact MMC/cdparanoia audio-boundary reconciliation;
-   MMC/cdparanoia type or boundary disagreement fails closed;
-   cdparanoia TOC parsing;
-   media-error parsing for clean output;
-   nonzero SCSI;
-   nonzero C2;
-   Q-only errors;
-   multiple media-error blocks and correct selection of current/final
    status;
-   missing/malformed media-error status fails closed;
-   CUE parsing;
-   INDEX 01 conversion;
-   selected track with no pregap;
-   selected track with an INDEX 00 pregap;
-   Track 0 with a positive Track 1 start;
-   Track 0 rejection when Track 1 begins at LBA 0;
-   Track 0 CUE INDEX 01 agreement with cdparanoia length;
-   following track pregap inclusion;
-   exact range assembly across two or more BINs;
-   all four track selection forms;
-   open-start ranges exclude Track 0;
-   open ranges omit data tracks;
-   bounded ranges reject explicitly selected data tracks;
-   `--include-data` ranges select audio and data tracks;
-   non-batch `--include-data` accepts only one explicit data track;
-   mixed batch naming uses `trackNN.wav` and `trackNN.iso`;
-   data CUE parsing starts at INDEX 01;
-   MODE1/2352 and MODE2/2352 Form 1 payload extraction;
-   Mode 2 Form 2 and unsupported modes fail closed;
-   ISO9660 primary-volume validation and exact filesystem trimming;
-   mixed batches still use one dump, refine range, and split;
-   mixed-batch failure removes the entire output set;
-   combined ranges produce one `track.wav`;
-   batch ranges produce correctly bounded `trackNN.wav` files;
-   insufficient split data;
-   exact PCM payload size.

When hardware/media are available, run the validated integration
comparison against cdparanoia. A production change affecting extraction
must not introduce any sample differences in the established comparison.

## Known regressions to avoid

1.  Endian swapping.
2.  Ending the physical read at `logical_end` rather than
    `logical_end + 1`.
3.  Including the endpoint padding sector in WAV output.
4.  Starting selected split BIN at sector zero when INDEX 01 is later.
5.  Ending output at the selected BIN and losing the next track's
    pregap.
6.  Treating normal split failure as proof of C2/SCSI errors.
7.  Running refine on every dump.
8.  Running refine without the partial LBA bounds.
9.  Treating `--force-split` as proof of integrity.
10. Shifting production output by 48 frames.
11. Assuming inability to parse error status means zero errors.

## Coding guidance

Keep sector arithmetic explicit and auditable. Prefer names such as:

``` text
logical_start_lba
logical_end_lba
dump_start_lba
dump_end_lba
expected_sectors
```

Use subprocess argument arrays, not shell strings. Print exact ranges
and final SCSI/C2 counts in diagnostics.

Avoid abstractions that make it difficult to determine exactly which
sectors are read or copied.

## Pre-commit review for extraction changes

Before completing a change, verify:

-   [ ] cdparanoia is still boundary-only;
-   [ ] Tracks 1+ begin at the selected Track N INDEX 01;
-   [ ] Track 0 begins at LBA 0 and ends at Track 1 INDEX 01;
-   [ ] a range is acquired with one dump over its full physical range;
-   [ ] batch mode only changes WAV/ISO packaging;
-   [ ] Track N's own pregap is excluded;
-   [ ] Track N+1's pregap is included;
-   [ ] final sector count equals cdparanoia length;
-   [ ] physical read includes one extra ending sector;
-   [ ] endpoint padding is not added to WAV or ISO;
-   [ ] no endian swap exists;
-   [ ] no manual sample/frame shift exists;
-   [ ] SCSI/C2 integrity is independent of partial split completeness;
-   [ ] refine runs only when SCSI/C2 require it;
-   [ ] refine is restricted to the partial physical range;
-   [ ] unresolved SCSI/C2 follows the selected default or abort policy;
-   [ ] output PCM payload is exactly `length * 2352` bytes;
-   [ ] data output begins at INDEX 01 and contains 2048-byte sectors;
-   [ ] ISO output is validated and trimmed to its ISO9660 volume size;
-   [ ] failure paths remove incomplete WAV and ISO output.

If a proposed change violates an invariant, explain the reason and
provide empirical evidence before implementing it.
