"""Console reporters for quiet, concise, and verbose output modes."""

import subprocess

from ..domain.outputs import OutputKind


class QuietReporter:
    verbose = False

    def publish(self, event):
        return None


class ConciseReporter:
    verbose = False

    def __init__(self, output=print):
        self._output = output

    def publish(self, event):
        values = event.values or {}
        handlers = {
            "plan": self._plan,
            "progress": self._progress,
            "progress_end": self._progress_end,
            "warning": lambda value: self._output(f"Warning: {value}"),
            "split_started": lambda _value: self._output(
                "Splitting...", end="", flush=True
            ),
            "split_finished": lambda success: self._output(
                " done" if success else " failed"
            ),
            "output_started": self._output_started,
            "output_finished": lambda success: self._output(
                " done" if success else " failed"
            ),
            "verification_started": lambda _value: self._output(
                "Checking AccurateRip...", flush=True
            ),
            "verification_report": self._verification,
            "omitted": self._omitted,
            "complete": self._complete,
            "disc_layout": self._disc_layout,
        }
        handler = handlers.get(event.name)
        if handler is not None:
            handler(values)

    def _plan(self, values):
        plan = values["plan"]
        request = values["request"]
        count = len(plan.selection.tracks)
        self._output(
            f"Ripping track{'' if count == 1 else 's'} {plan.track_label} from "
            f"sector {plan.logical_start_lba} to {plan.logical_end_lba - 1}"
        )
        if request.single_file:
            self._output(f"Output: {plan.outputs[0].output_path}")
        else:
            self._output(f"Output: {len(plan.outputs)} separate files")

    def _progress(self, values):
        self._output(
            f"\r{values['label']}: {values['percent']:3d}%",
            end="",
            flush=True,
        )

    def _progress_end(self, _values):
        self._output()

    def _output_started(self, values):
        outputs = values["outputs"]
        if len(outputs) != 1:
            label = f"Writing {len(outputs)} output files..."
        else:
            label = (
                "Writing ISO..."
                if outputs[0].plan.kind is OutputKind.DATA
                else "Writing WAV..."
            )
        self._output(label, end="", flush=True)

    def _omitted(self, item):
        errors = item.media_errors
        self._output(
            f"Skipping Track {item.plan.track.number:02d}: "
            f"SCSI={errors.scsi_samples} samples in {errors.scsi_sectors} sectors, "
            f"C2={errors.c2_samples} samples in {errors.c2_sectors} sectors"
        )

    def _complete(self, result):
        errors = result.acquisition.media_errors
        if result.omitted:
            count = len(result.omitted)
            self._output(
                f"Done with {count} track{'' if count == 1 else 's'} omitted "
                "due to unresolved SCSI/C2 errors."
            )
        else:
            self._output(f"Done. SCSI={errors.scsi}, C2={errors.c2}, Q={errors.q}")

    def _disc_layout(self, layout):
        self._output("\nDisc track layout\n-----------------")
        self._output(
            f"{'Track':>5}  {'Type':<5}  {'Length':>10}  "
            f"{'Begin':>10}  {'End':>10}"
        )
        for track in layout.tracks:
            self._output(
                f"{track.number:>5}  {track.kind.value:<5}  "
                f"{track.length_sectors:>10}  {track.begin_lba:>10}  "
                f"{track.end_lba:>10}"
            )

    def _verification(self, report):
        self._output("\nAccurateRip verification\n========================")
        self._output(f"Disc ID: {report.disc_id}")
        verified = 0
        for result in report.results:
            track = f"Track {result['track']:02d}"
            if result["status"] == "verified":
                verified += 1
                self._output(
                    f"{track}: verified ({result['version']} "
                    f"{result['checksum']:08x}, confidence {result['confidence']})"
                )
            elif result["status"] == "not-present":
                self._output(
                    f"{track}: not present in the database "
                    f"(ARv1 {result['arv1']:08x}, ARv2 {result['arv2']:08x})"
                )
            else:
                self._output(
                    f"{track}: no match (ARv1 {result['arv1']:08x}, "
                    f"ARv2 {result['arv2']:08x})"
                )
        total = len(report.results)
        self._output(
            f"Verified: {verified}/{total} selected audio "
            f"track{'' if total == 1 else 's'}"
        )


class VerboseReporter:
    verbose = True

    def __init__(self, output=print):
        self._output = output

    def publish(self, event):
        values = event.values
        handlers = {
            "workspace_created": lambda path: self._output(
                f"Temporary workspace: {path}"
            ),
            "workspace_removed": lambda path: self._output(
                f"Temporary workspace removed: {path}"
            ),
            "layout_read": lambda label: self._output(label),
            "command_started": self._command,
            "tool_output": lambda line: self._output(line, end=""),
            "plan": self._plan,
            "acquisition_started": lambda _value: self._heading(
                "Initial partial dump"
            ),
            "refinement_started": self._refinement,
            "media_errors": self._media_errors,
            "split_started": lambda _value: self._heading(
                "Splitting partial dump"
            ),
            "omitted": ConciseReporter(self._output)._omitted,
            "output_detail": self._output_detail,
            "verification_report": self._verification,
            "complete": self._complete,
            "disc_layout": ConciseReporter(self._output)._disc_layout,
        }
        handler = handlers.get(event.name)
        if handler is not None:
            handler(values)

    def _heading(self, title):
        self._output(f"\n{title}\n{'=' * len(title)}")

    def _command(self, command):
        quoted = " ".join(subprocess.list2cmdline([str(arg)]) for arg in command)
        self._output(f"\n+ {quoted}\n")

    def _plan(self, values):
        for track in values["plan"].selection.tracks:
            self._output(
                "\nSelected track\n--------------\n"
                f"Track:               {track.number:02d}\n"
                f"Type:                {track.kind.value}\n"
                f"Begin LBA:           {track.begin_lba}\n"
                f"Length:              {track.length_sectors:,} sectors\n"
                f"Length (MSF):        {track.length_msf}\n"
                f"Logical end LBA:     {track.end_lba} [exclusive]"
            )

    def _refinement(self, values):
        self._heading(
            f"Refine pass {values['pass_number']}/{values['maximum']}"
        )
        sector_range = values["range"]
        self._output(
            f"Refining only LBA {sector_range.start_lba}.."
            f"{sector_range.end_lba} [end exclusive]"
        )

    def _media_errors(self, values):
        errors = values["errors"]
        self._output(
            "\nData integrity\n==============\n\n"
            f"SCSI: {errors.scsi}\nC2:   {errors.c2}\nQ:    {errors.q} "
            "(reported, not used as audio-data failure criterion)"
        )

    def _verification(self, report):
        concise = ConciseReporter(self._output)
        concise._verification(report)
        for result in report.results:
            if result["status"] == "verified":
                self._output(f"  ARver response: {result['response']}")

    def _output_detail(self, resolved):
        output = resolved.plan
        label = "ISO" if output.kind is OutputKind.DATA else "WAV"
        track = (
            f"Track {output.track.number:02d}"
            if output.track is not None
            else "Combined output"
        )
        self._output(
            f"{track} BIN offset: {resolved.pregap_skipped or 0:,} sectors\n"
            f"Temporary CUE:      {resolved.cue_path}\n"
            f"{label}:                {output.output_path}"
        )

    def _complete(self, result):
        plan = result.plan
        errors = result.acquisition.media_errors
        self._heading("Complete")
        self._output(
            f"Tracks:             {plan.track_label}\n"
            f"Logical LBA range:  {plan.logical_start_lba}..{plan.logical_end_lba}\n"
            f"Physical read range: {plan.dump_start_lba}..{plan.dump_end_lba}\n"
            f"Selected sectors:   {plan.expected_sectors:,}\n"
            f"Refine passes used: {result.acquisition.refine_passes_used}\n"
            f"Final SCSI errors:  {errors.scsi}\n"
            f"Final C2 errors:    {errors.c2}\n"
            f"Final Q errors:     {errors.q}\n"
            f"Integrity:          {'WARNING' if errors.has_data_errors else 'PASS'}"
        )


def reporter_for(verbose=False, quiet=False, output=print):
    if quiet:
        return QuietReporter()
    if verbose:
        return VerboseReporter(output)
    return ConciseReporter(output)
