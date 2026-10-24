"""Source-contract checks for the shipped native patch, not Windows integration.

These run from a fresh source checkout without .build, a compiler, RAD, sockets,
or a target process. The real step/detach/exit regression is regression_detach.py.
"""
from dataclasses import dataclass
from pathlib import Path
import re
import unittest


PATCH = Path(__file__).resolve().parents[1] / "native" / "upstream.patch"
DEMON = "src/win32/demon/win32_demon.c"


@dataclass
class Hunk:
    path: str
    context: str
    lines: list[str]
    additions: list[str]


def read_hunks() -> list[Hunk]:
    result: list[Hunk] = []
    path = ""
    current = None
    for line in PATCH.read_text(encoding="utf-8").splitlines():
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@ "):
            context = line.split("@@", 2)[2].strip()
            current = Hunk(path, context, [], [])
            result.append(current)
        elif current is not None and line[:1] in (" ", "+"):
            current.lines.append(line[1:])
            if line.startswith("+"):
                current.additions.append(line[1:])
    return result


def braced_body(source: str, prefix: str) -> tuple[str, int]:
    """Return a complete small guard body and its end, including nested guards."""
    start = source.index("{", source.index(prefix))
    depth = 1
    cursor = start + 1
    while depth and cursor < len(source):
        if source[cursor] == "{":
            depth += 1
        elif source[cursor] == "}":
            depth -= 1
        cursor += 1
    if depth:
        raise AssertionError("Native guard is incomplete in the published patch")
    return source[start + 1:cursor - 1], cursor


class NativeDetachContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hunks = read_hunks()
        cls.detach = "\n".join(
            "\n".join(h.lines) for h in cls.hunks
            if h.path == DEMON and h.context.startswith("dmn_ctrl_detach(")
        )
        cls.run_source = "\n".join(
            "\n".join(h.lines) for h in cls.hunks
            if h.path == DEMON and h.context.startswith("dmn_ctrl_run(")
        )

    def test_disposition_belongs_to_shared_pending_event_state(self):
        header = "\n".join(
            "\n".join(h.lines) for h in self.hunks
            if h.path == "src/win32/demon/win32_demon.h"
        )
        self.assertIn("U32 detach_resume_code;", header)
        self.assertIn("U32 resume_pid;", header)
        self.assertIn("U32 resume_tid;", header)

    def test_only_matching_pending_pid_is_completed(self):
        guard = "if(w32_dmn_shared->resume_needed && w32_dmn_shared->resume_pid == pid)"
        body, _ = braced_body(self.detach, guard)
        self.assertIn("ContinueDebugEvent(w32_dmn_shared->resume_pid", body)
        self.assertIn("w32_dmn_shared->resume_tid", body)
        self.assertIn("w32_dmn_shared->detach_resume_code", body)
        self.assertEqual(self.detach.count("ContinueDebugEvent("), 1)
        # Do not drain/consume a debug event belonging to another process.
        self.assertNotIn("WaitForDebugEvent", self.detach)

    def test_continue_failure_preserves_pending_state_and_aborts(self):
        failure, end = braced_body(self.detach, "if(!ContinueDebugEvent(")
        self.assertIn("goto detach_done;", failure)
        self.assertNotRegex(failure, r"w32_dmn_shared->\w+\s*=")
        self.assertNotIn("ResumeThread(", failure)
        self.assertNotIn("DebugActiveProcessStop(", failure)
        before_success = self.detach[:end]
        for field in ("resume_needed", "resume_pid", "resume_tid"):
            self.assertNotIn(f"w32_dmn_shared->{field} = 0;", before_success)

    def test_success_clears_pending_state_inside_matching_pid_guard(self):
        body, _ = braced_body(
            self.detach,
            "if(w32_dmn_shared->resume_needed && w32_dmn_shared->resume_pid == pid)",
        )
        _, continued = braced_body(body, "if(!ContinueDebugEvent(")
        success = body[continued:]
        for field in ("resume_needed", "resume_pid", "resume_tid", "exception_not_handled"):
            self.assertIn(f"w32_dmn_shared->{field} = 0;", success)
        self.assertIn("w32_dmn_shared->detach_resume_code = DBG_CONTINUE;", success)

    def test_stop_failure_is_not_success_and_does_not_resume_threads(self):
        failure, end = braced_body(self.detach, "if(!DebugActiveProcessStop(pid))")
        self.assertIn("goto detach_done;", failure)
        self.assertNotIn("result = 1;", failure)
        self.assertNotIn("ResumeThread(", failure)
        self.assertLess(self.detach.index("ContinueDebugEvent("), self.detach.index("DebugActiveProcessStop("))
        self.assertLess(end, self.detach.index("result = 1;"))
        self.assertLess(self.detach.index("result = 1;"), self.detach.index("ResumeThread(n->handle)"))

    def test_independent_noninheritable_handles_are_retained_before_continue(self):
        self.assertRegex(
            self.detach,
            r"DuplicateHandle\(GetCurrentProcess\(\), child->handle, GetCurrentProcess\(\),\s*"
            r"&handle, 0, FALSE, DUPLICATE_SAME_ACCESS\)",
        )
        self.assertLess(self.detach.index("DuplicateHandle("), self.detach.index("ContinueDebugEvent("))
        failure, _ = braced_body(self.detach, "if(!DuplicateHandle(")
        self.assertIn("goto detach_done;", failure)
        self.assertNotRegex(failure, r"w32_dmn_shared->\w+\s*=")
        self.assertNotIn("ContinueDebugEvent(", failure)
        self.assertNotIn("DebugActiveProcessStop(", failure)
        self.assertNotIn("ResumeThread(", failure)

    def test_resume_uses_duplicate_once_and_every_path_releases_handles(self):
        self.assertNotIn("ResumeThread(child->handle)", self.detach)
        self.assertEqual(self.detach.count("ResumeThread(n->handle)"), 1)
        cleanup = self.detach[self.detach.index("detach_done:;"):]
        self.assertIn("for(ResumeHandle *n = first_resume_handle; n != 0; n = n->next)", cleanup)
        self.assertIn("CloseHandle(n->handle);", cleanup)
        self.assertLess(cleanup.index("CloseHandle(n->handle);"), cleanup.index("scratch_end(scratch);"))
        # There is no early return after any handle might have been duplicated.
        self.assertNotIn(
            "return ", self.detach[self.detach.index("DuplicateHandle("):self.detach.index("detach_done:;")],
        )

    def test_real_exception_default_is_independent_of_first_chance(self):
        self.assertRegex(
            self.run_source,
            r"detach_resume_code\s*=\s*\(evt\.dwDebugEventCode == EXCEPTION_DEBUG_EVENT\s*"
            r"\? DBG_EXCEPTION_NOT_HANDLED : DBG_CONTINUE\)",
        )
        assignments = [line for h in self.hunks for line in h.additions if "detach_resume_code =" in line]
        self.assertTrue(assignments)
        self.assertTrue(all("dwFirstChance" not in line for line in assignments))
        self.assertNotIn("ignore_previous_exception", self.detach)
        self.assertNotRegex(self.detach, r"exception_not_handled\s*\?")

    def test_breakpoint_override_requires_debugger_classification(self):
        self.assertRegex(
            self.run_source,
            r"if\(exception->ExceptionCode == W32_DMN_EXCEPTION_BREAKPOINT &&\s*"
            r"\(first_bp \|\| hit_user_trap \|\| hit_previous_trap\)\)",
        )
        # Explicit application INT3 is not a matched or previous debugger trap.
        self.assertIn("!hit_user_trap && !hit_explicit_trap", self.run_source)

    def test_single_step_override_needs_requested_thread_or_enabled_hardware_hit(self):
        self.assertRegex(
            self.run_source,
            r"if\(\(hit_flagged_trap && trap != 0\) \|\|\s*"
            r"dmn_handle_match\(ctrls->single_step_thread, w32_dmn_handle_from_entity\(thread\)\)\)",
        )
        self.assertIn("B32 hit_flagged_trap = 0;", self.run_source)
        for slot in range(4):
            self.assertRegex(
                self.run_source,
                rf"regs\.dr7 & \(1ull<<{slot * 2}\) && regs\.dr6 & \(1ull<<{slot}\)\)"
                rf"\s*\{{ hit_flagged_trap = 1; flagged_trap_idx = {slot};",
            )

    def test_metadata_override_excludes_fault_and_trap_event_kinds(self):
        match = re.search(r"if\(e->kind != DMN_EventKind_Exception.*?\)\s*\{", self.run_source, re.S)
        self.assertIsNotNone(match)
        for kind in ("Exception", "Trap", "Breakpoint", "SingleStep"):
            self.assertIn(f"e->kind != DMN_EventKind_{kind}", match.group())

    def test_controller_reports_error_only_in_detach_function(self):
        changes = [
            h for h in self.hunks
            if h.path == "src/dbg_engine/dbg_engine_ctrl.c"
            and any("detach_worked ?" in line for line in h.additions)
        ]
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0].context.startswith("d_ctrl_thread__detach("))
        self.assertIn(
            "event->cause      = detach_worked ? D_EventCause_Finished : D_EventCause_Error;",
            "\n".join(changes[0].additions),
        )


if __name__ == "__main__":
    unittest.main()
