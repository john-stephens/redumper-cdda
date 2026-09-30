# AGENTS.md

## Mission

Maintain `riptrack-redump`, a Linux utility that extracts one audio-CD
track with redumper while using cdparanoia only to determine logical
track boundaries.

Read `README.md` before changing extraction behavior. The rules below
encode behavior already established through hardware testing and must be
treated as regression constraints.

## Non-negotiable extraction invariants

### Logical range

For Track N:

``` text
logical_start = cdparanoia begin
logical_end   = begin + length
```

`logical_end` is exclusive. Final PCM payload must contain exactly:

``` text
track.length * 2352 bytes
```

### Pregap semantics

Output means:

``` text
Track N INDEX 01
    through
just before Track N+1 INDEX 01
```

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

Never include that extra sector in the WAV. It exists to provide source
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

Q/subchannel errors are currently informational and must not
independently fail audio extraction unless requirements explicitly
change.

The default behavior is to create the WAV even if SCSI or C2 errors
remain after the configured refinement passes. Print the final counts
and clearly identify the result as having unresolved errors.

When `-X` or `--abort-on-skip` is supplied, SCSI and C2 must both be
zero. If either remains nonzero, exit nonzero and create no final WAV.

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

Stop refinement immediately when SCSI and C2 are both zero. If the
configured maximum passes are exhausted with errors remaining, apply
the selected policy: write a warned WAV by default, or exit nonzero and
create no WAV when `--abort-on-skip` is active.

## Partial splitting

Once SCSI/C2 status has been independently determined and the selected
error policy allows extraction, `redumper split --force-split` may be
used because the image is intentionally incomplete.

`--force-split` is a representation/extraction step, not an integrity
check.

## Failure policy

Fail closed and do not leave a misleading WAV when:

-   TOC parsing fails;
-   requested track does not exist;
-   redumper dump fails;
-   SCSI/C2 state cannot be determined;
-   SCSI/C2 remain after allowed refinement and `--abort-on-skip` is
    active;
-   force-split fails;
-   CUE cannot be found or parsed;
-   selected audio track lacks INDEX 01;
-   required BINs are missing/malformed;
-   split data cannot provide exactly the requested logical length;
-   WAV conversion short-reads or otherwise fails.

If WAV creation has begun when a failure occurs, remove the incomplete
WAV.

Create redumper's intermediate files in a unique temporary workspace.
Remove that workspace after success, failure, SIGINT, SIGHUP, or SIGTERM.
Stop an active child process before removing its workspace. Keep only a
successfully completed final WAV.

## Tests Codex should add/maintain

Prefer unit tests for pure parsing/range functions. Cover:

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
-   following track pregap inclusion;
-   exact range assembly across two or more BINs;
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
-   [ ] selected output begins at Track N INDEX 01;
-   [ ] Track N's own pregap is excluded;
-   [ ] Track N+1's pregap is included;
-   [ ] final sector count equals cdparanoia length;
-   [ ] physical read includes one extra ending sector;
-   [ ] endpoint padding is not added to WAV;
-   [ ] no endian swap exists;
-   [ ] no manual sample/frame shift exists;
-   [ ] SCSI/C2 integrity is independent of partial split completeness;
-   [ ] refine runs only when SCSI/C2 require it;
-   [ ] refine is restricted to the partial physical range;
-   [ ] unresolved SCSI/C2 follows the selected default or abort policy;
-   [ ] output PCM payload is exactly `length * 2352` bytes;
-   [ ] failure paths remove incomplete WAV output.

If a proposed change violates an invariant, explain the reason and
provide empirical evidence before implementing it.
