"""parse_vllm_metrics: exact-name matching against vLLM /metrics.

The incident this file pins (2026-08-09): the old loop matched metric lines
with startswith(), and vLLM's `num_requests_waiting_by_reason` family shares
the `num_requests_waiting` prefix. The LAST family line parsed —
reason="deferred", 0.0 in steady state — overwrote the true queue depth on
every poll. The live box had waiting=17 while the controller read 0, so the
AdaptiveConcurrencyController's backpressure clamp never fired, requests
queued past prism's 300s stall watchdog, and the 02:14 pile-up preceded an
80-minute box outage.

Reverting parse_vllm_metrics to prefix matching turns LIVE_PAYLOAD red here —
that revert is the mutation these tests exist to catch.
"""

from app.services.prism_agent_caller import parse_vllm_metrics

# Verbatim from Gold Spark, 2026-08-09 ~18:10 UTC, while the desk stalled.
LIVE_PAYLOAD = """\
vllm:num_requests_running{engine="0",model_name="deepseek-v4-flash-0731"} 6.0
vllm:num_requests_waiting{engine="0",model_name="deepseek-v4-flash-0731"} 17.0
vllm:num_requests_waiting_by_reason{engine="0",model_name="deepseek-v4-flash-0731",reason="capacity"} 17.0
vllm:num_requests_waiting_by_reason{engine="0",model_name="deepseek-v4-flash-0731",reason="deferred"} 0.0
vllm:kv_cache_usage_perc{engine="0",model_name="deepseek-v4-flash-0731"} 0.05869872701555867
"""


def test_live_payload_reads_the_true_queue_depth():
    """The exact five lines from the incident: waiting must be 17, not the
    0.0 that the trailing by_reason{deferred} line used to leave behind."""
    out = parse_vllm_metrics(LIVE_PAYLOAD)
    assert out["requests_waiting"] == 17
    assert out["requests_running"] == 6
    assert abs(out["cache_usage"] - 0.0587) < 0.001


def test_line_order_must_not_matter():
    """The defect was order-dependent (last matching line won). Feed the same
    lines reversed and demand the same answer."""
    reversed_payload = "\n".join(reversed(LIVE_PAYLOAD.strip().splitlines()))
    out = parse_vllm_metrics(reversed_payload)
    assert out["requests_waiting"] == 17
    assert out["requests_running"] == 6


def test_by_reason_lines_are_ignored_entirely():
    """A payload containing ONLY the family lines must set nothing — the
    family metric is not the metric."""
    only_family = (
        'vllm:num_requests_waiting_by_reason{reason="capacity"} 17.0\n'
        'vllm:num_requests_waiting_by_reason{reason="deferred"} 0.0\n'
    )
    assert "requests_waiting" not in parse_vllm_metrics(only_family)


def test_comments_blanks_and_junk_values_are_skipped():
    payload = (
        "# HELP vllm:num_requests_waiting Number of requests waiting.\n"
        "\n"
        "vllm:num_requests_waiting{engine=\"0\"} not-a-number\n"
        "vllm:num_requests_running{engine=\"0\"} 3.0\n"
    )
    out = parse_vllm_metrics(payload)
    assert out == {"requests_running": 3}


def test_underscore_spelled_names_still_parse():
    """Older vLLM builds emit vllm_ instead of vllm: — both spellings are in
    the map and both must match exactly."""
    out = parse_vllm_metrics("vllm_num_requests_waiting 4\n")
    assert out["requests_waiting"] == 4
