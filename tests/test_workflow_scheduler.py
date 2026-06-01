import tempfile
import unittest

from workflow_models import WorkflowRun
from workflow_scheduler import AgentScheduler, FakeChildAgentRunner, SchedulerConfig
from workflow_store import WorkflowStore


class WorkflowSchedulerTest(unittest.TestCase):
    def make_scheduler(self, *, max_concurrent=4, max_total=1000, runner=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = WorkflowStore(root=tmp.name)
        run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script="spawn agents", status="running"))
        scheduler = AgentScheduler(
            store=store,
            run=run,
            runner=runner or FakeChildAgentRunner(),
            config=SchedulerConfig(max_concurrent=max_concurrent, max_total=max_total),
        )
        return scheduler, store, run

    def event_types(self, store):
        return [event.event_type for event in store.replay_events("wf_test")]

    def test_scheduler_caps_max_concurrent_at_16(self):
        with self.assertRaises(ValueError):
            SchedulerConfig(max_concurrent=17)

    def test_registers_job_with_cache_key_permission_fields_and_journal_event(self):
        scheduler, store, run = self.make_scheduler()

        job = scheduler.register_agent(prompt="inspect repo", label="Scout", options={"effort": "low"})

        self.assertEqual("queued", job.status)
        self.assertEqual(0, job.metadata["callIndex"])
        cache_key = job.metadata["cacheKey"]
        self.assertEqual("inherit-current-permissions", cache_key["permissionProfile"])
        self.assertEqual("inherit-current-v1", cache_key["permissionPolicyVersion"])
        self.assertIn("scriptHash", cache_key)
        self.assertIn("argsHash", cache_key)
        self.assertIn("callIndex", cache_key)
        self.assertIn("promptHash", cache_key)
        self.assertIn("optionsHash", cache_key)
        self.assertEqual(["agent_registered"], self.event_types(store))
        event = store.replay_events(run.run_id)[0]
        self.assertEqual(job.job_id, event.job_id)
        self.assertEqual(cache_key, event.payload["cacheKey"])

    def test_run_all_moves_successful_job_through_running_to_succeeded_and_writes_result(self):
        scheduler, store, run = self.make_scheduler(runner=FakeChildAgentRunner(results={"agent_1": {"summary": "ok"}}))
        job = scheduler.register_agent(prompt="do work")

        completed = scheduler.run_all()

        self.assertEqual([job], completed)
        self.assertEqual("succeeded", job.status)
        self.assertEqual({"summary": "ok"}, job.metadata["result"])
        self.assertIsNotNone(job.result_ref)
        self.assertEqual(["agent_registered", "agent_started", "agent_completed"], self.event_types(store))
        loaded = store.load_run(run.run_id)
        self.assertEqual("succeeded", loaded.jobs[0].status)
        self.assertEqual(job.result_ref, loaded.jobs[0].result_ref)

    def test_concurrency_limit_only_starts_configured_number_of_jobs_per_tick(self):
        scheduler, _store, _ = self.make_scheduler(max_concurrent=3, runner=FakeChildAgentRunner(delay_ticks=1))
        for index in range(20):
            scheduler.register_agent(prompt=f"job {index}")

        scheduler.tick()

        self.assertEqual(3, scheduler.running_count)
        self.assertEqual(17, scheduler.queued_count)
        statuses = [job.status for job in scheduler.jobs]
        self.assertEqual(3, statuses.count("running"))
        self.assertEqual(17, statuses.count("queued"))

    def test_total_agents_cap_rejects_excess_job_and_records_event(self):
        scheduler, store, _ = self.make_scheduler(max_total=2)
        scheduler.register_agent(prompt="one")
        scheduler.register_agent(prompt="two")

        with self.assertRaises(RuntimeError):
            scheduler.register_agent(prompt="three")

        self.assertEqual(["agent_registered", "agent_registered", "agent_rejected"], self.event_types(store))
        rejected = store.replay_events("wf_test")[-1]
        self.assertEqual("max_total_exceeded", rejected.payload["reason"])
        self.assertEqual(2, len(scheduler.jobs))

    def test_continue_failure_policy_keeps_other_jobs_running(self):
        runner = FakeChildAgentRunner(fail_job_ids={"agent_1"}, delay_ticks=0)
        scheduler, store, _ = self.make_scheduler(max_concurrent=2, runner=runner)
        failed = scheduler.register_agent(prompt="fail")
        succeeded = scheduler.register_agent(prompt="succeed")

        scheduler.run_all(failure_policy="continue")

        self.assertEqual("failed", failed.status)
        self.assertEqual("succeeded", succeeded.status)
        self.assertEqual("running", scheduler.run.status)
        self.assertEqual(
            ["agent_registered", "agent_registered", "agent_started", "agent_started", "agent_failed", "agent_completed"],
            self.event_types(store),
        )

    def test_fail_fast_failure_policy_cancels_queued_and_running_jobs_and_fails_run(self):
        runner = FakeChildAgentRunner(fail_job_ids={"agent_1"}, delay_ticks=1)
        scheduler, store, _ = self.make_scheduler(max_concurrent=2, runner=runner)
        failed = scheduler.register_agent(prompt="fail")
        running_cancelled = scheduler.register_agent(prompt="running")
        queued_cancelled = scheduler.register_agent(prompt="queued")

        scheduler.run_all(failure_policy="fail_fast")

        self.assertEqual("failed", failed.status)
        self.assertEqual("cancelled", running_cancelled.status)
        self.assertEqual("cancelled", queued_cancelled.status)
        self.assertEqual("failed", scheduler.run.status)
        self.assertIn("agent_failed", self.event_types(store))
        self.assertEqual(2, self.event_types(store).count("agent_cancelled"))

    def test_stop_cancels_queued_jobs_and_requests_cancellation_for_running_jobs(self):
        runner = FakeChildAgentRunner(delay_ticks=2)
        scheduler, store, _ = self.make_scheduler(max_concurrent=2, runner=runner)
        running_one = scheduler.register_agent(prompt="one")
        running_two = scheduler.register_agent(prompt="two")
        queued = scheduler.register_agent(prompt="three")
        scheduler.tick()

        scheduler.stop(reason="user stop")
        scheduler.run_all()

        self.assertEqual("cancelled", running_one.status)
        self.assertEqual("cancelled", running_two.status)
        self.assertEqual("cancelled", queued.status)
        self.assertEqual("killed", scheduler.run.status)
        self.assertEqual({"agent_1", "agent_2"}, runner.cancelled_job_ids)
        self.assertEqual(3, self.event_types(store).count("agent_cancelled"))


if __name__ == "__main__":
    unittest.main()
