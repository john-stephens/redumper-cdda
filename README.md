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

After the dump passes the SCSI/C2 integrity check, the intentionally
partial image is split with `redumper split --force-split`. The
generated CUE is used to locate the selected track's file-relative INDEX
01. The script starts there, then consumes subsequent AUDIO BINs from
sector zero until exactly `cdparanoia`'s reported track length has been
collected.

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

The acceptance condition for audio data is:

``` text
SCSI == 0
C2   == 0
```

Q/subchannel errors are currently reported but do not fail audio
extraction.

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
                              |
                             FAIL
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
-   Output starts at selected Track INDEX 01.
-   Selected Track INDEX 00 pregap is excluded.
-   Following Track INDEX 00 pregap is included.
-   Output is exactly cdparanoia's reported track length.
-   redumper reads one extra physical ending sector.
-   No endian swap.
-   No manual +48-frame shift.
-   SCSI and C2 must be zero.
-   Q is currently informational.
-   Refine only when SCSI/C2 remain.
-   Refine only the same partial physical LBA range.
-   Partial-disc split completeness must not be confused with C2/SCSI
    integrity.
-   Failed integrity/conversion must not leave a misleading WAV.

## Philosophy

The WAV is a derived convenience artifact. redumper's recovered
data/state is the preservation-oriented source. Avoid modifying
recovered samples unless necessary to select the desired logical range
and package it into WAV.
