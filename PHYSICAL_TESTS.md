# Physical-disc validation plan

This plan validates `redumper-cdda` against six representative physical CD
conditions and layouts:

1. regular audio CD;
2. audio CD with a Track 1 pregap containing hidden Track 0 audio;
3. audio CD with a data track first;
4. audio CD with a data track last;
5. data-only CD; and
6. audio CD with a known-clean track and a track with repeatable SCSI or C2
   errors.

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
./scripts/capture_physical_test_data.py audio-errors /dev/sg4 \
  --clean-track=C --error-track=N --error-range=N-M
```

The captures live under the Git-ignored `test_data/` directory. Every scenario
contains a redumper dump whose physical range matches the corresponding live
test, including endpoint padding. See its profile-level `manifest.json` for
the mapped case names, extensionless `--existing-dump` prefix, exact ranges,
commands, statuses, and drive identity. Use the profile-level
`cdparanoia-toc.txt` for `--cdparanoia-toc-file`. Packaging-only variants reuse
the same captured dump because they do not change acquisition.

For `audio-errors`, replace `C` with a known-clean audio track, `N` with a
known-damaged audio track, and `N-M` with a contiguous all-audio range that
contains both. The profile uses zero retries by default and rejects captures
unless the control is clean and both damaged scenarios retain SCSI or C2
errors.

After all profiles have been captured, run every media-free case with:

``` bash
./scripts/validate_physical_test_data.py
```

The validator reads only the capture manifests and dump files, creates outputs
in temporary storage, and rechecks every source hash afterward. It requires
redumper for splitting but does not require or access an optical device.

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

## F. Integrity and refinement

Run these cases using at least one imperfect audio disc. If available, also
run them with the enhanced CD.

### F01 - Clean disc skips refinement

Expected phase sequence:

``` text
dump -> inspect 0/0 -> split
```

No refine command should appear in the log.

### F02 - Recoverable errors

Use a disc and drive combination that initially reports SCSI or C2 errors:

``` bash
redumper-cdda /dev/sg4 N --refine-passes=3 --log-file=F02.log
```

Expected:

-   Refinement begins only after nonzero SCSI/C2 is detected.
-   Every refine uses the exact initial dump LBA bounds.
-   Refinement stops immediately when SCSI and C2 both reach zero.
-   Per-track progress displays that track's current error counts.

### F03 - Unresolved errors with default policy

``` bash
redumper-cdda /dev/sg4 N --refine-passes=1 --log-file=F03.log
```

Expected: output is created, and the final SCSI/C2 counts and an unresolved
error warning are shown.

### F04 - Per-track strict policy

Choose a range containing clean and damaged tracks:

``` bash
redumper-cdda /dev/sg4 N-M --abort-on-skip --log-file=F04.log
```

Expected:

-   Clean track files are retained.
-   Only affected track files are omitted.
-   The command exits nonzero.

### F05 - Single-file strict policy

``` bash
redumper-cdda /dev/sg4 N-M --single-file --abort-on-skip \
  --log-file=F05.log
```

Expected: any unresolved SCSI or C2 state rejects the complete combined
output.

### F06 - Unlimited-refinement interruption

``` bash
redumper-cdda /dev/sg4 N --refine-forever --log-file=F06.log
```

Interrupt the command with Ctrl-C during refinement. Expected:

-   The active redumper process stops.
-   The temporary workspace is removed.
-   No incomplete output remains.

### F07 - Imported dump with unresolved errors

Import a dump known to retain SCSI or C2 errors. Expected:

-   No refinement is attempted.
-   Default mode writes output with a warning.
-   `--abort-on-skip` applies the same per-track or single-file policy as live
    acquisition.
-   Q is reported as unavailable and does not alter the SCSI/C2 policy.

### F08 - Captured damaged track with default policy

Use the `f02-f03-f06-f07-damaged-track` fixture from the `audio-errors`
capture:

``` bash
redumper-cdda N \
  --existing-dump=test_data/audio-errors/f02-f03-f06-f07-damaged-track/trackNN \
  --cdparanoia-toc-file=test_data/audio-errors/cdparanoia-toc.txt \
  --no-accuraterip --log-file=F08.log
```

Replace `N` and `trackNN` with the captured damaged track number. Expected:

-   no device is required and no dump or refine command runs;
-   the output is created with an unresolved SCSI/C2 warning; and
-   source hashes still match `SHA256SUMS` after extraction.

### F09 - Captured damaged range with per-track strict policy

Use the `f04-f05-damaged-range` fixture:

``` bash
redumper-cdda N-M --abort-on-skip \
  --existing-dump=test_data/audio-errors/f04-f05-damaged-range/tracksNN-MM \
  --cdparanoia-toc-file=test_data/audio-errors/cdparanoia-toc.txt \
  --no-accuraterip --log-file=F09.log
```

Expected: clean track files are retained, affected files are omitted, and the
command exits nonzero.

### F10 - Captured damaged range with single-file strict policy

Repeat F09 with `--single-file`. Expected: the unresolved SCSI/C2 state rejects
the combined output and no final WAV is created.

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
