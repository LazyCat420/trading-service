"""Immutable observed failures: the old expression audit did not see these errors."""
import hashlib
import json
from pathlib import Path
import pytest
from app.v3.arithmetic_audit import audit_artifact

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'docs/benchmarks/evidence/board-memory-proxy-2026-09-11'
FIXTURE = ROOT / 'tests/benchmarks/fixtures/financial_reasoning_v1.json'


def test_source_transcription_tracks_the_unchanged_original_corpus():
    fixture = json.loads(FIXTURE.read_text())
    original = FIXTURE.with_name(fixture['source_fixture'])
    assert hashlib.sha256(original.read_bytes()).hexdigest() == fixture['source_sha256']
    assert len(fixture['cases']) == 4
    assert all('expected' not in question for case in fixture['cases'] for question in case['questions'])
    cases = {c['id']: {f['id']: f for f in c['facts']} for c in fixture['cases']}
    assert cases['missing_history']['rsi_14']['value'] == 46
    assert cases['missing_history']['volume_five_session_trend']['as_of'] == '2026-08-17'
    assert cases['missing_history']['eps_growth_next_year_pct']['value'] is None
    assert cases['held_deterioration']['operating_margin_pct']['value'] == -2
    assert cases['held_deterioration']['free_cash_flow']['value'] == -40000000
    assert cases['held_deterioration']['debt_to_equity_prior']['value'] is None
    assert cases['conditional_entry']['proposed_entry']['period'] == 'hypothetical'


@pytest.mark.parametrize('index', range(1, 13))
def test_old_expression_audit_has_no_coverage_of_frozen_financial_claims(index):
    row = json.loads(next(EVIDENCE.glob(f'{index:02}-*.json')).read_text())
    report = audit_artifact(dict(row['artifact']))
    assert report['checked'] == 0  # a coverage gap, never a financial pass
