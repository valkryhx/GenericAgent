"""Resume should reuse unchanged agents by content, not by position.

CC 319 journals each agent under a content hash (prompt + schema + model + tools
+ args + phase), so editing one line of a script invalidates only the affected
agents; the 319 doc calls the index-based approach out explicitly (§12.2.1).
GA matched ``resume_plan[call_index]`` only, and threw the rest of the plan away
on the first mismatch, so inserting one agent at the top re-ran every later
agent -- the most expensive possible outcome for the most common edit.
"""
import hashlib
import json
import unittest

from genericagent.workflow.models import WorkflowJob


class _Scheduler:
    """Same shape as AgentScheduler._cache_key, reduced to the two fields we match."""

    @staticmethod
    def _cache_key(job):
        options = job.metadata.get("options") or {}
        return {
            "promptHash": hashlib.sha256(str(job.prompt).encode("utf-8")).hexdigest(),
            "optionsHash": hashlib.sha256(json.dumps(options, sort_keys=True).encode("utf-8")).hexdigest(),
        }


def _entry(prompt, call_index, result=None, options=None):
    key = _Scheduler._cache_key(WorkflowJob(job_id="probe", prompt=prompt, metadata={"options": options or {}}))
    return {
        "callIndex": call_index,
        "prompt": prompt,
        "options": options or {},
        "promptHash": key["promptHash"],
        "optionsHash": key["optionsHash"],
        "result": result if result is not None else f"result:{prompt}",
        "sourceRunId": "wf_old",
        "sourceJobId": f"job_{call_index}",
    }


class ResumeMatchingTest(unittest.TestCase):
    def _runtime(self):
        from genericagent.workflow.runtime import WorkflowRuntime

        return WorkflowRuntime.__new__(WorkflowRuntime)

    def _match(self, plan, call_index, prompt, options=None):
        return self._runtime()._match_cached_agent(
            plan,
            call_index=call_index,
            prompt=prompt,
            options=options or {},
            scheduler=_Scheduler(),
        )

    def test_an_unchanged_prefix_still_matches(self):
        plan = [_entry("alpha", 0), _entry("beta", 1)]

        self.assertEqual("result:alpha", self._match(plan, 0, "alpha")["result"])
        self.assertEqual("result:beta", self._match(plan, 1, "beta")["result"])

    def test_inserting_an_agent_at_the_top_does_not_invalidate_the_rest(self):
        plan = [_entry("alpha", 0), _entry("beta", 1)]

        # The edited script runs one new agent first, so the old ones shift by one.
        self.assertIsNone(self._match(plan, 0, "brand new agent"))
        self.assertEqual("result:alpha", self._match(plan, 1, "alpha")["result"])
        self.assertEqual("result:beta", self._match(plan, 2, "beta")["result"])

    def test_a_changed_prompt_is_not_reused(self):
        plan = [_entry("alpha", 0)]

        self.assertIsNone(self._match(plan, 0, "alpha, but edited"))

    def test_a_changed_option_is_not_reused(self):
        plan = [_entry("alpha", 0, options={"schema": {"a": 1}})]

        self.assertIsNone(self._match(plan, 0, "alpha", options={"schema": {"a": 2}}))

    def test_matching_never_mutates_the_plan(self):
        plan = [_entry("alpha", 0), _entry("beta", 1)]

        self._match(plan, 2, "alpha")

        self.assertEqual(2, len(plan), "a lookup must not discard later entries")


if __name__ == "__main__":
    unittest.main()

class _Store:
    def __init__(self):
        self.events = []

    def append_event(self, run, event):
        self.events.append(event)
        return event

    def save_run(self, run):
        return None

    def write_workflow_progress(self, run):
        return None

    def write_agent_result(self, run, job, result):
        return f"agents/{job.job_id}/result.json"

    def read_agent_result(self, run, job):
        return None

    def copy_agent_transcript(self, *args, **kwargs):
        return None


def _scheduler(jobs):
    from genericagent.workflow.models import WorkflowRun
    from genericagent.workflow.scheduler import AgentScheduler, SchedulerConfig

    run = WorkflowRun(session_id="session_a", script="return 1;", status="running", jobs=jobs)
    return AgentScheduler(store=_Store(), run=run, config=SchedulerConfig(), manage_run_completion=False)


def _job(job_id, label, status, depends=None):
    from genericagent.workflow.models import WorkflowJob

    metadata = {"label": label}
    if depends:
        metadata["dependsOn"] = list(depends)
    return WorkflowJob(job_id=job_id, prompt=label, status=status, metadata=metadata)


class CachedJobDependencyGuardTest(unittest.TestCase):
    """Content matching must not serve a downstream result computed from old upstream output.

    GA injects upstream results as handoff metadata, not into the prompt, so the
    cache key cannot see them. Reusing a downstream job while an upstream job
    actually re-ran would answer with stale data.
    """

    def test_tick_runs_the_guard_before_starting_jobs(self):
        upstream = _job("agent_1", "A", "succeeded")
        downstream = _job("agent_2", "B", "cached", ["A"])
        downstream.metadata["result"] = {"x": 1}

        scheduler = _scheduler([upstream, downstream])
        scheduler.tick()

        self.assertIn("agent_cache_demoted", [event.event_type for event in scheduler.store.events])

    def test_cached_downstream_survives_when_upstream_is_also_cached(self):
        upstream = _job("agent_1", "A", "cached")
        downstream = _job("agent_2", "B", "cached", ["A"])
        downstream.metadata["result"] = {"x": 1}

        scheduler = _scheduler([upstream, downstream])
        scheduler._demote_stale_cached_jobs()

        self.assertEqual("cached", downstream.status)
        self.assertEqual({"x": 1}, downstream.metadata["result"])

    def test_cached_downstream_is_demoted_when_an_upstream_re_ran(self):
        upstream = _job("agent_1", "A", "succeeded")
        downstream = _job("agent_2", "B", "cached", ["A"])
        downstream.metadata["result"] = {"x": 1}
        downstream.metadata["handoff"] = {"summary": "old"}
        downstream.result_ref = "agents/agent_2/result.json"

        scheduler = _scheduler([upstream, downstream])
        scheduler._demote_stale_cached_jobs()

        self.assertEqual("queued", downstream.status)
        self.assertIsNone(downstream.result_ref)
        self.assertNotIn("result", downstream.metadata)
        self.assertNotIn("handoff", downstream.metadata)
        self.assertEqual("agent_cache_demoted", scheduler.store.events[-1].event_type)

    def test_demotion_waits_until_the_upstream_finishes(self):
        upstream = _job("agent_1", "A", "running")
        downstream = _job("agent_2", "B", "cached", ["A"])

        scheduler = _scheduler([upstream, downstream])
        scheduler._demote_stale_cached_jobs()

        self.assertEqual("cached", downstream.status)
