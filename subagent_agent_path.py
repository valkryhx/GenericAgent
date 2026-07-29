import re
from dataclasses import dataclass


_SEGMENT_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class AgentPath:
    segments: tuple[str, ...]

    @classmethod
    def root(cls):
        return cls(("root",))

    @classmethod
    def parse(cls, value):
        text = str(value or "").strip()
        if text != "/root" and (not text.startswith("/root/") or text.endswith("/")):
            raise ValueError("agent_path must be /root or a canonical /root/<name> path")
        parts = tuple(text.strip("/").split("/"))
        if not parts or parts[0] != "root":
            raise ValueError("agent_path must start with /root")
        for segment in parts[1:]:
            _validate_segment(segment)
        return cls(parts)

    @property
    def is_root(self):
        return self.segments == ("root",)

    @property
    def name(self):
        return self.segments[-1]

    @property
    def parent(self):
        if self.is_root:
            return None
        return AgentPath(self.segments[:-1])

    def join(self, child_name):
        child_name = str(child_name or "").strip()
        _validate_segment(child_name)
        return AgentPath(self.segments + (child_name,))

    def resolve(self, reference):
        text = str(reference or "").strip()
        if text.startswith("/"):
            return AgentPath.parse(text)
        if "/" in text or "\\" in text:
            raise ValueError("relative agent reference must be a direct child name")
        return self.join(text)

    def __str__(self):
        return "/" + "/".join(self.segments)


def _validate_segment(segment):
    if not segment or segment in {".", ".."} or not _SEGMENT_RE.fullmatch(segment):
        raise ValueError("agent path segment must contain lowercase letters, digits, and underscores")
