from __future__ import annotations

from pathlib import Path

from subagent_state import atomic_write_json, now_iso, read_json_or_none, sha256_file


class SubagentArtifactStore:
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.manifest_path = self.run_dir / "artifacts.json"

    def record_final_output(self, output_path, *, round_no):
        output_path = Path(output_path)
        artifact_id = f"final_output_round_{int(round_no)}"
        artifact = {
            "artifact_id": artifact_id,
            "type": "final_output",
            "path": str(output_path),
            "sha256": sha256_file(output_path),
            "bytes": output_path.stat().st_size,
            "round": int(round_no),
            "created_at": now_iso(),
        }
        manifest = read_json_or_none(self.manifest_path) or {"schema_version": 1, "artifacts": []}
        artifacts = [item for item in manifest.get("artifacts", []) if item.get("artifact_id") != artifact_id]
        artifacts.append(artifact)
        manifest["artifacts"] = artifacts
        atomic_write_json(self.manifest_path, manifest)
        return artifact

    def get(self, artifact_id):
        manifest = read_json_or_none(self.manifest_path) or {}
        for artifact in manifest.get("artifacts", []) or []:
            if artifact.get("artifact_id") == artifact_id:
                return artifact
        raise FileNotFoundError(str(artifact_id))
