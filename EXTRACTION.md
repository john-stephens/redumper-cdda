# Extraction details

This document describes the sector-boundary model, output assembly, integrity
policy, and hardware-validated invariants used by `riptrack-redump`.

## Extraction algorithm

An MMC `READ TOC` command supplies every track's INDEX 01 start, audio/data
control bit, and the lead-out address. The next track's start, or lead-out for
the final track, establishes each exclusive end. During this migration stage,
`cdparanoia -Q` independently supplies each audio track's `begin` and `length`.
The command fails closed if the two sources disagree about any audio track's
type or sector boundaries.

For a selected audio track:

``` text
logical_start = begin
logical_end   = begin + length
```

The end is exclusive. The output begins at Track N **INDEX 01**,
includes its program audio and the following track's **INDEX 00
pregap**, and stops immediately before Track N+1 INDEX 01. The selected
track's own INDEX 00 pregap is excluded.

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
`redumper split --force-split`. The generated CUE is used to locate the
selected track's file-relative INDEX 01. The script starts there, then
consumes subsequent AUDIO BINs from sector zero until exactly
`cdparanoia`'s reported track length has been collected.

Output packaging does not change acquisition. By default, each selected track
is written separately as `PREFIXNN.wav` or `PREFIXNN.iso`. `--single-file`
combines a multi-track audio selection into `PREFIX.wav`; it does not trigger
another dump, refine, or split. The default prefix is `track` and `--prefix`
changes it.

When data output is requested, splitting also uses `--filesystem-trim`. The
selected data BIN begins at its CUE `INDEX 01`, excluding its INDEX 00 pregap.
Raw `MODE1/2352` sectors are reduced to their 2048-byte user payload;
`MODE2/2352` is accepted only for Form 1 sectors, and `MODE1/2048` is copied
directly. MODE0, Mode 2 Form 2, malformed sectors, and unknown modes fail
closed.

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
./riptrack-redump /dev/sg4 0
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

Use `-X` or `--abort-on-skip` to require SCSI and C2 to both reach zero.
With that option, unresolved SCSI/C2 errors cause a nonzero exit and no
output is created:

``` bash
./riptrack-redump /dev/sg4 2 --abort-on-skip
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
                         /           \
                  default             -X
                     |                 |
              write with warning     FAIL
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

If the script cannot reliably determine the SCSI/C2 state, it should
fail closed rather than create apparently verified output. A
structured/state-based redumper error query is preferable to fragile
console parsing if one can be validated.

## AccurateRip verification

AccurateRip is a post-extraction check implemented with the
[ARver](https://pypi.org/project/ARver/) Python library. It runs by default when
ARver and its runtime dependencies are installed, and can be disabled with
`--no-accuraterip`. The complete reconciled MMC layout supplies all track
offsets and lead-out for the AccurateRip disc ID, while checksums are calculated
only for selected audio tracks. Combined output is temporarily separated at the
already established logical boundaries for checksumming; this does not trigger
another redumper dump or split.

ARv2 matches are preferred, with ARv1 used as a fallback. Report the matching
version, checksum, and database confidence for each selected audio track.
Track 0 and data tracks are not represented in AccurateRip and are not
checksummed.

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
-   With `--abort-on-skip`, unresolved SCSI/C2 errors produce no output.
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
