from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

@dataclass
class TelemetryEvent:
    ts: str  # ISO timestamp
    cycle_id: str
    ticker: str
    kind: str  # e.g., "llm", "pipeline", "tool", "heartbeat"
    source: str  # e.g., "prism", "local_fallback", "cycle_runner"
    status: str  # "ok", "error", "running"
    step: str  # e.g., "PRISM_AGENT_START", "TOOL_CALL", "PRISM_AGENT_END", "PHASE_CHANGE"
    detail: str
    phase: str = ""
    elapsed_ms: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
