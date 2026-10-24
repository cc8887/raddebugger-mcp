"""Mock-only checks: no backend, target, socket or real process handle is opened."""
import ctypes
import os
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from regression_detach import FixtureProcess, exit_details, require_normal_exit, wait_for_detached_counter_progress


class ExitAcceptanceTests(unittest.TestCase):
    def test_only_exit_zero_is_normal_success(self):
        self.assertEqual(require_normal_exit(0), {"unsigned": 0, "hex": "0x00000000", "signed": 0})
        for code in (1, 98, 259, 0x80000003, -2147483645):
            with self.subTest(code=code), self.assertRaises(AssertionError):
                require_normal_exit(code)

    def test_breakpoint_exit_has_exact_unsigned_and_signed_evidence(self):
        self.assertEqual(exit_details(0x80000003), {
            "unsigned": 2147483651, "hex": "0x80000003", "signed": -2147483645,
        })


class CounterProgressTests(unittest.TestCase):
    def process(self):
        process = MagicMock()
        process.alive.return_value = True
        process.debugging.return_value = False
        return process

    def test_growth_before_close_cannot_count_as_post_detach_progress(self):
        process = self.process()
        # A pre-close sample could have been 7; the process now stays at 100.
        # The helper must take its own post-close baseline and reject this stop.
        process.read.return_value = struct.pack("<i", 100)
        with patch("regression_detach.time.monotonic", side_effect=[0, .01, .2]), \
             patch("regression_detach.time.sleep"), self.assertRaisesRegex(AssertionError, "post-detach"):
            wait_for_detached_counter_progress(process, 0x1234, .1)
        self.assertEqual(process.read.call_count, 2)

    def test_requires_growth_between_two_samples_after_close(self):
        process = self.process()
        process.read.side_effect = [struct.pack("<i", 100), struct.pack("<i", 101)]
        with patch("regression_detach.time.monotonic", side_effect=[0, .01]), \
             patch("regression_detach.time.sleep"):
            result = wait_for_detached_counter_progress(process, 0x1234, .1)
        self.assertEqual(result, {"counter_at_detach": 100, "counter_after_detach": 101})
        self.assertEqual(process.read.call_args_list[0].args, (0x1234, 4))
        self.assertEqual(process.read.call_count, 2)
        self.assertEqual(process.debugging.call_count, 2)


@unittest.skipUnless(os.name == "nt", "Windows original process handle contract")
class OriginalHandleTests(unittest.TestCase):
    def process(self, waits):
        process = FixtureProcess.__new__(FixtureProcess)
        process.kernel = MagicMock()
        process.kernel.WaitForSingleObject.side_effect = waits
        process.handle = 1234
        return process

    def test_wait_timeout_is_not_accepted_as_normal_exit(self):
        process = self.process([0x102])
        with self.assertRaises(TimeoutError):
            process.wait_for_exit(.1)
        process.kernel.GetExitCodeProcess.assert_not_called()

    def test_failed_wait_is_not_accepted_as_exited(self):
        process = self.process([0xFFFFFFFF])
        with patch.object(ctypes, "get_last_error", return_value=6), self.assertRaises(OSError):
            process.wait_for_exit(.1)
        process.kernel.GetExitCodeProcess.assert_not_called()

    def test_exit_code_is_read_only_after_original_handle_is_signaled(self):
        process = self.process([0])

        def exit_code(handle, output):
            self.assertEqual(handle, 1234)
            output._obj.value = 0x80000003
            return True

        process.kernel.GetExitCodeProcess.side_effect = exit_code
        self.assertEqual(process.wait_for_exit(.1), 0x80000003)
        with self.assertRaises(AssertionError):
            require_normal_exit(0x80000003)

    def test_access_denied_cleanup_requires_signaled_original_handle(self):
        process = self.process([0x102, 0x102])
        process.kernel.TerminateProcess.return_value = False
        with patch.object(ctypes, "get_last_error", return_value=5), self.assertRaises(OSError):
            process.terminate_if_alive()
        process.kernel.TerminateProcess.assert_called_once_with(1234, 0)

    def test_cleanup_accepts_exit_race_only_on_original_handle(self):
        process = self.process([0x102, 0])
        process.kernel.TerminateProcess.return_value = False
        with patch.object(ctypes, "get_last_error", return_value=5):
            self.assertEqual(process.terminate_if_alive(), "exited_during_cleanup")


if __name__ == "__main__":
    unittest.main()
