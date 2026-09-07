"""Pure route selection: visits are not analyses; a wake is not no new evidence."""
from math import isfinite

from app.v3.shared_desk import PhaseOutcome


def select_tier(hours_old: float, news_count: int | None, *, deep_hours: float,
                deep_news_volume: int, glance_hours: float,
                prior_contradictions: int = 0, trigger_type: str = 'manual',
                force_full: bool = False) -> str:
    if force_full or news_count is None or hours_old >= deep_hours or news_count >= deep_news_volume:
        return 'v3_deep'
    if prior_contradictions > 0 and hours_old > glance_hours / 8:
        return 'v3_deep'
    material_wake = trigger_type not in ('manual', 'scheduled', 'staleness', '')
    if hours_old <= glance_hours and news_count == 0 and not material_wake:
        return 'v3_glance'
    return 'v3_delta'


def delta_needs_panel(delta: dict, outcome: PhaseOutcome) -> bool:
    """Only a valid agent HOLD may finish before the full policy prerequisites.

    BUY and SELL need fresh regime/research authorization. Escalating them
    preserves every policy gate instead of inventing a missing regime artifact.
    """
    if not isinstance(delta, dict) or outcome != PhaseOutcome.SUCCESS:
        return True
    confidence = delta.get('confidence')
    return (delta.get('escalate') is not False
            or delta.get('verdict') not in ('REAFFIRM', 'ADJUST')
            or str(delta.get('action') or '').upper() != 'HOLD'
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not isfinite(confidence)
            or not 0 < confidence <= 100)
