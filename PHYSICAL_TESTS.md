# Physical-disc validation plan

This plan validates `redumper-cdda` against five representative physical CD
conditions and layouts:

1. regular audio CD;
2. audio CD with a Track 1 pregap containing hidden Track 0 audio;
3. audio CD with a data track first;
4. clean enhanced CD with a data track last, a nonzero split offset, and two
   adjacent clean audio tracks used for a synthetic strict-policy regression;
5. data-only CD.

The suite emphasizes exact boundaries, output equivalence across extraction
modes, mixed-mode handling, integrity policy, and reuse of existing redumper
dumps. Not every CLI permutation needs to be tested on every disc.

## Capturing offline test data

Use `scripts/capture_physical_test_data.py` to capture one disc profile at a
time. It performs every applicable scenario-sized redumper read for the
inserted disc before returning, so the media need not be revisited while
capturing another profile:

``` bash
./scripts/capture_physical_test_data.py regular-audio /dev/sg4
./scripts/capture_physical_test_data.py track0-pregap /dev/sg4
./scripts/capture_physical_test_data.py data-first /dev/sg4
./scripts/capture_physical_test_data.py data-last /dev/sg4
./scripts/capture_physical_test_data.py data-only /dev/sg4
```

The captures live under the Git-ignored `test_data/` directory. Every scenario
contains a redumper dump whose physical range matches the corresponding live
test, including endpoint padding. See its profile-level `manifest.json` for
the mapped case names, extensionless `--existing-dump` prefix, exact ranges,
commands, statuses, and drive identity. Use the profile-level
`cdparanoia-toc.txt` for `--cdparanoia-toc-file`. Packaging-only variants reuse
the same captured dump because they do not change acquisition.

The regular-audio capture must remain clean. The offline validator fabricates
C2 states only in disposable copies of its single-track and all-tracks dumps;
no damaged disc or separate error profile is required.

For `data-last`, use an enhanced CD whose final track is data. The script
automatically uses the final two audio tracks as clean target Track `C` and
following alignment Track `N`, then captures the full numbered-track range
through data Track `D`. Its mixed split must report a nonzero offset.

Capture performs automatic-offset probes on disposable copies of the complete
regular-audio and data-last ranges. The regular-audio profile requires zero;
the data-last profile requires a negative nonzero offset. Probe commands never use
`--force-offset=0`; their output and parsed
LBA/offset mapping are stored in the capture manifest while derived CUE/BIN
files are discarded. Each probe scenario is captured first and checked
immediately, before any remaining scenarios are acquired. Do not replace the
complete probe with a smaller audio/data range: redumper may infer a different
offset for that bounded selection.

After all profiles have been captured, run every media-free case with:

``` bash
./scripts/validate_physical_test_data.py
```

The validator reads only the capture manifests and dump files, creates outputs
in temporary storage, and rechecks every source hash afterward. It requires
redumper for splitting but does not require or access an optical device.
Profiles for which no capture data exists are reported as skipped; invalid or
incomplete data that is present remains a failure.

## Test records and common validation

For every run, retain:

-   the command and exit status;
-   output from `--show-layout`;
-   the file written by `--log-file`;
-   SHA-256 hashes of every resulting WAV and ISO;
-   the redumper and `redumper-cdda` versions;
-   the optical-drive model and firmware revision.

Use a separate output directory for each case. Unless a case deliberately
tests failure cleanup, begin with no output files at the requested final
paths.

Apply these checks wherever they are relevant:

-   `--show-layout` reports every audio and data track with correct INDEX 01
    starts, exclusive ends, sessions, and session lead-outs.
-   A clean disc finishes with `SCSI=0, C2=0`.
-   A clean initial dump does not invoke `redumper refine`.
-   A selected range uses exactly one dump and one split, not one dump per
    track.
-   Refine commands use the same bounded physical range as the initial dump.
-   The same track has identical PCM when extracted alone or as part of a
    larger range.
-   Imported-dump extraction produces the same output as live extraction.
-   Temporary workspaces disappear after success, failure, and interruption.
-   AccurateRip results are identical whether a track is extracted alone or
    as part of a range.
-   A WAV's PCM payload contains exactly `track length * 2352` bytes.
-   An ISO contains exactly `ISO9660 volume-space size * 2048` bytes.
-   Normal terminal output remains concise. Full redumper and WAV-conversion
    diagnostics appear only with `--verbose` or in the `--log-file` output.

Record output hashes with:

``` bash
sha256sum track*.wav track*.iso
```

When WAV container metadata could differ, compare decoded PCM rather than
relying only on the whole-file hash.

## A. Regular audio CD

Use a disc with at least three tracks whose Track 1 begins at LBA 0.

### A01 - Layout

``` bash
redumper-cdda /dev/sg4 --show-layout
```

Expected:

-   Every numbered track is audio.
-   Track 1 begins at LBA 0.
-   Each non-final track ends at the following track's INDEX 01.
-   The final track ends at the session lead-out.

### A02 - First, middle, and final tracks individually

``` bash
redumper-cdda /dev/sg4 1 --log-file=A02-1.log
redumper-cdda /dev/sg4 2 --log-file=A02-2.log
redumper-cdda /dev/sg4 N --log-file=A02-N.log
```

Replace `N` with the final track number. Validate the exact payload length and
record the AccurateRip result for every output.

### A03 - Multi-track separate extraction

``` bash
redumper-cdda /dev/sg4 1-N --log-file=A03.log
```

Expected:

-   The range uses one dump and one split.
-   Dumping and refinement display one completed progress line per track.
-   Files `track01.wav` through `trackNN.wav` are produced.
-   The tested first, middle, and final WAVs match their A02 counterparts.

### A04 - Combined output

``` bash
redumper-cdda /dev/sg4 1-N --single-file --log-file=A04.log
```

Expected:

-   One `track.wav` is produced.
-   Its PCM equals the A03 track PCM concatenated in track order.
-   AccurateRip still verifies individual constituent tracks.

### A05 - Open-range equivalence

``` bash
redumper-cdda /dev/sg4 -
redumper-cdda /dev/sg4 -3
redumper-cdda /dev/sg4 2-
```

Also run the command with no selection. Expected:

-   Bare `-` and an omitted selection produce the same track set.
-   `-3` begins at Track 1.
-   `2-` ends at the final audio track.

### A06 - Track 0 rejection

``` bash
redumper-cdda /dev/sg4 0
```

Expected: the command fails because Track 1 begins at LBA 0, and it leaves no
output file.

### A07 - Imported-dump parity

Using a complete redumper dump of this disc:

``` bash
redumper-cdda 2 --existing-dump=/images/audio \
  --cdparanoia-toc-file=/images/cdparanoia-toc.txt
redumper-cdda 1-N --existing-dump=/images/audio \
  --cdparanoia-toc-file=/images/cdparanoia-toc.txt
```

Expected:

-   No dump or refine command runs.
-   Split runs once.
-   Output matches A02 and A03.
-   SHA-256 hashes of the source dump and all sidecars remain unchanged.
-   Q is reported as unavailable rather than zero.

## B. Audio CD with a Track 1 pregap

Use a disc for which cdparanoia reports Track 1 beginning above LBA 0. Record
that begin value as `P`; it is the expected Track 0 sector count.

### B01 - Pregap layout

``` bash
redumper-cdda /dev/sg4 --show-layout
```

Expected: Track 1 has a positive begin value and all numbered audio-track
boundaries agree with the reference layout.

### B02 - Track 0 alone

``` bash
redumper-cdda /dev/sg4 0 --no-accuraterip --log-file=B02.log
```

Expected:

-   Output begins at LBA 0.
-   Output stops immediately before Track 1 INDEX 01.
-   The PCM payload contains exactly `P * 2352` bytes.
-   Track 0 is not submitted to AccurateRip.

### B03 - Track 1 alone

``` bash
redumper-cdda /dev/sg4 1 --log-file=B03.log
```

Expected:

-   Output starts at Track 1 INDEX 01, not at sector zero of an INDEX 00 BIN.
-   Track 0 audio is absent.
-   AccurateRip can verify Track 1 when the pressing is present in the
    database.

### B04 - Track 0 through Track 1

``` bash
redumper-cdda /dev/sg4 0-1 --log-file=B04.log
```

Expected:

-   Separate Track 0 and Track 1 WAVs are produced.
-   Track 0 matches B02 and Track 1 matches B03.
-   No samples are duplicated or omitted at the boundary.

### B05 - Open ranges exclude Track 0

``` bash
redumper-cdda /dev/sg4 -
redumper-cdda /dev/sg4 -2
```

Expected: neither command creates `track00.wav`.

### B06 - Imported pregap extraction

Repeat B02 through B04 with `--existing-dump` and the captured cdparanoia TOC.
Expected: imported outputs exactly match live extraction, particularly at the
Track 0/Track 1 boundary.

## C. Data track first, followed by audio tracks

Use a disc where Track 1 is data and Tracks 2 and later are audio.

### C01 - Layout and default full-disc selection

``` bash
redumper-cdda /dev/sg4 --show-layout
redumper-cdda /dev/sg4 - --log-file=C01.log
```

Expected:

-   The layout identifies Track 1 as data.
-   Default audio extraction omits Track 1.
-   The first output is `track02.wav`.

### C02 - Open-start audio selection

``` bash
redumper-cdda /dev/sg4 -3
```

Expected: audio Tracks 2 and 3 are selected and data Track 1 is omitted.

### C03 - Strict bounded audio rejection

``` bash
redumper-cdda /dev/sg4 1-3
```

Expected: the command fails because the explicitly bounded Track 1 is data
and `--include-data` was not supplied. No output is created.

### C04 - Data track alone

``` bash
redumper-cdda /dev/sg4 1 --include-data --log-file=C04.log
redumper-cdda /dev/sg4 1 --include-data --single-file --log-file=C04s.log
```

Expected:

-   A validated `track01.iso` is produced.
-   The ISO begins at Track 1 INDEX 01.
-   Both modes produce identical ISO content.
-   The ISO mounts successfully or passes an independent ISO9660 inspection.

### C05 - Mixed data/audio range

``` bash
redumper-cdda /dev/sg4 1-3 --include-data --log-file=C05.log
```

Expected:

-   One dump and one split run.
-   `track01.iso`, `track02.wav`, and `track03.wav` are produced.
-   The audio matches Tracks 2 and 3 extracted individually.
-   The data output matches C04.

### C06 - Invalid mixed single-file mode

``` bash
redumper-cdda /dev/sg4 1-3 --include-data --single-file
```

Expected: planning fails and no output is created.

### C07 - Imported mixed extraction

Repeat C04 and C05 with `--existing-dump` and the captured cdparanoia TOC.
Expected:

-   No dump or refine command runs.
-   ISO and WAV output matches live extraction.
-   The source dump set remains unchanged.

## D. Audio CD with a final data track

Use an enhanced CD with an audio session followed by a later data session.
Let `A` be the final audio track and `D` the data track.

### D01 - Session-aware layout

``` bash
redumper-cdda /dev/sg4 --show-layout
```

Expected:

-   Track `A` belongs to the earlier audio session.
-   Track `A` ends at that session's lead-out.
-   Track `A` does not extend to Track `D`'s start.
-   Track `D` appears in the later session.

### D02 - Final audio track alone

``` bash
redumper-cdda /dev/sg4 A --log-file=D02.log
```

Expected:

-   AccurateRip verifies the track when the pressing is known.
-   Output ends at the audio session's lead-out.
-   The lead-out, inter-session gap, and data-track INDEX 00 are absent.

### D03 - Full audio-only extraction

``` bash
redumper-cdda /dev/sg4 - --log-file=D03.log
```

Expected:

-   The data track is omitted.
-   Track `A` is PCM-identical to D02.
-   Its AccurateRip result matches D02.

### D04 - Final audio plus data track

``` bash
redumper-cdda /dev/sg4 A-D --include-data --log-file=D04.log
```

Expected:

-   One dump and one split run.
-   Track `A` is PCM-identical to D02.
-   Track `D` begins at its INDEX 01 and validates as ISO9660.
-   Including data does not alter the final audio track.
-   No base-LBA or insufficient-split-data failure occurs.

This is the highest-priority regression case.

### D05 - Full disc including data

``` bash
redumper-cdda /dev/sg4 - --include-data --log-file=D05.log
```

Expected:

-   Every selected audio track and the final ISO are produced.
-   Every audio WAV matches audio-only extraction.
-   The final audio track remains AccurateRip-verifiable.

### D06 - Imported enhanced-CD extraction

Repeat D02, D04, D05, and D07 using their corresponding captured dumps and the
captured cdparanoia TOC. Expected:

-   Live and imported WAV and ISO outputs are identical.
-   The final audio track remains AccurateRip-verifiable when data is
    included.
-   The original dump metadata is unchanged even though split temporarily
    bounds the workspace copy.

### D07 - Final data track alone

``` bash
redumper-cdda /dev/sg4 D --include-data --log-file=D07.log
redumper-cdda /dev/sg4 D --include-data --single-file --log-file=D07s.log
```

Expected:

-   Only the final data track is read and converted.
-   Separate and single-file modes produce identical ISO output.
-   The ISO begins at Track `D` INDEX 01, is directly mountable, and is
    trimmed to its declared ISO9660 volume size.
-   The ISO volume and all checked filesystem metadata remain within the
    physical length of Track `D`.

## E. Data-only CD

### E01 - Layout

``` bash
redumper-cdda /dev/sg4 --show-layout
```

Expected:

-   Every numbered track is data.
-   cdparanoia is not queried when there are no MMC audio tracks.

### E02 - Default audio extraction rejection

``` bash
redumper-cdda /dev/sg4 -
```

Expected: selection fails because the disc has no audio tracks, and no output
is created.

### E03 - Single data track

``` bash
redumper-cdda /dev/sg4 1 --include-data --log-file=E03.log
redumper-cdda /dev/sg4 1 --include-data --single-file --log-file=E03s.log
```

Expected:

-   Both commands produce valid, identical ISO content.
-   The ISO begins at INDEX 01.
-   The ISO is trimmed to the filesystem's declared volume size.

### E04 - Multiple data tracks

If the disc contains multiple data tracks:

``` bash
redumper-cdda /dev/sg4 1-N --include-data --log-file=E04.log
```

Expected:

-   A separate ISO is produced for each track.
-   Exactly one dump and one split run.
-   Every track is resolved from a dedicated split BIN.
-   Every ISO validates independently.

### E05 - Multiple data tracks in single-file mode

``` bash
redumper-cdda /dev/sg4 1-N --include-data --single-file
```

Expected: planning fails and no output is created.

### E06 - Imported data extraction

Repeat E03 and, where applicable, E04 with `--existing-dump` and the captured
cdparanoia TOC. Expected: ISO hashes
match live extraction and all source dump hashes remain unchanged.

## F. Synthetic integrity-policy regression

The offline validator uses the clean `regular-audio` capture. It changes one
sample-state byte to unresolved C2 in disposable copies; it never changes the
captured PCM or source fixtures. Redumper may replace the flagged output sample
to represent the unresolved read.

### F01 - Clean control

The original single-track and all-tracks dumps extract without an unresolved
error warning and retain their AccurateRip results.

### F02 - Synthetic single-track error

The validator fabricates C2 state in Track 2. Default mode succeeds, reports
the unresolved error, retains the output despite its expected AccurateRip
mismatch, and leaves the captured source unchanged. With `--abort-on-skip`,
Track 2 is omitted and the command exits nonzero.

### F03 - Synthetic range error

The validator fabricates C2 state in Track 2 of the all-audio range. Default
mode retains every output and reports the expected Track 2 AccurateRip
mismatch without treating it as evidence of another read error.
Strict separate mode retains every clean track and omits only Track 2; strict
single-file mode rejects the combined output. No dump or refine command runs
for these imported-dump checks, and source hashes remain unchanged.

Live acquisition/refinement sequencing, bounded refinement, and interruption
cleanup remain covered by the automated application and adapter tests; they no
longer require a repeatably damaged physical disc fixture.

## D (continued). Nonzero-offset strict AccurateRip regression

Use a clean enhanced CD with a final data track and two clean audio tracks: an
AccurateRip-verifiable Track `C` and alignment Track `N` immediately after it.
The mixed range `R-D` must include both tracks and the final data track. The
capture script derives these tracks and the full range directly from the disc
layout. The split must report a nonzero write offset when data is included.

### D08 - Clean single-track control

``` bash
redumper-cdda /dev/sg4 C --retries=100 --log-file=D08.log
```

Expected: the audio-only split forces offset zero, Track `C` has no unresolved
SCSI/C2 state, and AccurateRip verifies it.

### D09 - Mixed-range nonzero offset

``` bash
redumper-cdda /dev/sg4 R-D --include-data --retries=100 --log-file=D09.log
```

Expected: one dump and one split run, the split reports a negative nonzero
write offset, and Track `C` has the same AccurateRip result as D08 after
checksum-only alignment.

### D10 - Strict omission keeps alignment PCM

Run the offline validator after capturing the clean profile:

``` bash
./scripts/validate_physical_test_data.py \
  --profile=data-last --log-file=D10.log
```

The validator copies the mixed dump and changes one state byte in the middle
of Track `N` to redumper's unresolved C2 value before its strict-policy run.
PCM is not changed, and the source dump remains hash-identical. Expected:

-   the strict extraction exits nonzero because the synthetically flagged
    output is omitted;
-   clean Track `C` is retained and synthetically flagged Track `N` is omitted;
-   Track `N` remains available only inside the temporary workspace as the
    following alignment source for the negative checksum window;
-   only Track `C`, not omitted Track `N`, is submitted to AccurateRip; and
-   Track `C` verifies exactly as it did in D08.

### D11 - Cross-range PCM comparison

Compare Track `C` from D08 and D09 over their common frame window after using
the split-reported write offset. Every shared frame must match. Both WAVs must
still contain exactly `track.length * 2352` PCM bytes.

### D12 - Imported-dump regression

D10 uses the clean offset-control and full-range `data-last` dumps. Expected: no
dump or refine runs, each extraction splits exactly once, D10 still verifies
Track `C`, and all source hashes remain unchanged.

## Acceptance criteria

The implementation is physically validated when:

1.  Every applicable case has the expected exit status and final file set.
2.  The same audio track is PCM-identical when extracted alone, in an audio
    range, in a mixed range, or from an imported dump.
3.  The enhanced CD's final audio track remains PCM-identical and
    AccurateRip-verifiable when the data track is included.
4.  Every ISO independently validates and is exactly trimmed to its declared
    ISO9660 size.
5.  Dump, refine, split, error-policy, and LBA-range evidence in the verbose
    logs satisfies the documented extraction invariants.
