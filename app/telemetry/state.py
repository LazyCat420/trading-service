from dataclasses import dataclass, field, asdict
from typing import Any

@dataclass
class CycleState:
    cycle_id: str
    status: str = "idle"
    phase: str = ""
    progress: str = ""
    tickers: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    llm_stats: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
