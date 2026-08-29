"""START_CYCLE has exactly one writer.

Five producers can start a cycle -- schedule cadence, market-open job, Watch
Desk wake, research governor, UI -- and each carried its own copy of the same
insert. The copies agreed, which is precisely what made them dangerous: the
QUEUE NAME is the contract with the worker, and a copy-pasted writer is how a
producer ends up addressing a queue nothing drains.

That is not hypothetical. trading-client writes its worker commands to
`system_commands`; cycle_main drains `v3_system_commands`. Its schedule
refresh, analyze-ticker and sector-collection commands have been dispatching
into a void.
"""
import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"


def _service_sources():
    for p in sorted(_APP.rglob("*.py")):
        if "scraper" in p.parts:      # build-copy for scraper-service
            continue
        yield p, p.read_text()


def test_only_cycle_queue_inserts_a_start_cycle_command():
    offenders = []
    for path, src in _service_sources():
        if path.name == "cycle_queue.py":
            continue
        for m in re.finditer(r'insert_docs\(\s*[\'"]v3_system_commands[\'"]', src):
            line = src[:m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(_APP.parent)}:{line}")
    assert not offenders, (
        "a START_CYCLE insert reappeared outside app/services/cycle_queue.py: "
        + ", ".join(offenders)
        + ". Use enqueue_start_cycle() so the queue name has one owner."
    )


def test_the_helper_targets_the_queue_the_worker_actually_drains():
    from app.services.cycle_queue import COMMAND_COLLECTION

    worker = (_APP.parent / "cycle_main.py").read_text()
    assert COMMAND_COLLECTION in worker, (
        f"cycle_queue enqueues onto {COMMAND_COLLECTION!r}, which cycle_main "
        "never reads — commands would queue forever"
    )
    assert COMMAND_COLLECTION == "v3_system_commands"


def test_every_producer_passes_a_distinct_prefix():
    """The id prefix is what traces a queued cycle back to whatever started it."""
    prefixes = []
    for path, src in _service_sources():
        prefixes += re.findall(r'enqueue_start_cycle\([^)]*prefix=["\']([^"\']+)["\']', src)
    assert prefixes, "no producer calls enqueue_start_cycle — re-point this guard"
    assert len(prefixes) == len(set(prefixes)), (
        f"two producers share a command-id prefix: {sorted(prefixes)}"
    )
