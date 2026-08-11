"""Reconcile the two records every decision leaves behind.

`DECISION_INTEGRITY_PLAN.md` §3 rule 3: *"Two records of the same decision must
reconcile, or the mismatch is an alert."* Both halves of that check were built
correctly in `scripts/verify_audit_phases.py` and **neither had a caller** — no
cron, no router, no pipeline hook. It ran on the days somebody remembered to
type the command, and a check nobody runs is documentation, not a control. The
divergence it was written for went unnoticed for 19 days.

This module is that check, extracted so the CLI and the runtime share one
implementation. It is deliberately NOT copied into a second place: a check that
reimplements what it verifies cannot see the real thing drift.

**It records; it does not page and it does not block.** Open item 26 warns that
production has been ahead of the deployed code before — a parallel session once
ran a 69-row backfill by hand — so until it is known what can write
`decision_outcomes` from outside the service, the first mismatch may well be a
person rather than a bug. `logger.warning` is durable here: `DbLoggingHandler`
persists warnings to `execution_errors`, so the evidence accumulates without
inventing a table or crying wolf at ERROR.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationResult:
    """What the two stores said, and where they disagreed."""

    cycle_id: str
    desks_seen: int = 0
    saved_rows: int = 0
    action_mismatches: list[str] = field(default_factory=list)
    provenance_mismatches: list[str] = field(default_factory=list)
    rows_with_provenance: int = 0
    error: str = ""

    @property
    def checked(self) -> bool:
        """False when there was nothing to compare — an empty result is not a pass."""
        return not self.error and (self.desks_seen > 0 or self.saved_rows > 0)

    @property
    def reconciled(self) -> bool:
        return self.checked and not self.action_mismatches and not self.provenance_mismatches

    def summary(self) -> str:
        if self.error:
            return f"not checked: {self.error}"
        if not self.checked:
            return "nothing to compare (no desks and no trade_results rows)"
        bits = [f"{self.saved_rows} saved rows vs {self.desks_seen} desks"]
        if self.action_mismatches:
            bits.append(f"ACTION MISMATCH: {self.action_mismatches}")
        if self.provenance_mismatches:
            bits.append(f"PROVENANCE MISMATCH: {self.provenance_mismatches}")
        if not self.action_mismatches and not self.provenance_mismatches:
            bits.append("all agree")
        return "; ".join(bits)


def reconcile_cycle(cycle_id: str, desks: dict[str, dict] | None = None) -> ReconciliationResult:
    """Compare `shared_desk` against `trade_results` for one cycle.

    `desks` may be supplied by a caller that already loaded them (the CLI does);
    otherwise they are read here. Never raises — a reconciliation failure must
    not be able to break the thing it is watching.
    """
    result = ReconciliationResult(cycle_id=cycle_id)
    try:
        from app.db.connection import get_db

        with get_db() as db:
            if desks is None:
                rows = db.execute(
                    "SELECT ticker, desk_data FROM shared_desk WHERE cycle_id = %s",
                    [cycle_id],
                ).fetchall()
                desks = {r[0]: (r[1] or {}) for r in rows}

            tr_rows = db.execute(
                "SELECT ticker, action, decision_provenance FROM trade_results "
                "WHERE cycle_id = %s",
                [cycle_id],
            ).fetchall()
    except Exception as e:  # noqa: BLE001 — an observer must never break a cycle
        result.error = str(e)[:200]
        return result

    tr = {r[0]: {"action": r[1], "provenance": r[2]} for r in tr_rows}
    result.desks_seen = len(desks)
    result.saved_rows = len(tr)

    for ticker, desk in desks.items():
        art = desk.get("trade_decision") or desk.get("final_decision") or {}
        desk_act = art.get("action")
        saved = tr.get(ticker)

        if saved is None and desk_act:
            result.action_mismatches.append(f"{ticker}: desk={desk_act} but NO trade_results row")
        elif saved and not desk_act:
            result.action_mismatches.append(
                f"{ticker}: trade_results={saved['action']} but desk has no action"
            )
        elif saved and desk_act and str(saved["action"]).upper() != str(desk_act).upper():
            result.action_mismatches.append(
                f"{ticker}: desk={desk_act} != trade_results={saved['action']}"
            )

        # Comparing only the ACTION let the two stores disagree about whether an
        # agent decided at all — exactly the laundering decision_provenance
        # exists to stop.
        desk_prov = art.get("decision_provenance")
        if saved and desk_prov and saved["provenance"] != desk_prov:
            result.provenance_mismatches.append(
                f"{ticker}: desk={desk_prov} != trade_results={saved['provenance']}"
            )

    result.rows_with_provenance = sum(1 for t in tr if tr[t]["provenance"])
    return result


def reconcile_and_report(cycle_id: str) -> ReconciliationResult:
    """Run the check at the end of a cycle and record whatever it finds.

    The runtime entry point. Warning-level on purpose — see the module
    docstring: this builds an evidence trail rather than paging someone about
    what may be a human with a psql prompt.
    """
    result = reconcile_cycle(cycle_id)

    if result.error:
        logger.warning("[Reconcile] %s: check did not run — %s", cycle_id, result.error)
        return result
    if not result.checked:
        # An empty comparison is the absence of evidence, not evidence of
        # agreement, and must never read as a pass.
        logger.warning(
            "[Reconcile] %s: nothing to compare — no shared_desk rows and no "
            "trade_results rows. This is NOT a clean reconciliation.", cycle_id,
        )
        return result
    if result.reconciled:
        logger.info("[Reconcile] %s: %s", cycle_id, result.summary())
        return result

    logger.warning(
        "[Reconcile] %s: the two records of this cycle's decisions DISAGREE — %s. "
        "Check whether anything wrote decision_outcomes/trade_results from "
        "outside the service before treating this as a code defect.",
        cycle_id, result.summary(),
    )
    return result
