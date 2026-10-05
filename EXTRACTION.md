# Extraction details

This document describes the sector-boundary model, output assembly, integrity
policy, and hardware-validated invariants used by `redumper-cdda`.

## Extraction algorithm

MMC `READ TOC` format 0 supplies every track's INDEX 01 start, audio/data
control bit, and the final-session lead-out. Full TOC format 2 supplies session
membership and each session's own lead-out. The next track's start establishes
an exclusive end only within the same session; the last track in an earlier
session ends at that session's lead-out. During this migration stage,
`cdparanoia -Q` independently supplies each audio track's `begin` and `length`.
The command fails closed if the two sources disagree, except for the validated
enhanced-CD case where cdparanoia extends the last audio track to a later
session's data-track start. In that case the full-TOC session lead-out wins.

When an existing dump prefix is supplied, its `.toc` and `.fulltoc` artifacts
provide the MMC format-0 and full-TOC format-2 responses. A separately captured
cdparanoia text file completes the file-backed layout. Parsing,
session-boundary derivation, audio-track-set checks, and exact boundary
reconciliation remain identical to live reads. The combined inputs permit
layout planning and extraction without physical media.

For a selected audio track:

``` text
logical_start = begin
logical_end   = begin + length
```

The end is exclusive. The output begins at Track N **INDEX 01**,
includes its program audio and the following track's **INDEX 00
pregap**, and stops immediately before Track N+1 INDEX 01. The selected
track's own INDEX 00 pregap is excluded.

For the final audio track of an earlier session, output stops at that
session's lead-out. It excludes the lead-out, inter-session gap, and the later
data track's INDEX 00 pregap.

For a range, `logical_start` is the first selected track's start and
`logical_end` is the last selected track's exclusive end. redumper reads
that complete physical range once, with the same one-sector endpoint
padding used for a single track.

A bare `-` selects Track 1 through the final numbered track, and omitting the
selection is equivalent to `-`. As with other open ranges, data tracks are
omitted unless `--include-data` is active. Track 0 is never implicit.

redumper performs the actual read. It must physically read one extra
sector at the end:

``` text
dump_start = logical_start
dump_end   = logical_end + 1
```

The extra sector exists only to give redumper enough source material for
endpoint/read-offset processing. It is not included in final WAV or ISO
output.

After the SCSI/C2 state is determined and the configured error policy is
applied, the intentionally partial image is split with
`redumper split --force-split`. Audio-only selections also use
`--force-offset=0`. Without it, redumper can mistake a bounded partial range's
endpoint for the physical disc lead-out and apply a selection-dependent write
offset; a captured single-track case applied `+540` and failed AccurateRip
while the same track from the full range verified at offset zero. Selections
containing data retain automatic offset detection because redumper uses it to
identify the data-sector mode. The generated CUE is used to locate the selected
track's file-relative INDEX 01. The script starts there, then consumes
subsequent AUDIO BINs from sector zero until exactly `cdparanoia`'s reported
track length has been collected.

When `--existing-dump=PATH` is supplied, `PATH` is the redumper image prefix
without an extension. Primary dump artifacts are copied and renamed inside the
temporary workspace; the source files are never modified. Initial dump and
refinement are skipped. Splitting supplies the write-offset mapping needed to
inspect the copied state file across the selected logical track ranges before
output policy is applied. Missing or malformed state and offset information
fails closed. SCSI/C2 values for this mode are unresolved sample counts from
the state file; historical Q counts are unavailable.

Output packaging does not change acquisition. By default, each selected track
is written separately as `PREFIXNN.wav` or `PREFIXNN.iso`. `--single-file`
combines a multi-track audio selection into `PREFIX.wav`; it does not trigger
another dump, refine, or split. The default prefix is `track` and `--prefix`
changes it.

When data output is requested, splitting also uses `--filesystem-trim`. The
selected data BIN begins at its CUE `INDEX 01`, excluding its INDEX 00 pregap.
Raw `MODE1/2352` sectors are reduced to their 2048-byte user payload, and
`MODE1/2048` is copied directly. For both Form 1 and Form 2 `MODE2/2352`
sectors, the first 2048 bytes of user data become the ISO logical block. This
supports enhanced CDs whose ISO9660 volume contains mixed Mode 2 forms; the
redumper dump remains the lossless source for the additional 276 Form 2
bytes. MODE0, malformed sectors, and unknown modes fail closed.

The converted file must contain a valid ISO9660 primary volume descriptor,
matching little- and big-endian volume sizes, and a 2048-byte logical block
size. It is trimmed to the filesystem's declared volume size. Consequently,
`trackNN.iso` is a mountable filesystem image rather than a renamed raw BIN.

### Track 0 / hidden audio

Track `0` represents the pregap before Track 1. Its logical range is:

``` text
logical_start = 0
logical_end   = Track 1 begin
```

For example, if cdparanoia reports that Track 1 begins at LBA 300,
Track 0 contains LBAs `0..300` with 300 as the exclusive endpoint. The
generated CUE must place Track 1 INDEX 01 at the same file-relative
sector. The physical redumper read still extends one sector beyond the
logical endpoint. If Track 1 begins at LBA 0, Track 0 does not exist and
the command fails without creating a WAV.

``` bash
redumper-cdda /dev/sg4 0
```

## PCM handling

Do **not** byte-swap redumper's split audio BIN data. Write the selected
bytes directly as 44.1 kHz, 16-bit, stereo PCM in the WAV container.

Do **not** manually shift the final WAV by 48 stereo frames. The
observed 48-frame difference was an alignment difference between
redumper and cdparanoia during validation, not a transformation that
belongs in production output.

## Integrity and refinement

A failed normal `redumper split` is not a valid error detector because
this project deliberately reads only part of the disc.

The preferred result is:

``` text
SCSI == 0
C2   == 0
```

Q/subchannel errors are currently reported but do not fail extraction. By
default, the script also writes output if SCSI or C2 errors remain after all
configured refinement passes. It reports the
remaining counts so the result is not mistaken for an error-free rip.

Use `-X` or `--abort-on-skip` to prevent unresolved SCSI/C2 errors from being
written. In the default separate-file mode, the final redumper sample-state
file and the write-offset mapping reported by `split` are used to classify the
exact logical LBA range of each selected track. Clean track files are retained,
affected track files are omitted, and the command exits nonzero when any file
is omitted. With `--single-file`, the range remains all-or-nothing: any
unresolved SCSI/C2 error causes a nonzero exit and no output is created.

``` bash
redumper-cdda /dev/sg4 2 --abort-on-skip
```

The desired control flow is:

``` text
initial partial dump
        |
inspect SCSI/C2 status
        |
        +-- both zero --> continue
        |
        +-- errors remain --> targeted refine
                              |
                              v
                       inspect again
                              |
                       repeat as needed
                              |
                 still nonzero at limit
                         /                    \
                  default                      -X
                     |                 separate / single
              write with warning       omit bad / FAIL all
```

Run `refine` only when SCSI or C2 errors remain. Restrict it to exactly
the same physical partial range as the original dump, including the
one-sector endpoint padding:

``` bash
redumper refine \
  --drive=/dev/sg4 \
  --image-path=... \
  --image-name=track02 \
  --retries=100 \
  --lba-start=17385 \
  --lba-end=34311
```

`--refine-passes=0` represents an unlimited pass count;
`--refine-forever` is an alias for the same setting. Unlimited refinement does
not change the stop condition: refinement stops as soon as SCSI and C2 are
both zero. Q remains informational, is not explicitly refined, and does not
independently start or continue refinement.

If the script cannot reliably determine the SCSI/C2 state, it should
fail closed rather than create apparently verified output. A
structured/state-based redumper error query is preferable to fragile
console parsing if one can be validated.

Some redumper builds probe every data track in the stored full TOC while
splitting, including an unselected later-session data track outside a partial
image. Before splitting a range that ends before the disc lead-out, temporarily
replace redumper's stored format-0 TOC with a bounded view containing only the
tracks intersecting the acquired logical range and an `AA` lead-out at the
logical endpoint. Temporarily hide the full-TOC sidecar so redumper cannot
replace that bounded view with later sessions. Restore both metadata files
immediately after splitting. This prevents unselected lead-out, inter-session,
and later data-track ranges from entering split analysis; selected output must
still pass the normal CUE and exact-sector validation.

## AccurateRip verification

AccurateRip is a post-extraction check implemented with the
[ARver](https://pypi.org/project/ARver/) Python library, which is a required
package dependency. Verification runs by default and can be disabled with
`--no-accuraterip`. The complete reconciled MMC layout supplies all track
offsets and lead-out for the AccurateRip disc ID, while checksums are calculated
only for selected audio tracks. Combined output is temporarily separated at the
already established logical boundaries for checksumming; this does not trigger
another redumper dump or split.

ARv2 matches are preferred, with ARv1 used as a fallback. Report the matching
version, checksum, and database confidence for each selected audio track.
Track 0 and data tracks are not represented in AccurateRip and are not
checksummed. When a selection contains no numbered audio tracks, AccurateRip
is skipped automatically and data extraction continues normally.

AccurateRip is independent corroboration, not a replacement for redumper's
SCSI/C2 status. A database miss, lookup failure, or checksum mismatch must not
delete successfully completed output. Unsupported audio/data layouts fail
before acquisition when verification is active.

## Empirical validation

For the tested Track 2:

``` text
cdparanoia begin:  17385
cdparanoia length: 16925 sectors
logical end:       34310
physical dump end: 34311
```

Expected raw PCM payload:

``` text
16925 * 2352 = 39,807,600 bytes
```

An early version byte-swapped the PCM and produced millions of
mismatches. Removing the swap reduced the discrepancy to the final
sector. That remaining mismatch consisted of 540 stereo frames of
`0x5555`-style fill data. Since a CDDA sector has 588 stereo frames and
the drive offset was +48, `588 - 48 = 540`. Reading one additional
physical sector eliminated the endpoint problem.

After removing the byte swap and adding the extra endpoint sector,
comparison against a cdparanoia rip after accounting for their
48-stereo-frame alignment difference produced:

``` text
Channel samples compared:   19,903,704
Differing channel samples:  0
Identical channel samples:  19,903,704
Match:                      100.000000000%
```

This validates the underlying audio samples; it is not a reason to shift
the production WAV.

## Regression comparison

A useful hardware regression test is:

``` bash
cdparanoia -d /dev/sg4 2 cdparanoia-track02.wav

ffmpeg -v error -i track02.wav \
  -f s16le -acodec pcm_s16le - > redumper.pcm

ffmpeg -v error -i cdparanoia-track02.wav \
  -f s16le -acodec pcm_s16le - > cdparanoia.pcm
```

For the validated drive/workflow, comparison used a 48-stereo-frame
alignment. Do not encode 48 as a universal production offset.

## Important invariants

-   cdparanoia determines boundaries only; redumper extracts the audio.
-   Track 0 spans LBA 0 through Track 1's start, end exclusive.
-   A range starts at its first selected track boundary and ends at its
    last selected track's exclusive logical endpoint.
-   A range uses one physical dump and one force-split operation.
-   Batch mode changes WAV/ISO packaging only; it does not perform separate
    per-track dumps.
-   For Tracks 1+, output starts at the selected Track INDEX 01.
-   For Tracks 1+, the selected Track INDEX 00 pregap is excluded.
-   Following Track INDEX 00 pregap is included.
-   Output is exactly cdparanoia's reported track length.
-   redumper reads one extra physical ending sector.
-   No endian swap.
-   No manual +48-frame shift.
-   SCSI/C2 counts must always be determined and reported.
-   Data ISO output begins at INDEX 01 and contains 2048-byte sectors.
-   Data ISO output is validated and trimmed to its ISO9660 volume size.
-   By default, unresolved SCSI/C2 errors produce warned output.
-   With `--abort-on-skip`, separate output omits only tracks whose logical
    ranges contain unresolved SCSI/C2 states and retains clean track files.
-   With `--abort-on-skip --single-file`, unresolved SCSI/C2 errors produce no
    output.
-   Q is currently informational.
-   Refine only when SCSI/C2 remain.
-   Refine only the same partial physical LBA range.
-   Partial-disc split completeness must not be confused with C2/SCSI
    integrity.
-   Failed status detection or conversion must not leave misleading WAV or
    ISO output.

## Philosophy

WAV and ISO files are derived convenience artifacts. redumper's recovered
data/state is the preservation-oriented source. Avoid modifying recovered
samples or sectors except as necessary to select the desired logical range
and package it into the requested container.

[Back to the README](README.md)
