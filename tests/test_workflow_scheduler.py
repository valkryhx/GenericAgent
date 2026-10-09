import json
import tempfile
import unittest
from pathlib import Path

from workflow_models import AgentResult, WorkflowRun
from workflow_scheduler import AgentScheduler, FakeChildAgentRunner, SchedulerConfig, normalize_retry_policy
from workflow_store import WorkflowStore


class FailedResultRunner:
    def start(self, job):
        pass

    def poll(self, job):
        return AgentResult(
            job_id=job.job_id,
            status="failed",
            payload={"error": "api down"},
            transcript_ref=f"agents/{job.job_id}/transcript.jsonl",
            transcript_events=[{"type": "error", "error": "api down"}],
        )

    def cancel(self, job):
        pass


class SequenceResultRunner:
    def __init__(self):
        self.polls = {}

    def start(self, job):
        self.polls.setdefault(job.job_id, 0)

    def poll(self, job):
        attempt = self.polls.get(job.job_id, 0)
        self.polls[job.job_id] = attempt + 1
        if attempt == 0:
            return AgentResult(job_id=job.job_id, status="failed", payload={"error": "provider transient"})
        return AgentResult(job_id=job.job_id, payload={"summary": "recovered"})

    def cancel(self, job):
        pass


class LabelAwareRunner:
    def __init__(self, results_by_label=None, fail_labels=None):
        self.results_by_label = results_by_label or {}
        self.fail_labels = set(fail_labels or set())

    def start(self, job):
        pass

    def poll(self, job):
        label = job.metadata.get("label")
        if label in self.fail_labels:
            return AgentResult(job_id=job.job_id, status="failed", payload={"error": "child failed"})
        return AgentResult(job_id=job.job_id, payload=self.results_by_label.get(label, {"summary": f"done {label}"}))

    def cancel(self, job):
        pass


class ProtocolMetadataRunner:
    def __init__(self):
        self.started = []
        self.cancelled = []

    def start(self, job):
        self.started.append(job.job_id)

    def poll(self, job):
        return AgentResult(
            job_id=job.job_id,
            payload={"summary": "real-ish child result", "text": "verbose child transcript text"},
            transcript_ref=f"agents/{job.job_id}/transcript.jsonl",
            token_usage={"input_tokens": 5, "output_tokens": 7},
            tool_summary={},
            transcript_events=[
                {"type": "metadata", "runId": "wf_test", "jobId": job.job_id},
                {"type": "assistant", "text": "verbose child transcript text"},
            ],
        )

    def cancel(self, job):
        self.cancelled.append(job.job_id)


class PermissionEventsRunner:
    def __init__(self, *, status="succeeded"):
        self.status = status

    def start(self, job):
        pass

    def poll(self, job):
        return AgentResult(
            job_id=job.job_id,
            status=self.status,
            payload={"summary": "permission checked"} if self.status == "succeeded" else {"error": "permission failure"},
            transcript_events=[
                {"type": "metadata", "runId": "wf_test", "jobId": job.job_id},
                {
                    "type": "permission_profile_selected",
                    "runId": "wf_test",
                    "jobId": job.job_id,
                    "toolName": "file_read",
                    "profile": "read_only",
                    "decision": "allow",
                    "reason": "read_only_static_safe",
                    "permission": {"action": "allow", "reason": "read_only_static_safe", "profile": "read_only"},
                },
                {
                    "type": "tool_allowed",
                    "runId": "wf_test",
                    "jobId": job.job_id,
                    "toolName": "file_read",
                    "profile": "read_only",
                    "decision": "allow",
                    "reason": "read_only_static_safe",
                    "permission": {"action": "allow", "reason": "read_only_static_safe", "profile": "read_only"},
                },
                {
                    "type": "tool_denied",
                    "runId": "wf_test",
                    "jobId": job.job_id,
                    "toolName": "file_write",
                    "profile": "read_only",
                    "decision": "deny",
                    "reason": "read_only_static_write_or_execute",
                    "permission": {"action": "deny", "reason": "read_only_static_write_or_execute", "profile": "read_only"},
                },
            ],
        )

    def cancel(self, job):
        pass


class WorkflowSchedulerTest(unittest.TestCase):
    def make_scheduler(self, *, max_concurrent=4, max_total=1000, runner=None, run_kwargs=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = WorkflowStore(root=tmp.name)
        run_data = {"run_id": "wf_test", "session_id": "session_test", "script": "spawn agents", "status": "running"}
        run_data.update(run_kwargs or {})
        run = store.create_run(WorkflowRun(**run_data))
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
        self.assertEqual("wf_test", job.metadata["runId"])
        self.assertEqual("inherit-current-permissions", job.metadata["permissionProfile"])
        self.assertEqual("inherit-current-v1", job.metadata["permissionPolicyVersion"])
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
        progress = json.loads((Path(run.artifact_dir) / "workflow-progress.json").read_text(encoding="utf-8"))
        entry = progress["workflowProgress"][0]
        self.assertEqual("queued", entry["state"])
        self.assertEqual("Scout", entry["label"])
        self.assertEqual("agent_1", entry["agentId"])
        self.assertEqual("agent_1", entry["jobId"])
        self.assertIn("inspect repo", entry["promptPreview"])

    def test_evidence_roles_get_host_enforced_non_mutating_profile(self):
        scheduler, _store, _run = self.make_scheduler()

        reviewer = scheduler.register_agent(prompt="review it", label="Review", options={"role": "review"})
        verifier = scheduler.register_agent(prompt="verify it", label="Verify", options={"role": "verification"})
        implementer = scheduler.register_agent(prompt="build it", label="Build", options={"role": "implementation"})

        self.assertEqual("verify", reviewer.metadata["permissionProfile"])
        self.assertEqual("verify", verifier.metadata["permissionProfile"])
        self.assertEqual("inherit-current-permissions", implementer.metadata["permissionProfile"])
        self.assertEqual("verify", verifier.metadata["cacheKey"]["permissionProfile"])

    def test_verify_profile_never_loosens_a_read_only_run(self):
        scheduler, _store, _run = self.make_scheduler(
            run_kwargs={"permission_profile": "read_only", "permission_policy_version": "read-only-v1"}
        )

        job = scheduler.register_agent(prompt="verify it", label="Verify", options={"role": "verification"})

        self.assertEqual("read_only", job.metadata["permissionProfile"])

    def test_observed_mutations_are_recorded_from_tool_events(self):
        scheduler, store, run = self.make_scheduler()
        job = scheduler.register_agent(prompt="edit it", label="Editor", options={"role": "implementation"})
        result = AgentResult(
            job_id=job.job_id,
            status="succeeded",
            payload={"summary": "edited"},
            transcript_events=[
                {
                    "type": "tool_allowed",
                    "toolName": "file_patch",
                    "decision": "allow",
                    "permission": {"action": "allow"},
                }
            ],
        )

        scheduler._record_observed_mutations(job, result)

        self.assertEqual(["file_patch"], job.metadata["observedMutations"])
        self.assertEqual([job.job_id], store.load_run(run.run_id).metadata["observedMutationAgents"])
        self.assertIn("state_mutation_observed", self.event_types(store))

    def test_filesystem_evidence_records_a_mutation_for_an_unlisted_writer_tool(self):
        """A tool we have not enumerated must still count as a state mutation.

        ``observedMutations`` used to be derived from a fixed tool-name list, so
        a writer added later -- or ``code_run`` -- silently produced no mutation
        evidence even though the workspace changed.
        """
        scheduler, store, run = self.make_scheduler()
        job = scheduler.register_agent(prompt="write via a new tool", label="Writer")
        result = AgentResult(
            job_id=job.job_id,
            status="succeeded",
            payload={"summary": "wrote"},
            tool_summary={"writtenPaths": ["report.html"]},
            transcript_events=[
                {"type": "tool_allowed", "toolName": "some_future_writer", "decision": "allow"},
            ],
        )

        scheduler._record_observed_mutations(job, result)

        self.assertEqual(["workspace_write"], job.metadata["observedMutations"])
        self.assertEqual(["workspace_write"], store.load_run(run.run_id).metadata["observedMutationTools"])

    def test_observed_artifacts_come_from_filesystem_changes_not_tool_names(self):
        """Artifact observation must not depend on ``file_write``/``file_patch``.

        The runner reports the workspace before/after diff, so a file produced by
        ``code_run`` (or any future writer) is observed exactly like one produced
        by ``file_write``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scheduler, store, run = self.make_scheduler(run_kwargs={"metadata": {"workspacePath": str(workspace)}})
            scheduler.args = {"workspacePath": str(workspace)}
            job = scheduler.register_agent(prompt="write the docx", label="Writer")
            result = AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "wrote"},
                tool_summary={"writtenPaths": ["reports/summary.docx", "notes.md"]},
                transcript_events=[
                    {"type": "tool_allowed", "toolName": "code_run", "decision": "allow"},
                ],
            )

            scheduler._record_observed_artifacts(job, result)

            self.assertEqual(
                ["notes.md", "reports/summary.docx"],
                sorted(entry["path"] for entry in job.metadata["observedArtifacts"]),
            )
            progress = json.loads((Path(run.artifact_dir) / "workflow-progress.json").read_text(encoding="utf-8"))
            self.assertEqual(
                ["notes.md", "reports/summary.docx"],
                sorted(entry["path"] for entry in progress["workflowProgress"][0]["observedArtifacts"]),
            )

    def test_observed_artifacts_record_the_writing_job(self):
        """Each artifact must name the job that wrote it.

        Without ownership the handoff can only hand over an opaque path; when two
        children write the same filename there is no way to tell which content is
        in the file or who to blame for overwriting whom.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scheduler, store, run = self.make_scheduler(run_kwargs={"metadata": {"workspacePath": str(workspace)}})
            scheduler.args = {"workspacePath": str(workspace)}
            job = scheduler.register_agent(prompt="write the report", label="Synthesis")
            result = AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "wrote"},
                tool_summary={"writtenPaths": ["report.md"]},
            )

            scheduler._record_observed_artifacts(job, result)

            self.assertEqual([{"path": "report.md", "writer": "Synthesis"}], job.metadata["observedArtifacts"])
            progress = json.loads((Path(run.artifact_dir) / "workflow-progress.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [{"path": "report.md", "writer": "Synthesis"}],
                progress["workflowProgress"][0]["observedArtifacts"],
            )

    def test_owner_falls_back_to_the_job_id_when_no_label_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scheduler, _store, _run = self.make_scheduler(run_kwargs={"metadata": {"workspacePath": str(workspace)}})
            scheduler.args = {"workspacePath": str(workspace)}
            job = scheduler.register_agent(prompt="write")
            result = AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "wrote"},
                tool_summary={"writtenPaths": ["report.md"]},
            )

            scheduler._record_observed_artifacts(job, result)

            self.assertEqual([{"path": "report.md", "writer": job.job_id}], job.metadata["observedArtifacts"])

    def test_a_path_written_by_two_jobs_is_recorded_as_a_collision(self):
        """A silent overwrite must become a visible, recorded fact."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scheduler, store, run = self.make_scheduler(run_kwargs={"metadata": {"workspacePath": str(workspace)}})
            scheduler.args = {"workspacePath": str(workspace)}
            first = scheduler.register_agent(prompt="research", label="Research")
            second = scheduler.register_agent(prompt="synthesize", label="Synthesis")

            scheduler._record_observed_artifacts(first, AgentResult(
                job_id=first.job_id, status="succeeded", payload={}, tool_summary={"writtenPaths": ["report.md"]},
            ))
            scheduler._record_observed_artifacts(second, AgentResult(
                job_id=second.job_id, status="succeeded", payload={}, tool_summary={"writtenPaths": ["report.md"]},
            ))

            loaded = store.load_run(run.run_id)
            self.assertEqual({"report.md": ["Research", "Synthesis"]}, loaded.metadata["artifactCollisions"])
            codes = [issue["code"] for issue in loaded.metadata["workflowIssues"]]
            self.assertIn("artifact_path_collision", codes)
            self.assertIn("artifact_collision", self.event_types(store))
            # Ownership is per job, so both jobs still name themselves the writer.
            self.assertEqual([{"path": "report.md", "writer": "Research"}], first.metadata["observedArtifacts"])
            self.assertEqual([{"path": "report.md", "writer": "Synthesis"}], second.metadata["observedArtifacts"])

    def test_no_collision_is_recorded_when_each_job_writes_its_own_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scheduler, store, run = self.make_scheduler(run_kwargs={"metadata": {"workspacePath": str(workspace)}})
            scheduler.args = {"workspacePath": str(workspace)}
            first = scheduler.register_agent(prompt="research", label="Research")
            second = scheduler.register_agent(prompt="synthesize", label="Synthesis")

            scheduler._record_observed_artifacts(first, AgentResult(
                job_id=first.job_id, status="succeeded", payload={}, tool_summary={"writtenPaths": ["notes.md"]},
            ))
            scheduler._record_observed_artifacts(second, AgentResult(
                job_id=second.job_id, status="succeeded", payload={}, tool_summary={"writtenPaths": ["report.md"]},
            ))

            loaded = store.load_run(run.run_id)
            self.assertNotIn("artifactCollisions", loaded.metadata or {})
            self.assertNotIn("artifact_collision", self.event_types(store))

    def test_recording_a_write_persists_the_run_level_ownership_index(self):
        """The run index must exist for consumers that never see a job handoff.

        A child runs in its own process and keeps no in-process ``handoff``
        dict, so ownership has to be readable from run metadata/progress.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scheduler, _store, run = self.make_scheduler(run_kwargs={"metadata": {"workspacePath": str(workspace)}})
            scheduler.args = {"workspacePath": str(workspace)}
            job = scheduler.register_agent(prompt="write it", label="Research")
            (workspace / "notes.md").write_text("# notes", encoding="utf-8")
            result = AgentResult(
                job_id=job.job_id, status="succeeded", payload={"summary": "wrote"},
                tool_summary={"writtenPaths": ["notes.md"]},
            )

            scheduler._record_observed_artifacts(job, result)

            reloaded = scheduler.store.load_run(run.run_id)
            self.assertEqual({"notes.md": ["Research"]}, (reloaded.metadata or {}).get("artifactOwnership"))

    def test_handoff_names_the_job_that_wrote_each_artifact(self):
        """A downstream reader must be able to tell whose output it is reading."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scheduler, _store, _run = self.make_scheduler(run_kwargs={"metadata": {"workspacePath": str(workspace)}})
            scheduler.args = {"workspacePath": str(workspace)}
            job = scheduler.register_agent(prompt="write it", label="Synthesis")
            (workspace / "report.md").write_text("# report", encoding="utf-8")
            result = AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "wrote the report"},
                tool_summary={"writtenPaths": ["report.md"]},
            )

            scheduler._record_observed_artifacts(job, result)
            handoff = scheduler._build_handoff(job, result)

            self.assertEqual(["report.md"], handoff["artifactRefs"])
            self.assertEqual({"report.md": ["Synthesis"]}, handoff["artifactOwners"])

    def test_handoff_ownership_survives_into_the_downstream_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scheduler, _store, _run = self.make_scheduler(run_kwargs={"metadata": {"workspacePath": str(workspace)}})
            scheduler.args = {"workspacePath": str(workspace)}
            job = scheduler.register_agent(prompt="write it", label="Research")
            (workspace / "notes.md").write_text("# notes", encoding="utf-8")
            result = AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "wrote notes"},
                tool_summary={"writtenPaths": ["notes.md"]},
            )

            scheduler._record_observed_artifacts(job, result)
            job.metadata["handoff"] = scheduler._build_handoff(job, result)
            downstream = scheduler.downstream_result(job)

            self.assertEqual(["notes.md"], downstream["artifactRefs"])
            self.assertEqual({"notes.md": ["Research"]}, downstream["artifactOwners"])

    def test_dependency_handoff_carries_ownership_to_the_next_child(self):
        """The dependent child must see who produced each upstream ref."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scheduler, _store, _run = self.make_scheduler(run_kwargs={"metadata": {"workspacePath": str(workspace)}})
            scheduler.args = {"workspacePath": str(workspace)}
            upstream = scheduler.register_agent(prompt="research", label="Research")
            downstream = scheduler.register_agent(prompt="synthesize", label="Synthesis", options={"dependsOn": ["Research"]})

            (workspace / "notes.md").write_text("# notes", encoding="utf-8")
            result = AgentResult(
                job_id=upstream.job_id,
                status="succeeded",
                payload={"summary": "researched"},
                tool_summary={"writtenPaths": ["notes.md"]},
            )
            scheduler._record_observed_artifacts(upstream, result)
            upstream.metadata["handoff"] = scheduler._build_handoff(upstream, result)
            upstream.status = "succeeded"

            handoff = scheduler._build_dependency_handoff(downstream)

            self.assertEqual(1, len(handoff))
            self.assertEqual(["notes.md"], handoff[0]["artifactRefs"])
            self.assertEqual({"notes.md": ["Research"]}, handoff[0]["artifactOwners"])

    def test_handoff_reports_when_another_job_also_wrote_the_same_ref(self):
        """Ownership is run-wide, so an overwritten artifact is visible."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scheduler, _store, _run = self.make_scheduler(run_kwargs={"metadata": {"workspacePath": str(workspace)}})
            scheduler.args = {"workspacePath": str(workspace)}
            first = scheduler.register_agent(prompt="research", label="Research")
            second = scheduler.register_agent(prompt="synthesize", label="Synthesis")

            (workspace / "report.md").write_text("research version", encoding="utf-8")
            first_result = AgentResult(
                job_id=first.job_id, status="succeeded", payload={"summary": "a"},
                tool_summary={"writtenPaths": ["report.md"]},
            )
            scheduler._record_observed_artifacts(first, first_result)
            (workspace / "report.md").write_text("synthesis version", encoding="utf-8")
            second_result = AgentResult(
                job_id=second.job_id, status="succeeded", payload={"summary": "b"},
                tool_summary={"writtenPaths": ["report.md"]},
            )
            scheduler._record_observed_artifacts(second, second_result)

            handoff = scheduler._build_handoff(second, second_result)

            self.assertEqual({"report.md": ["Research", "Synthesis"]}, handoff["artifactOwners"])

    def test_handoff_omits_ownership_when_no_artifact_was_observed(self):
        scheduler, _store, _run = self.make_scheduler()
        job = scheduler.register_agent(prompt="think", label="Thinker")
        result = AgentResult(job_id=job.job_id, status="succeeded", payload={"summary": "thought"})

        handoff = scheduler._build_handoff(job, result)

        self.assertEqual([], handoff["artifactRefs"])
        self.assertEqual({}, handoff["artifactOwners"])

    def test_observed_artifacts_drop_paths_outside_the_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scheduler, _store, _run = self.make_scheduler(run_kwargs={"metadata": {"workspacePath": str(workspace)}})
            scheduler.args = {"workspacePath": str(workspace)}
            job = scheduler.register_agent(prompt="write", label="Writer")
            result = AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "wrote"},
                tool_summary={"writtenPaths": ["ok.md", r"C:\Windows\system32\drivers\etc\hosts", "../escape.md"]},
            )

            scheduler._record_observed_artifacts(job, result)

            self.assertEqual([{"path": "ok.md", "writer": "Writer"}], job.metadata["observedArtifacts"])

    def test_register_agent_rejects_non_dict_options_with_clear_error(self):
        scheduler, _store, _run = self.make_scheduler()

        with self.assertRaisesRegex(TypeError, "agent options must be a plain object"):
            scheduler.register_agent(prompt="inspect repo", options=[["label", "Scout"]])

        self.assertEqual([], scheduler.jobs)

    def test_register_cached_agent_rejects_non_dict_options_with_clear_error(self):
        scheduler, _store, _run = self.make_scheduler()
        result = AgentResult(job_id="agent_1", status="succeeded", payload={"summary": "ok"})

        with self.assertRaisesRegex(TypeError, "agent options must be a plain object"):
            scheduler.register_cached_agent(prompt="inspect repo", options="bad", result=result)

        self.assertEqual([], scheduler.jobs)

    def test_cache_key_changes_when_permission_profile_or_policy_version_changes(self):
        scheduler_a, _store_a, _run_a = self.make_scheduler()
        scheduler_b, _store_b, _run_b = self.make_scheduler(
            run_kwargs={
                "run_id": "wf_test_b",
                "permission_profile": "read_only",
                "permission_policy_version": "read-only-v1",
            }
        )

        job_a = scheduler_a.register_agent(prompt="same", options={"effort": "low"})
        job_b = scheduler_b.register_agent(prompt="same", options={"effort": "low"})

        self.assertNotEqual(job_a.metadata["cacheKey"], job_b.metadata["cacheKey"])
        self.assertEqual("inherit-current-permissions", job_a.metadata["permissionProfile"])
        self.assertEqual("read_only", job_b.metadata["permissionProfile"])
        self.assertEqual("inherit-current-v1", job_a.metadata["permissionPolicyVersion"])
        self.assertEqual("read-only-v1", job_b.metadata["permissionPolicyVersion"])

    def test_cache_key_args_hash_uses_runtime_args(self):
        scheduler_a, _store_a, _run_a = self.make_scheduler()
        scheduler_b, _store_b, _run_b = self.make_scheduler(run_kwargs={"run_id": "wf_test_b"})
        scheduler_a.args = {"target": "alpha"}
        scheduler_b.args = {"target": "beta"}

        job_a = scheduler_a.register_agent(prompt="same", options={"effort": "low"})
        job_b = scheduler_b.register_agent(prompt="same", options={"effort": "low"})

        self.assertNotEqual(job_a.metadata["cacheKey"]["argsHash"], job_b.metadata["cacheKey"]["argsHash"])
        self.assertNotEqual(job_a.metadata["cacheKey"], job_b.metadata["cacheKey"])

    def test_register_agent_records_canonical_workspace_from_runtime_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            scheduler, store, run = self.make_scheduler()
            scheduler.args = {"workspace": str(workspace)}

            job = scheduler.register_agent(prompt="write relative file", label="coder")

            expected = str(workspace.resolve())
            self.assertEqual(expected, job.metadata["workspacePath"])
            self.assertEqual(expected, store.load_run(run.run_id).metadata["workspacePath"])

    def test_register_cached_agent_records_canonical_workspace_from_runtime_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            scheduler, store, run = self.make_scheduler()
            scheduler.args = {"workspacePath": str(workspace)}
            result = AgentResult(job_id="source_agent_1", status="succeeded", payload={"summary": "cached"})

            job = scheduler.register_cached_agent(prompt="reuse result", result=result)

            expected = str(workspace.resolve())
            self.assertEqual(expected, job.metadata["workspacePath"])
            self.assertEqual(expected, store.load_run(run.run_id).metadata["workspacePath"])

    def test_register_agent_rejects_conflicting_workspace_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_a = Path(tmp) / "workspace-a"
            workspace_b = Path(tmp) / "workspace-b"
            workspace_a.mkdir()
            workspace_b.mkdir()
            scheduler, _store, _run = self.make_scheduler()
            scheduler.args = {"workspacePath": str(workspace_a), "workspace": str(workspace_b)}

            with self.assertRaisesRegex(ValueError, "must resolve to the same directory"):
                scheduler.register_agent(prompt="ambiguous workspace")

            self.assertEqual([], scheduler.jobs)

    def test_cache_key_distinguishes_json_values_from_json_like_strings(self):
        scheduler_object, _store_object, _run_object = self.make_scheduler()
        scheduler_string, _store_string, _run_string = self.make_scheduler(run_kwargs={"run_id": "wf_test_string"})
        scheduler_none, _store_none, _run_none = self.make_scheduler(run_kwargs={"run_id": "wf_test_none"})
        scheduler_null_string, _store_null_string, _run_null_string = self.make_scheduler(run_kwargs={"run_id": "wf_test_null_string"})
        scheduler_object.args = {}
        scheduler_string.args = "{}"
        scheduler_none.args = None
        scheduler_null_string.args = "null"

        object_job = scheduler_object.register_agent(prompt="same")
        string_job = scheduler_string.register_agent(prompt="same")
        none_job = scheduler_none.register_agent(prompt="same")
        null_string_job = scheduler_null_string.register_agent(prompt="same")

        self.assertNotEqual(object_job.metadata["cacheKey"]["argsHash"], string_job.metadata["cacheKey"]["argsHash"])
        self.assertNotEqual(none_job.metadata["cacheKey"]["argsHash"], null_string_job.metadata["cacheKey"]["argsHash"])

    def test_run_all_marks_cached_jobs_as_successful_completion(self):
        scheduler, store, run = self.make_scheduler()
        scheduler.register_cached_agent(
            prompt="cached work",
            result=AgentResult(job_id="agent_source", payload={"summary": "cached"}),
            source_run_id="wf_source",
            source_job_id="agent_1",
        )

        completed = scheduler.run_all()

        self.assertEqual([], completed)
        loaded = store.load_run(run.run_id)
        self.assertEqual("succeeded", loaded.status)
        self.assertEqual("cached", loaded.jobs[0].status)
        final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
        self.assertEqual("succeeded", final_result["status"])
        self.assertEqual("cached", final_result["jobs"][0]["status"])

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

    def test_degraded_upstream_still_unblocks_dependent_job(self):
        """A degraded result is usable delivery, not a failure.

        The downstream job must run (the run is already reported degraded),
        instead of the whole tail of the plan being skipped.
        """
        scheduler, store, run = self.make_scheduler(runner=FakeChildAgentRunner(results={
            "agent_1": {"summary": "plain text"},
            "agent_2": {"summary": "synthesis"},
        }))
        upstream = scheduler.register_agent(
            prompt="collect",
            label="collector",
            options={"schema": {"type": "object", "required": ["sources"]}, "fallback": "text"},
        )
        downstream = scheduler.register_agent(prompt="synthesize", label="writer", options={"dependsOn": ["collector"], "role": "synthesis"})
        downstream.metadata["dependsOn"] = ["collector"]

        scheduler.run_all()

        self.assertEqual("degraded", upstream.status)
        self.assertEqual("succeeded", downstream.status)
        self.assertEqual("degraded", store.load_run(run.run_id).status)

    def test_schema_fallback_records_workflow_issue_in_scheduler_artifacts(self):
        scheduler, store, run = self.make_scheduler(runner=FakeChildAgentRunner(results={"agent_1": {"summary": "plain text"}}))
        job = scheduler.register_agent(
            prompt="collect sources",
            label="collector",
            options={"schema": {"type": "object", "required": ["sources"]}, "fallback": "text"},
        )

        scheduler.run_all()

        loaded = store.load_run(run.run_id)
        self.assertEqual("degraded", loaded.status)
        loaded_job = loaded.jobs[0]
        self.assertEqual("degraded", loaded_job.status)
        self.assertTrue(loaded_job.metadata["schemaValidation"]["fallbackApplied"])
        self.assertTrue(loaded_job.metadata["result"]["schemaFallback"])
        self.assertEqual("degraded", loaded.metadata["executionOutcome"])
        self.assertEqual("schema_validation_failed", loaded.metadata["workflowIssues"][0]["code"])
        events = store.replay_events(run.run_id)
        self.assertIn("workflow_issue", [event.event_type for event in events])
        result_data = json.loads((Path(run.artifact_dir) / job.result_ref).read_text(encoding="utf-8"))
        self.assertTrue(result_data["payload"]["schemaFallback"])
        progress = json.loads((Path(run.artifact_dir) / "workflow-progress.json").read_text(encoding="utf-8"))
        self.assertEqual(loaded.metadata["workflowIssues"], progress["workflowIssues"])
        final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
        self.assertEqual(loaded.metadata["workflowIssues"], final_result["workflowIssues"])

    def test_strict_schema_accepts_json_object_returned_in_child_text(self):
        scheduler, store, run = self.make_scheduler(
            runner=FakeChildAgentRunner(
                results={
                    "agent_1": {
                        "summary": '{"verificationPassed": true, "checks": [], "blockingIssues": []}',
                        "text": '{"verificationPassed": true, "checks": [], "blockingIssues": []}',
                    }
                }
            )
        )
        job = scheduler.register_agent(
            prompt="return verification JSON",
            label="verify",
            options={
                "role": "verification",
                "schema": {
                    "type": "object",
                    "required": ["verificationPassed", "checks", "blockingIssues"],
                },
                "strictSchema": True,
            },
        )

        scheduler.run_all()

        loaded = store.load_run(run.run_id)
        self.assertEqual("succeeded", loaded.jobs[0].status)
        self.assertTrue(loaded.jobs[0].metadata["result"]["verificationPassed"])
        self.assertTrue(loaded.jobs[0].metadata["schemaValidation"]["ok"])

    def test_schema_failure_feeds_issues_into_the_next_attempt_metadata(self):
        """A retried child must be told which fields the host rejected.

        Regression: the scheduler re-queued the identical prompt, so the retry
        reproduced the identical schema failure.
        """
        class RecordingRunner:
            def __init__(self):
                self.polls = {}
                self.feedback_by_attempt = []

            def start(self, job):
                self.polls.setdefault(job.job_id, 0)

            def poll(self, job):
                attempt = self.polls.get(job.job_id, 0)
                self.polls[job.job_id] = attempt + 1
                self.feedback_by_attempt.append(job.metadata.get("retryFeedback"))
                if attempt == 0:
                    return AgentResult(job_id=job.job_id, payload={"summary": "prose"})
                return AgentResult(job_id=job.job_id, payload={"sources": ["S1"], "claims": ["C1"]})

            def cancel(self, job):
                pass

        runner = RecordingRunner()
        scheduler, _store, _run = self.make_scheduler(runner=runner)
        scheduler.register_agent(
            prompt="collect sources",
            label="collector",
            options={
                "schema": {"type": "object", "required": ["sources", "claims"]},
                "retryPolicy": {"maxAttempts": 3, "retryableErrors": ["schema_validation_failed"], "backoffMs": 0},
            },
        )

        scheduler.run_all()

        self.assertIsNone(runner.feedback_by_attempt[0])
        retry_feedback = runner.feedback_by_attempt[1]
        self.assertIsInstance(retry_feedback, dict)
        self.assertIn("missing required field: sources", retry_feedback["issues"])
        self.assertIn("missing required field: claims", retry_feedback["issues"])

    def test_downstream_result_exposes_validated_schema_fields_to_the_script(self):
        """The script must receive the structured value it asked the agent for.

        Regression: the RPC boundary only ever exposed ``summary``/``text``, so a
        schema-validated answer reached the workflow script as prose (the
        ``Array.isArray(result.sources)`` probes all read ``-1``).
        """
        scheduler, store, run = self.make_scheduler(
            runner=FakeChildAgentRunner(
                results={
                    "agent_1": {
                        "sources": [{"id": "S1"}],
                        "claims": [{"id": "C1"}],
                        "summary": "collected",
                        "text": "long raw transcript text",
                    }
                }
            )
        )
        job = scheduler.register_agent(
            prompt="collect sources",
            label="collector",
            options={
                "schema": {
                    "type": "object",
                    "required": ["sources", "claims"],
                    "properties": {"sources": {"type": "array"}, "claims": {"type": "array"}},
                }
            },
        )
        scheduler.run_all()

        downstream = scheduler.downstream_result(store.load_run(run.run_id).jobs[0])

        self.assertEqual([{"id": "S1"}], downstream["sources"])
        self.assertEqual([{"id": "C1"}], downstream["claims"])
        self.assertEqual("collected", downstream["summary"])
        self.assertNotIn("text", downstream)
        self.assertTrue(downstream["schemaValidation"]["ok"])

    def test_downstream_result_hides_schema_fields_when_validation_failed(self):
        scheduler, store, run = self.make_scheduler(
            runner=FakeChildAgentRunner(results={"agent_1": {"summary": "prose only"}})
        )
        job = scheduler.register_agent(
            prompt="collect sources",
            label="collector",
            options={
                "schema": {"type": "object", "required": ["sources"]},
                "fallback": "text",
            },
        )
        scheduler.run_all()

        downstream = scheduler.downstream_result(store.load_run(run.run_id).jobs[0])

        self.assertNotIn("sources", downstream)
        self.assertTrue(downstream["schemaFallback"])

    def test_handoff_carries_the_file_a_child_actually_wrote(self):
        """The next reader needs a resolvable path, not a plan label.

        Regression: the plan's ``artifacts: ["synthesis"]`` is a semantic name,
        so the handoff ``artifactRefs`` was empty and the GA agent guessed the
        run's internal directory instead of reading the file the child wrote at
        the workspace root.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "synthesis_report.md").write_text("# report\n", encoding="utf-8")
            scheduler, store, run = self.make_scheduler(
                runner=FakeChildAgentRunner(results={"agent_1": {"summary": "written"}}),
                run_kwargs={"metadata": {"workspacePath": str(workspace)}},
            )
            job = scheduler.register_agent(
                prompt="write the report",
                label="synthesis",
                options={"deliverables": ["reports/synthesis.md"]},
            )
            scheduler.run_all()
            scheduler.jobs[0].metadata["observedArtifacts"] = ["synthesis_report.md"]
            result = AgentResult(job_id="agent_1", payload={"summary": "written"})

            handoff = scheduler._build_handoff(scheduler.jobs[0], result)

            self.assertIn("synthesis_report.md", handoff["artifactRefs"])

    def test_handoff_ignores_observed_paths_that_do_not_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            scheduler, store, run = self.make_scheduler(
                runner=FakeChildAgentRunner(),
                run_kwargs={"metadata": {"workspacePath": str(workspace)}},
            )
            job = scheduler.register_agent(prompt="write", label="writer")
            job.metadata["observedArtifacts"] = ["never_written.md"]

            handoff = scheduler._build_handoff(job, AgentResult(job_id="agent_1", payload={"summary": "x"}))

            self.assertNotIn("never_written.md", handoff["artifactRefs"])

    def test_handoff_rejects_observed_paths_outside_the_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            scheduler, store, run = self.make_scheduler(
                runner=FakeChildAgentRunner(),
                run_kwargs={"metadata": {"workspacePath": str(workspace)}},
            )
            job = scheduler.register_agent(prompt="write", label="writer")
            job.metadata["observedArtifacts"] = ["../escape.md", "C:/Windows/system32/drivers/etc/hosts"]

            handoff = scheduler._build_handoff(job, AgentResult(job_id="agent_1", payload={"summary": "x"}))

            self.assertEqual([], handoff["artifactRefs"])

    def test_schema_repair_receives_original_assignment_and_schema(self):
        """The repair packet must be a real retry, not a blind regeneration."""
        scheduler, store, run = self.make_scheduler(
            runner=FakeChildAgentRunner(results={"agent_2": {"sources": ["S1"]}})
        )
        upstream = scheduler.register_agent(
            prompt="collect sources for the quarterly report",
            label="collector",
            options={
                "schema": {"type": "object", "required": ["sources"]},
                "retryPolicy": {"maxAttempts": 1, "retryableErrors": [], "repairRole": "repair"},
            },
        )
        upstream.status = "failed"
        upstream.error = "schema_validation_failed"
        upstream.metadata["schemaValidation"] = {
            "ok": False,
            "code": "schema_validation_failed",
            "issues": ["missing required field: sources"],
        }
        scheduler.jobs = [upstream]

        scheduled = scheduler._schedule_repair(
            upstream,
            "schema_validation_failed: missing required field: sources",
            normalize_retry_policy(upstream.metadata["retryPolicy"]),
        )

        self.assertFalse(scheduled)
        repair_job = scheduler.jobs[-1]
        self.assertEqual("repair", repair_job.metadata["label"])
        self.assertIn("collect sources for the quarterly report", repair_job.prompt)
        self.assertEqual({"type": "object", "required": ["sources"]}, repair_job.metadata["options"]["schema"])
        self.assertEqual(["missing required field: sources"], repair_job.metadata["retryFeedback"]["issues"])

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

    def test_scheduler_accepts_protocol_runner_and_persists_child_transcript_metadata(self):
        runner = ProtocolMetadataRunner()
        scheduler, store, run = self.make_scheduler(runner=runner)
        job = scheduler.register_agent(prompt="do protocol work")

        scheduler.run_all()

        self.assertEqual([job.job_id], runner.started)
        self.assertEqual("agents/agent_1/transcript.jsonl", job.metadata["transcriptRef"])
        self.assertEqual({"input_tokens": 5, "output_tokens": 7}, job.metadata["tokenUsage"])
        self.assertEqual({}, job.metadata["toolSummary"])
        transcript_path = Path(run.artifact_dir) / "agents" / "agent_1" / "transcript.jsonl"
        self.assertTrue(transcript_path.exists())
        transcript_lines = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual("metadata", transcript_lines[0]["type"])
        self.assertEqual("assistant", transcript_lines[1]["type"])
        result_path = Path(run.artifact_dir) / job.result_ref
        result_data = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual("agents/agent_1/transcript.jsonl", result_data["transcriptRef"])
        self.assertEqual({"summary": "real-ish child result", "text": "verbose child transcript text"}, result_data["payload"])
        self.assertNotIn("transcriptEvents", result_data)
        completed_event = store.replay_events(run.run_id)[-1]
        self.assertEqual("agent_completed", completed_event.event_type)
        self.assertNotIn("transcriptEvents", completed_event.payload["result"])

    def test_permission_events_from_successful_agent_result_are_written_to_journal_before_completion(self):
        scheduler, store, run = self.make_scheduler(runner=PermissionEventsRunner())
        job = scheduler.register_agent(prompt="check permissions")

        scheduler.run_all()

        events = store.replay_events(run.run_id)
        self.assertEqual(
            ["agent_registered", "agent_started", "permission_profile_selected", "tool_allowed", "tool_denied", "agent_completed"],
            [event.event_type for event in events],
        )
        self.assertEqual(list(range(1, len(events) + 1)), [event.sequence for event in events])
        denied = events[4]
        self.assertEqual(job.job_id, denied.job_id)
        self.assertEqual("file_write", denied.payload["toolName"])
        self.assertEqual("read_only", denied.payload["profile"])
        self.assertEqual("deny", denied.payload["decision"])
        self.assertEqual("read_only_static_write_or_execute", denied.payload["reason"])
        self.assertEqual("deny", denied.payload["permission"]["action"])

    def test_permission_events_from_failed_agent_result_are_written_to_journal_before_failure(self):
        scheduler, store, run = self.make_scheduler(runner=PermissionEventsRunner(status="failed"))
        scheduler.register_agent(prompt="check permissions then fail")

        scheduler.run_all()

        events = store.replay_events(run.run_id)
        self.assertEqual("agent_failed", events[-1].event_type)
        self.assertEqual(
            ["permission_profile_selected", "tool_allowed", "tool_denied"],
            [event.event_type for event in events[2:5]],
        )
        self.assertEqual("file_write", events[4].payload["toolName"])

    def test_failed_agent_result_marks_job_failed_without_raising_or_killing_run(self):
        scheduler, store, run = self.make_scheduler(runner=FailedResultRunner())
        job = scheduler.register_agent(prompt="api may fail")

        scheduler.run_all(failure_policy="continue")

        self.assertEqual("failed", job.status)
        self.assertEqual("api down", job.error)
        self.assertEqual("running", run.status)
        self.assertEqual("agent_failed", self.event_types(store)[-1])
        failed_event = store.replay_events(run.run_id)[-1]
        self.assertNotIn("transcriptEvents", failed_event.payload["result"])
        transcript_path = Path(run.artifact_dir) / "agents" / "agent_1" / "transcript.jsonl"
        self.assertTrue(transcript_path.exists())

    def test_retry_policy_requeues_transient_child_failure_with_bounded_attempts(self):
        scheduler, store, run = self.make_scheduler(runner=SequenceResultRunner())
        job = scheduler.register_agent(
            prompt="retry transient provider",
            options={
                "retryPolicy": {
                    "maxAttempts": 2,
                    "retryableErrors": ["provider transient"],
                    "backoffMs": 0,
                }
            },
        )

        scheduler.run_all()

        loaded = store.load_run(run.run_id)
        self.assertEqual("succeeded", loaded.jobs[0].status)
        self.assertEqual(1, loaded.jobs[0].metadata["retryPolicy"]["attempts"] - 1)
        self.assertEqual("recovered", loaded.jobs[0].metadata["result"]["summary"])
        self.assertIn("agent_retry_scheduled", self.event_types(store))

    def test_completed_child_result_contains_compact_handoff_envelope(self):
        scheduler, store, run = self.make_scheduler()
        scheduler.register_agent(prompt="produce evidence")

        scheduler.run_all()

        loaded = store.load_run(run.run_id)
        handoff = loaded.jobs[0].metadata["handoff"]
        self.assertEqual("succeeded", handoff["status"])
        self.assertIn("summary", handoff)
        self.assertIn("evidence", handoff)
        self.assertEqual([], handoff["blockingIssues"])

    def test_dependent_job_receives_compact_handoff_not_upstream_payload(self):
        runner = LabelAwareRunner(
            results_by_label={
                "Researcher": {
                    "summary": "bounded conclusion",
                    "text": "x" * 100_000,
                    "artifacts": ["artifacts/research.json"],
                },
                "Writer": {"summary": "written"},
            }
        )
        scheduler, store, run = self.make_scheduler(runner=runner, max_concurrent=1)
        workspace = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(workspace, ignore_errors=True))
        scheduler.args = {"workspacePath": str(workspace)}
        scheduler._sync_workspace_metadata()
        scheduler.register_agent(prompt="research", label="Researcher")
        scheduler.register_agent(
            prompt="write",
            label="Writer",
            options={"dependsOn": ["Researcher"]},
        )

        scheduler.run_all()

        writer = store.load_run(run.run_id).jobs[1]
        handoff = writer.metadata.get("dependencyHandoff")
        self.assertEqual(1, len(handoff))
        self.assertEqual("bounded conclusion", handoff[0]["summary"])
        self.assertEqual(["artifacts/research.json"], handoff[0]["artifactRefs"])
        self.assertNotIn("x" * 10_000, str(handoff))
        self.assertEqual("workflow-handoffs/agent_1.json", handoff[0]["handoffRef"])
        handoff_path = workspace / handoff[0]["handoffRef"]
        self.assertTrue(handoff_path.exists())
        persisted = json.loads(handoff_path.read_text(encoding="utf-8"))
        self.assertEqual("bounded conclusion", persisted["summary"])
        self.assertNotIn("payload", persisted)
        downstream = scheduler.downstream_result(store.load_run(run.run_id).jobs[1])
        self.assertNotIn("x" * 10_000, json.dumps(downstream, ensure_ascii=False))

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

    def test_delegated_workflow_enforces_bounded_agent_cap(self):
        scheduler, store, _run = self.make_scheduler(
            max_total=10,
            run_kwargs={
                "metadata": {
                    "mode": "delegated",
                    "orchestration": {"delegationAllowed": True, "maxAgents": 2, "maxWaves": 2},
                }
            },
        )
        scheduler.register_agent(prompt="one")
        scheduler.register_agent(prompt="two")

        with self.assertRaisesRegex(RuntimeError, "workflow agent limit exceeded"):
            scheduler.register_agent(prompt="three")

        rejected = store.replay_events("wf_test")[-1]
        self.assertEqual("delegation_max_agents_exceeded", rejected.payload["reason"])

    def test_wave_scheduler_waits_for_declared_upstream_before_starting_job(self):
        runner = LabelAwareRunner()
        scheduler, store, run = self.make_scheduler(max_concurrent=4, runner=runner)
        upstream = scheduler.register_agent(prompt="collect", label="collect", options={"phase": "Collect"})
        downstream = scheduler.register_agent(
            prompt="synthesize",
            label="synthesize",
            options={"phase": "Synthesis", "dependsOn": ["collect"]},
        )

        self.assertEqual(1, upstream.metadata["wave"])
        self.assertEqual(2, downstream.metadata["wave"])
        self.assertEqual(["collect"], downstream.metadata["dependsOn"])

        scheduler.run_all()

        self.assertEqual("succeeded", upstream.status)
        self.assertEqual("succeeded", downstream.status)
        started_labels = [
            event.payload.get("label")
            for event in store.replay_events(run.run_id)
            if event.event_type == "agent_started"
        ]
        self.assertEqual(["collect", "synthesize"], started_labels)

    def test_wave_scheduler_skips_dependents_when_upstream_fails(self):
        runner = LabelAwareRunner(fail_labels={"collect"})
        scheduler, store, run = self.make_scheduler(max_concurrent=4, runner=runner)
        scheduler.register_agent(prompt="collect", label="collect", options={"phase": "Collect"})
        dependent = scheduler.register_agent(
            prompt="synthesize",
            label="synthesize",
            options={"phase": "Synthesis", "dependsOn": ["collect"]},
        )

        scheduler.run_all()

        loaded = store.load_run(run.run_id)
        self.assertEqual("failed", loaded.jobs[0].status)
        self.assertEqual("skipped", loaded.jobs[1].status)
        self.assertEqual("dependency_failed", loaded.jobs[1].metadata.get("skipReason"))

    def test_wave_scheduler_skips_dependents_when_upstream_is_stale(self):
        runner = LabelAwareRunner()
        scheduler, store, run = self.make_scheduler(max_concurrent=4, runner=runner)
        upstream = scheduler.register_agent(prompt="collect", label="collect", options={"phase": "Collect"})
        dependent = scheduler.register_agent(
            prompt="synthesize",
            label="synthesize",
            options={"phase": "Synthesis", "dependsOn": ["collect"]},
        )
        upstream.status = "stale"

        scheduler.tick()

        self.assertEqual("skipped", dependent.status)
        self.assertEqual("dependency_failed", dependent.metadata.get("skipReason"))
        started_labels = [
            event.payload.get("label")
            for event in store.replay_events(run.run_id)
            if event.event_type == "agent_started"
        ]
        self.assertNotIn("synthesize", started_labels)

    def test_wave_limit_rejects_job_that_exceeds_declared_max_waves(self):
        scheduler, store, _run = self.make_scheduler(
            max_total=10,
            run_kwargs={
                "metadata": {
                    "mode": "delegated",
                    "orchestration": {"delegationAllowed": True, "maxAgents": 5, "maxWaves": 1},
                }
            },
        )
        scheduler.register_agent(prompt="wave one", label="one")

        with self.assertRaisesRegex(RuntimeError, "workflow agent limit exceeded"):
            scheduler.register_agent(prompt="wave two", options={"dependsOn": ["one"]})

        rejected = store.replay_events("wf_test")[-1]
        self.assertEqual("delegation_max_waves_exceeded", rejected.payload["reason"])

    def test_retry_exhaustion_can_hand_off_to_bounded_repair_role(self):
        runner = LabelAwareRunner(fail_labels={"primary"})
        scheduler, store, run = self.make_scheduler(runner=runner)
        scheduler.register_agent(
            prompt="primary work",
            label="primary",
            options={
                "retryPolicy": {"maxAttempts": 1, "retryableErrors": ["child failed"], "repairRole": "repair"},
            },
        )

        scheduler.run_all()

        loaded = store.load_run(run.run_id)
        self.assertEqual(2, len(loaded.jobs))
        self.assertEqual("repair", loaded.jobs[1].metadata["label"])
        self.assertEqual("repair", loaded.jobs[1].metadata["repairRole"])
        self.assertEqual(loaded.jobs[0].job_id, loaded.jobs[1].metadata["repairOf"])
        self.assertIn("agent_repair_scheduled", self.event_types(store))

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
        self.assertEqual("partial", scheduler.run.metadata.get("executionOutcome"))

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
