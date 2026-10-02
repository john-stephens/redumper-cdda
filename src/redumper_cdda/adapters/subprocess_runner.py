"""Subprocess-backed implementation of the process-execution port."""

import subprocess

from ..ports.process import CommandResult


class SubprocessRunner:
    def __init__(self, popen=subprocess.Popen, stop_timeout=5):
        self._popen = popen
        self._stop_timeout = stop_timeout

    def stop(self, process):
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=self._stop_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def wait(self, process, stopper=None):
        stop = stopper or self.stop
        try:
            return process.wait()
        except BaseException:
            stop(process)
            raise

    def communicate(self, process, stopper=None):
        stop = stopper or self.stop
        try:
            return process.communicate()
        except BaseException:
            stop(process)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            raise

    def capture(
        self,
        command,
        text=True,
        merge_stderr=False,
        communicator=None,
    ):
        process = self._popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            text=text,
        )
        output, stderr = (communicator or self.communicate)(process)
        return CommandResult(
            tuple(command),
            process.returncode,
            output,
            stderr,
        )

    def run(self, command, inherit_output=False, waiter=None, communicator=None):
        process = self._popen(
            command,
            stdout=None if inherit_output else subprocess.PIPE,
            stderr=None if inherit_output else subprocess.STDOUT,
            text=not inherit_output,
        )
        if inherit_output:
            returncode = waiter(process) if waiter is not None else process.wait()
            output = None
        else:
            output, _ = (communicator or self.communicate)(process)
            returncode = process.returncode
        return CommandResult(tuple(command), returncode, output)

    def run_streaming(self, command, observer=None, stopper=None):
        process = self._popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        lines = []
        assert process.stdout is not None
        stop = stopper or self.stop
        try:
            for line in process.stdout:
                if observer is not None:
                    observer(line)
                lines.append(line)
            returncode = process.wait()
        except BaseException:
            stop(process)
            raise
        finally:
            process.stdout.close()
        return CommandResult(tuple(command), returncode, "".join(lines))
