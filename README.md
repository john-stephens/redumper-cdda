# riptrack-redump

`riptrack-redump` extracts one audio-CD track on Linux using
**cdparanoia** only for logical track layout and **redumper** for the
actual preservation-oriented extraction.

## Requirements

-   Linux / Python 3
-   `cdparanoia`
-   `redumper`
-   Optical drive exposed as a SCSI generic device such as `/dev/sg4`

## Usage

``` bash
./riptrack-redump /dev/sg4 2
```

Typical optional controls:

``` bash
./riptrack-redump /dev/sg4 2 --retries=100 --refine-passes=3
```

Normal output is concise, with a track summary and single-line progress
for dumping and refinement. Use `-v` or `--verbose` to show executed
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

Each run creates a unique temporary workspace for redumper's dump,
state, BIN, and CUE files. The workspace is removed after success,
failure, or interruption. The completed WAV is written outside that
workspace and is retained.

Use `--output=PATH` to choose the final WAV filename and location. By
default, Track N is written as `trackNN.wav` in the current directory.

To inspect the disc's audio-track boundaries without dumping anything,
omit the track number and use:

``` bash
./riptrack-redump /dev/sg4 --show-layout
```

## Extraction algorithm

`cdparanoia -Q` supplies the selected track's `begin` and `length`:

``` text
logical_start = begin
logical_end   = begin + length
```

The end is exclusive. The output begins at Track N **INDEX 01**,
includes its program audio and the following track's **INDEX 00
pregap**, and stops immediately before Track N+1 INDEX 01. The selected
track's own INDEX 00 pregap is excluded.

redumper performs the actual read. It must physically read one extra
sector at the end:

``` text
dump_start = logical_start
dump_end   = logical_end + 1
```

The extra sector exists only to give redumper enough source material for
endpoint/read-offset processing. It is not included in the final WAV.

After the SCSI/C2 state is determined and the configured error policy is
applied, the intentionally partial image is split with
`redumper split --force-split`. The generated CUE is used to locate the
selected track's file-relative INDEX 01. The script starts there, then
consumes subsequent AUDIO BINs from sector zero until exactly
`cdparanoia`'s reported track length has been collected.

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

Q/subchannel errors are currently reported but do not fail audio
extraction. By default, the script also writes the WAV if SCSI or C2
errors remain after all configured refinement passes. It reports the
remaining counts so the result is not mistaken for an error-free rip.

Use `-X` or `--abort-on-skip` to require SCSI and C2 to both reach zero.
With that option, unresolved SCSI/C2 errors cause a nonzero exit and no
WAV is created:

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
redumper refine   --drive=/dev/sg4   --image-path=...   --image-name=track02   --retries=100   --lba-start=17385   --lba-end=34311
```

If the script cannot reliably determine the SCSI/C2 state, it should
fail closed rather than create an apparently verified WAV. A
structured/state-based redumper error query is preferable to fragile
console parsing if one can be validated.

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

ffmpeg -v error -i track02.wav   -f s16le -acodec pcm_s16le - > redumper.pcm

ffmpeg -v error -i cdparanoia-track02.wav   -f s16le -acodec pcm_s16le - > cdparanoia.pcm
```

For the validated drive/workflow, comparison used a 48-stereo-frame
alignment. Do not encode 48 as a universal production offset.

## Important invariants

-   cdparanoia determines boundaries only; redumper extracts the audio.
-   Track 0 spans LBA 0 through Track 1's start, end exclusive.
-   For Tracks 1+, output starts at the selected Track INDEX 01.
-   For Tracks 1+, the selected Track INDEX 00 pregap is excluded.
-   Following Track INDEX 00 pregap is included.
-   Output is exactly cdparanoia's reported track length.
-   redumper reads one extra physical ending sector.
-   No endian swap.
-   No manual +48-frame shift.
-   SCSI/C2 counts must always be determined and reported.
-   By default, unresolved SCSI/C2 errors produce a warned WAV.
-   With `--abort-on-skip`, unresolved SCSI/C2 errors produce no WAV.
-   Q is currently informational.
-   Refine only when SCSI/C2 remain.
-   Refine only the same partial physical LBA range.
-   Partial-disc split completeness must not be confused with C2/SCSI
    integrity.
-   Failed status detection or conversion must not leave a misleading
    WAV.

## Philosophy

The WAV is a derived convenience artifact. redumper's recovered
data/state is the preservation-oriented source. Avoid modifying
recovered samples unless necessary to select the desired logical range
and package it into WAV.
