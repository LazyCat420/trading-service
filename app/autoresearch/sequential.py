"""Anytime-valid sequential testing for champion/challenger comparisons.

THE PROBLEM: classical significance tests assume the sample size was fixed in
advance. Peeking at results as they arrive and stopping "when it looks
significant" inflates the false-positive rate — but with trading outcomes
resolving on a 7-day drip, never peeking is operationally absurd.

E-processes solve this. The running e-value is a measure of evidence against
the null that is valid at EVERY point in time: you may look after every single
resolved pair and stop the moment the threshold is crossed, and the error
guarantee still holds (Ville's inequality: P(sup E >= 1/alpha) <= alpha under
the null). Evidence thresholds:

    e >= 20   reject at alpha = 0.05
    e >= 100  reject at alpha = 0.01
    e <  1    evidence currently FAVOURS the null

The test here is the paired-disagreement sign test: among resolved pairs where
champion and challenger disagreed, count pairs the challenger "won" (its call
was correct, the champion's was not) vs pairs it lost. Ties — both correct or
both incorrect — carry no information about which is better and are excluded.
Under the null (no difference), wins ~ Bernoulli(0.5). The e-value is the
Beta(1/2,1/2)-mixture likelihood ratio against that null — the standard
universal test for a coin, optimal-ish without choosing an alternative.
"""

from __future__ import annotations

import math
from typing import Iterable


def eprocess_bernoulli(wins: int, losses: int) -> float:
    """E-value against H0: p = 0.5 for `wins` successes in wins+losses trials.

    Beta(1/2,1/2) (Jeffreys) mixture over the alternative:

        E = Beta(wins + 1/2, losses + 1/2) / Beta(1/2, 1/2) / 0.5**n

    Computed in log space; overflow-safe for any realistic n.
    """
    if wins < 0 or losses < 0:
        raise ValueError("counts must be non-negative")
    n = wins + losses
    if n == 0:
        return 1.0
    log_e = (
        _lbeta(wins + 0.5, losses + 0.5)
        - _lbeta(0.5, 0.5)
        + n * math.log(2.0)
    )
    # exp() overflows past ~709; any such e-value is decisive regardless.
    return math.exp(log_e) if log_e < 700 else math.inf


def _lbeta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def paired_disagreement_test(pairs: Iterable[tuple[bool, bool]]) -> dict:
    """Run the sign test over (champion_correct, challenger_correct) pairs.

    Pairs where both were right or both were wrong are ties: informative about
    the market, not about the difference between the two systems.
    """
    champion_wins = 0
    challenger_wins = 0
    ties = 0
    for champ_ok, chall_ok in pairs:
        if champ_ok == chall_ok:
            ties += 1
        elif chall_ok:
            challenger_wins += 1
        else:
            champion_wins += 1

    e_value = eprocess_bernoulli(challenger_wins, champion_wins)
    informative = challenger_wins + champion_wins

    if e_value >= 100:
        verdict = "strong evidence (alpha 0.01)"
    elif e_value >= 20:
        verdict = "significant (alpha 0.05)"
    elif e_value >= 5:
        verdict = "suggestive — keep collecting"
    elif e_value < 1 and informative >= 5:
        verdict = "evidence favours no-difference"
    else:
        verdict = "insufficient evidence"

    direction = None
    if informative:
        direction = "challenger" if challenger_wins > champion_wins else (
            "champion" if champion_wins > challenger_wins else "even"
        )

    return {
        "champion_wins": champion_wins,
        "challenger_wins": challenger_wins,
        "ties": ties,
        "informative_pairs": informative,
        "e_value": round(e_value, 3),
        "anytime_valid": True,
        "leader": direction,
        "verdict": verdict,
    }
