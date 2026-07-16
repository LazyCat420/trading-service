"""
Board Quality Telemetry (plan item B6).

A read-only, data-driven map of WHERE board decision quality is falling, so fixes
target real problems instead of guesses. Prints:

  1. Per-ticker mean quality        (decision_evaluations.final_quality_score)
  2. Per-regime mean quality        (joined to trade_results.regime)
  3. Failure-reason distribution    (evidence_gathering->>'failure_reason')
  4. Zero-score rate                (red-card / precheck hard-zeros)
  5. Regime → persona routing        (trade_results: is it collapsing to one?)
  6. H2H tournament persona win rate (debate_history persona_outcomes)

Storage notes (verified against migrations.py @ 79f39a6):
  - decision_evaluations: final_quality_score is a top column; failure_reason is
    NOT — it lives inside the evidence_gathering JSON string as 'failure_reason'.
  - regime / persona_used live on trade_results, keyed (ticker, cycle_id).
  - tournament debates land in debate_history with persona_name='tournament',
    winner in {'bull','bear'}, and pro/con_argument JSON carrying the persona.

Usage:
    DATABASE_URL=postgres://... python scripts/board_quality_telemetry.py [DAYS]
    (DAYS = lookback window, default 14)
"""
import os
import sys
import psycopg2

url = os.environ["DATABASE_URL"]
days = int(sys.argv[1]) if len(sys.argv) > 1 else 14

conn = psycopg2.connect(url)
cur = conn.cursor()


def q(title, sql, params=None):
    print(f"\n--- {title} ---")
    try:
        cur.execute(sql, params or {})
        if cur.description:
            cols = [d[0] for d in cur.description]
            print(" | ".join(cols))
            rows = cur.fetchall()
            if not rows:
                print("(no rows)")
            for row in rows:
                print(" | ".join("" if v is None else str(v) for v in row))
        else:
            print("Executed.")
    except Exception as e:
        print(f"Error: {e}")
        conn.rollback()


WINDOW = "timestamp > NOW() - INTERVAL '%s days'" % days

print(f"=== Board Quality Telemetry — last {days} days ===")

# 1. Per-ticker mean quality (0-5 scale). Lowest first — worst offenders on top.
q(
    "1. Per-ticker mean quality",
    f"""
    SELECT ticker,
           COUNT(*)                         AS n,
           ROUND(AVG(final_quality_score)::numeric, 2) AS mean_q,
           ROUND(MIN(final_quality_score)::numeric, 2) AS min_q,
           ROUND(MAX(final_quality_score)::numeric, 2) AS max_q
    FROM decision_evaluations
    WHERE {WINDOW}
    GROUP BY ticker
    ORDER BY mean_q ASC NULLS FIRST
    LIMIT 40;
    """,
)

# 2. Per-regime mean quality — is a particular regime (e.g. CONTRADICTORY)
#    systematically scoring lower? Joins the score to the regime the board ran in.
q(
    "2. Per-regime mean quality",
    f"""
    SELECT COALESCE(tr.regime, '(unknown)')    AS regime,
           COUNT(*)                            AS n,
           ROUND(AVG(de.final_quality_score)::numeric, 2) AS mean_q
    FROM decision_evaluations de
    LEFT JOIN trade_results tr
           ON tr.ticker = de.ticker AND tr.cycle_id = de.cycle_id
    WHERE de.{WINDOW}
    GROUP BY tr.regime
    ORDER BY mean_q ASC NULLS FIRST;
    """,
)

# 3. Failure-reason distribution. failure_reason is stored INSIDE the
#    evidence_gathering JSON, not as a column (see judge_agent.py).
q(
    "3. Failure-reason distribution",
    f"""
    SELECT COALESCE((evidence_gathering::jsonb) ->> 'failure_reason', 'none') AS failure_reason,
           COUNT(*) AS n,
           ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct,
           ROUND(AVG(final_quality_score)::numeric, 2) AS mean_q
    FROM decision_evaluations
    WHERE {WINDOW}
    GROUP BY 1
    ORDER BY n DESC;
    """,
)

# 4. Zero-score rate — how often the final score is hard-zeroed (red card /
#    precheck). A high rate means the SCORING path, not the board, caps quality.
q(
    "4. Zero-score rate",
    f"""
    SELECT COUNT(*)                                             AS total,
           COUNT(*) FILTER (WHERE final_quality_score = 0)      AS zeroed,
           ROUND(100.0 * COUNT(*) FILTER (WHERE final_quality_score = 0)
                 / NULLIF(COUNT(*), 0), 1)                      AS zero_pct
    FROM decision_evaluations
    WHERE {WINDOW};
    """,
)

# 5. Regime → persona routing distribution. B3 audit predicted this collapses
#    onto Jane Street/CONTRADICTORY — confirm it with data.
q(
    "5. Regime -> persona routing",
    """
    SELECT COALESCE(regime, '(none)')       AS regime,
           COALESCE(persona_used, '(none)') AS persona_used,
           COUNT(*)                         AS n
    FROM trade_results
    WHERE created_at > NOW() - INTERVAL '%s days'
    GROUP BY regime, persona_used
    ORDER BY n DESC;
    """,
    (days,),
)

# 6. H2H tournament persona win rate. Tournament rows use winner in {bull,bear};
#    the winning persona is pro_argument.persona (bull) or con_argument.persona
#    (bear). Shows which quant lens actually wins the final bracket.
q(
    "6. H2H tournament persona win rate",
    """
    WITH t AS (
        SELECT CASE WHEN winner = 'bull'
                    THEN (pro_argument::jsonb) ->> 'persona'
                    ELSE (con_argument::jsonb) ->> 'persona'
               END AS winning_persona
        FROM debate_history
        WHERE persona_name = 'tournament'
          AND created_at > NOW() - INTERVAL '%s days'
          AND winner IN ('bull', 'bear')
    )
    SELECT COALESCE(winning_persona, '(unparsed)') AS winning_persona,
           COUNT(*) AS wins,
           ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
    FROM t
    GROUP BY winning_persona
    ORDER BY wins DESC;
    """,
    (days,),
)

cur.close()
conn.close()
print("\n=== done ===")
