"""A running cycle reports `running`, and its clock starts when it starts.

Measured on cycle-v3-1788479270 (2026-09-03): the news scrape ran from
23:47:51 to 23:59:23 — 11m32s, the longest single stretch of the cycle — and
for all of it `/status` returned `status="starting"`. `discovery_emit` updated
`progress` and `phase` and never touched `status`, which stayed as dispatch
left it until the gatekeeper finished.

The consequences were all downstream of that one field:

  * the control bar renders `isStarting` before `isRunning`, so it showed
    "Starting…" and offered no Stop button for eleven minutes;
  * the client's staleness threshold is 120 s, so a normal scrape looked stale;
  * `started_at` was first written in the SAME post-gatekeeper block, so the
    elapsed clock began after the scrape and every duration was short by it;
  * `cycle_benchmarks.collect_ms` derived durations for a phase named
    `collecting` that nothing has ever emitted — the scrape emits `discovery` —
    so that column is NULL on every row ever written.
"""
import ast
import inspect

from app.services.pipeline_service import PipelineService


def _source():
    return inspect.getsource(PipelineService._run_all_v3)


class TestStatusDuringTheScrape:
    def test_discovery_emit_reports_running(self):
        """The state update inside discovery_emit must set status=running."""
        src = _source()
        i = src.index("def discovery_emit(")
        j = src.index("async def run_scraper_sync", i)
        block = src[i:j]
        assert '"status": "running"' in block, (
            "the scrape phase must report a RUNNING cycle; leaving status at "
            "'starting' is what hid the Stop button for 11m32s"
        )

    def test_the_phase_still_names_the_scrape(self):
        src = _source()
        i = src.index("def discovery_emit(")
        j = src.index("async def run_scraper_sync", i)
        assert '"phase": "discovery"' in src[i:j], (
            "status says WHETHER it is running; phase says WHAT is running. "
            "Do not conflate them."
        )


class TestTheClock:
    def test_started_at_is_stamped_at_dispatch(self):
        """Not in the post-gatekeeper block, which was its only writer."""
        src = inspect.getsource(PipelineService.run_full_cycle_v3) \
            if hasattr(PipelineService, "run_full_cycle_v3") else _source()
        full = inspect.getsource(PipelineService)
        i = full.index('"status": "starting"')
        window = full[i - 200:i + 600]
        assert '"started_at"' in window, (
            "the cycle's clock must start when the cycle starts"
        )

    def test_the_gatekeeper_block_does_not_restart_the_clock(self):
        full = inspect.getsource(PipelineService)
        i = full.index('# Set status to running now that gatekeeper is done')
        window = full[i:i + 800]
        assert 'cls._state.get("started_at") or' in window, (
            "re-stamping started_at here made the elapsed timer jump back to "
            "zero after the gatekeeper, hiding the scrape from every duration"
        )


class TestCollectMs:
    def test_the_derivation_reads_the_phase_the_scrape_emits(self):
        """`collecting` is emitted by nothing; the scrape emits `discovery`."""
        full = inspect.getsource(PipelineService)
        i = full.index("_PHASE_SOURCE")
        block = full[i:i + 400]
        assert '"discovery"' in block and '"gatekeeper"' in block, (
            "collect_ms must cover the phases the collection stage actually "
            "emits, or it stays NULL forever as it has on every row to date"
        )

    def test_the_phase_map_is_a_partition(self):
        """Every source phase maps to exactly one benchmark column."""
        full = inspect.getsource(PipelineService)
        i = full.index("_PHASE_SOURCE = ")
        j = full.index("}", full.index("{", i)) + 1
        mapping = ast.literal_eval(full[full.index("{", i):j])
        seen = [src for srcs in mapping.values() for src in srcs]
        assert len(seen) == len(set(seen)), f"a phase is counted twice: {seen}"
        assert set(mapping) == {"collecting", "analyzing", "trading"}
