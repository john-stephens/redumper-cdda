"""Bounded initial acquisition and conditional refinement service."""

from ..domain.errors import DumpError, IntegrityStatusError, RefineError
from ..domain.events import LifecycleEvent
from ..domain.extraction import AcquisitionResult
from ..ports.acquisition import IntegrityParser, RedumperPort


class AcquisitionService:
    def __init__(
        self,
        redumper: RedumperPort,
        integrity_parser: IntegrityParser,
        reporter,
    ):
        self._redumper = redumper
        self._integrity_parser = integrity_parser
        self._reporter = reporter

    def acquire(self, plan, request):
        self._reporter.publish(LifecycleEvent("acquisition_started"))
        result = self._redumper.dump(plan)
        if result.returncode != 0:
            raise DumpError(
                "redumper dump failed.\nNo output file was created."
            )
        errors = self._parse_errors(result.output, after_refine=False)
        self._reporter.publish(
            LifecycleEvent("media_errors", {"errors": errors, "pass_number": 0})
        )

        passes = 0
        while errors.has_data_errors and passes < request.refine_passes:
            passes += 1
            self._reporter.publish(
                LifecycleEvent(
                    "refinement_started",
                    {
                        "pass_number": passes,
                        "maximum": request.refine_passes,
                        "range": plan.physical_range,
                    },
                )
            )
            result = self._redumper.refine(plan)
            if result.returncode != 0:
                raise RefineError(
                    "redumper refine failed.\nNo output file was created."
                )
            errors = self._parse_errors(result.output, after_refine=True)
            self._reporter.publish(
                LifecycleEvent(
                    "media_errors", {"errors": errors, "pass_number": passes}
                )
            )

        if errors.has_data_errors and request.abort_on_skip and request.single_file:
            plural = "" if passes == 1 else "es"
            raise IntegrityStatusError(
                "SCSI/C2 errors remain after "
                f"{passes} refine pass{plural}.\n"
                f"Remaining SCSI errors: {errors.scsi}\n"
                f"Remaining C2 errors:   {errors.c2}\n"
                "No output file was created because --abort-on-skip was "
                "specified with --single-file."
            )
        if errors.has_data_errors and not request.abort_on_skip:
            self._reporter.publish(
                LifecycleEvent(
                    "warning",
                    "writing output with unresolved "
                    f"SCSI={errors.scsi}, C2={errors.c2}",
                )
            )
        return AcquisitionResult(errors, passes)

    def _parse_errors(self, output, after_refine):
        try:
            return self._integrity_parser.media_errors(output)
        except IntegrityStatusError as exc:
            if after_refine:
                raise IntegrityStatusError(
                    "Could not determine redumper SCSI/C2 error status after "
                    "refine.\nNo output file was created."
                ) from exc
            raise IntegrityStatusError(
                "Could not determine redumper SCSI/C2 error status from dump "
                "output.\nRefusing to create output because dump integrity "
                "cannot be verified."
            ) from exc
