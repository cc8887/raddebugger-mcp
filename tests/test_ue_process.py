"""No real processes are opened or terminated by these cleanup contract tests."""
import ctypes
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from smoke_ue import OwnedProcess


@unittest.skipUnless(os.name == "nt", "Windows process handle contract")
class ProcessCleanupTests(unittest.TestCase):
    def process(self, waits):
        process = OwnedProcess.__new__(OwnedProcess)
        process.kernel = MagicMock()
        process.kernel.WaitForSingleObject.side_effect = waits
        process.handle = 1234
        return process

    def test_liveness_distinguishes_signaled_from_timeout(self):
        process = self.process([0x102, 0])
        self.assertTrue(process.alive())
        self.assertFalse(process.alive())

    def test_wait_failure_is_not_reported_as_process_exit(self):
        process = self.process([0xFFFFFFFF])
        with patch.object(ctypes, "get_last_error", return_value=6), self.assertRaises(OSError):
            process.alive()

    def test_already_exited_process_is_not_terminated(self):
        process = self.process([0])
        self.assertEqual(process.terminate(), "already_exited_before_cleanup")
        process.kernel.TerminateProcess.assert_not_called()

    def test_exit_race_requires_original_handle_to_be_signaled(self):
        process = self.process([0x102, 0])
        process.kernel.TerminateProcess.return_value = False
        with patch.object(ctypes, "get_last_error", return_value=5):
            self.assertEqual(process.terminate(), "already_exited_during_cleanup")

    def test_access_denied_on_live_process_is_not_swallowed(self):
        process = self.process([0x102, 0x102])
        process.kernel.TerminateProcess.return_value = False
        with patch.object(ctypes, "get_last_error", return_value=5), self.assertRaises(OSError):
            process.terminate()

    def test_successful_termination_still_waits_for_exit(self):
        process = self.process([0x102, 0])
        process.kernel.TerminateProcess.return_value = True
        self.assertEqual(process.terminate(), "terminated_owned_process")
        process.kernel.TerminateProcess.assert_called_once_with(1234, 0)
