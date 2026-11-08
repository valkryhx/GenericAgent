"""A child that finished its work must not be announced as a failure.

Real incident, from a live session: three subagents ran in parallel. `probe_mid`
completed everything it was asked to do -- its output file contains `MID_ONE`, `MID_TWO`
and `MID_DONE`, and its state says `completion_status: completed` -- but the UI announced
it in red as failed. `probe_fast` and `probe_slow` were announced correctly.

The difference was shutdown timing, not work:

  ``close_agent`` waits ``grace_s`` for the child to exit and then escalates to
  ``terminate_process()``. A child that takes slightly longer to exit (probe_mid made two
  tool calls instead of one) gets ``process_status = "killed"``. And
  ``_project_status`` checks ``process_status == "killed"`` *before*
  ``turn_status == "completed"``, so finished work is reported as a kill.

How a process was reclaimed is not a statement about whether its work succeeded. The
projection must answer two separate questions and the record has to carry both, otherwise
the UI cannot tell the difference and has to guess -- which is what it was doing.

These tests pin the record shape the UI needs: the work status must be present and
readable independently of the process status.
"""

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _state(**overrides):
    from genericagent.agents.manager import AgentState

    values = dict(
        task_name="probe_mid",
        agent_path="/root/probe_mid",
        pid=4242,
        task_dir="D:/tmp/probe_mid",
        turn_status="completed",
        process_status="shutdown",
        round=0,
        output_path=None,
        final_output_path=None,
        completion_status="completed",
        result_status="available",
    )
    values.update(overrides)
    return AgentState(**values)


class WorkStatusReachesTheRecordTest(unittest.TestCase):
    def _adapter(self):
        from genericagent.core.control.process import ProcessSubagentAdapter

        return ProcessSubagentAdapter(manager=None)

    def test_a_killed_process_with_completed_work_still_reports_completed_work(self):
        """The exact shape probe_mid had, and the bug being fixed."""
        adapter = self._adapter()

        record = adapter._record_from_state(
            _state(process_status="killed", completion_status="completed")
        )

        meta = record.metadata
        self.assertEqual(
            "completed",
            meta.get("completionStatus"),
            "the record does not carry the work status, so the UI has to guess from "
            "process_status and will call finished work a failure",
        )

    def test_a_genuine_failure_still_reports_a_failure(self):
        adapter = self._adapter()

        record = adapter._record_from_state(
            _state(turn_status="errored", completion_status="failed")
        )

        self.assertEqual("failed", record.metadata.get("completionStatus"))

    def test_the_projection_still_marks_a_kill_for_an_unfinished_child(self):
        """The status projection must not be neutered by this fix."""
        from genericagent.core.control.process import ProcessSubagentAdapter

        # No completion_status: the work never got a verdict, so `killed` stands.
        state = _state(turn_status="running", process_status="killed", completion_status=None)

        self.assertEqual("killed", ProcessSubagentAdapter._project_status(state))

    def test_a_completed_child_is_projected_as_succeeded_even_after_a_kill(self):
        """Work status wins over the reclamation method."""
        from genericagent.core.control.process import ProcessSubagentAdapter

        state = _state(turn_status="completed", process_status="killed", completion_status="completed")

        self.assertEqual(
            "succeeded",
            ProcessSubagentAdapter._project_status(state),
            "a completed child is still projected as killed, so every consumer reads it "
            "as a failure regardless of the metadata fix",
        )

    def test_a_killed_child_with_errored_work_is_projected_as_failed(self):
        from genericagent.core.control.process import ProcessSubagentAdapter

        state = _state(turn_status="errored", process_status="killed", completion_status="failed")

        self.assertEqual("failed", ProcessSubagentAdapter._project_status(state))


if __name__ == "__main__":
    unittest.main()
