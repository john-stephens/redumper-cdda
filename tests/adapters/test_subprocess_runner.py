import io
import subprocess
import unittest
from unittest import mock

from redumper_cdda.adapters.subprocess_runner import SubprocessRunner


class SubprocessRunnerTests(unittest.TestCase):
    def test_stop_all_process_states(self):
        runner = SubprocessRunner(stop_timeout=2)
        complete = mock.Mock()
        complete.poll.return_value = 0
        runner.stop(complete)
        complete.terminate.assert_not_called()

        gone = mock.Mock()
        gone.poll.return_value = None
        gone.terminate.side_effect = ProcessLookupError
        runner.stop(gone)
        gone.wait.assert_not_called()

        normal = mock.Mock()
        normal.poll.return_value = None
        runner.stop(normal)
        normal.wait.assert_called_once_with(timeout=2)

        stubborn = mock.Mock()
        stubborn.poll.return_value = None
        stubborn.wait.side_effect = (subprocess.TimeoutExpired("x", 2), 0)
        runner.stop(stubborn)
        stubborn.kill.assert_called_once_with()

    def test_wait_and_communicate_stop_on_base_exception(self):
        stopper = mock.Mock()
        process = mock.Mock()
        process.wait.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            SubprocessRunner().wait(process, stopper)
        stopper.assert_called_once_with(process)

        process = mock.Mock()
        process.communicate.side_effect = RuntimeError("broken")
        process.stdout = mock.Mock()
        process.stderr = mock.Mock()
        stopper.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "broken"):
            SubprocessRunner().communicate(process, stopper)
        stopper.assert_called_once_with(process)
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()

        process = mock.Mock(stdout=None, stderr=None)
        process.communicate.side_effect = RuntimeError("broken")
        with self.assertRaises(RuntimeError):
            SubprocessRunner().communicate(process, stopper)

    def test_capture_with_separate_and_merged_stderr(self):
        first = mock.Mock(returncode=0)
        first.communicate.return_value = ("output", "error")
        second = mock.Mock(returncode=1)
        communicator = mock.Mock(return_value=(b"merged", None))
        popen = mock.Mock(side_effect=(first, second))
        runner = SubprocessRunner(popen=popen)

        separate = runner.capture(["one"])
        merged = runner.capture(
            ["two"],
            text=False,
            merge_stderr=True,
            communicator=communicator,
        )

        self.assertEqual(separate.output, "output")
        self.assertEqual(separate.stderr, "error")
        self.assertEqual(merged.command, ("two",))
        self.assertEqual(merged.returncode, 1)
        self.assertEqual(merged.output, b"merged")
        popen.assert_has_calls(
            [
                mock.call(
                    ["one"],
                    stdout=__import__("subprocess").PIPE,
                    stderr=__import__("subprocess").PIPE,
                    text=True,
                ),
                mock.call(
                    ["two"],
                    stdout=__import__("subprocess").PIPE,
                    stderr=__import__("subprocess").STDOUT,
                    text=False,
                ),
            ]
        )
        communicator.assert_called_once_with(second)

    def test_streaming_without_observer(self):
        process = mock.Mock(returncode=0)
        process.stdout = io.StringIO("one\ntwo\n")
        process.wait.return_value = 0
        runner = SubprocessRunner(popen=mock.Mock(return_value=process))

        result = runner.run_streaming(["tool"])

        self.assertEqual(result.command, ("tool",))
        self.assertEqual(result.output, "one\ntwo\n")
        self.assertEqual(result.returncode, 0)

    def test_streaming_with_observer(self):
        process = mock.Mock(returncode=0)
        process.stdout = io.StringIO("one\n")
        process.wait.return_value = 0
        observer = mock.Mock()
        SubprocessRunner(popen=mock.Mock(return_value=process)).run_streaming(
            ["tool"], observer=observer
        )
        observer.assert_called_once_with("one\n")

    def test_run_inherited_and_captured(self):
        inherited = mock.Mock()
        waiter = mock.Mock(return_value=3)
        captured = mock.Mock(returncode=0)
        communicator = mock.Mock(return_value=("output", None))
        runner = SubprocessRunner(popen=mock.Mock(side_effect=(inherited, captured)))
        first = runner.run(["one"], inherit_output=True, waiter=waiter)
        second = runner.run(["two"], communicator=communicator)
        self.assertEqual((first.returncode, first.output), (3, None))
        self.assertEqual((second.returncode, second.output), (0, "output"))
        waiter.assert_called_once_with(inherited)
        communicator.assert_called_once_with(captured)

    def test_streaming_stops_and_closes_on_exception(self):
        class Broken:
            def __iter__(self):
                raise RuntimeError("stream")

            def close(self):
                self.closed = True

        stream = Broken()
        process = mock.Mock(stdout=stream)
        stopper = mock.Mock()
        runner = SubprocessRunner(popen=mock.Mock(return_value=process))
        with self.assertRaisesRegex(RuntimeError, "stream"):
            runner.run_streaming(["tool"], stopper=stopper)
        stopper.assert_called_once_with(process)
        self.assertTrue(stream.closed)
