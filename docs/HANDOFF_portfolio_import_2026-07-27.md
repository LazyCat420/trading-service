# HANDOFF — Portfolio import: seed a profile with real holdings (2026-07-27)

Shipped `dcdd2ba`, **deployed** to `synology` 2026-07-27 23:50Z, container
`Up (healthy)` confirmed via `docker ps` after the deploy. Verified live
end-to-end against a throwaway profile (`import-smoke-test`, created and
deleted; **no existing profile was touched**).

Pairs with **trading-client `0afce8f`**, which owns the file parsing and the UI.
Deploy this repo FIRST when both change — the client calls the endpoint added
here.

**⚠ Carried forward from the previous wave and still unrun:** the skill-gate /
data-collection post-deploy checklist. It now lives in
[`docs/HANDOFF_skill_gate_2026-07-27.md`](docs/HANDOFF_skill_gate_2026-07-27.md)
§"Verify next cycle" — that work is unaffected by this wave, but nobody has run
the checks yet.

Previous handoffs archived:
[`docs/HANDOFF_skill_gate_2026-07-27.md`](docs/HANDOFF_skill_gate_2026-07-27.md) ·
[`docs/HANDOFF_data_collection_audit_2026-07-27.md`](docs/HANDOFF_data_collection_audit_2026-07-27.md) ·
[`docs/HANDOFF_coral_repair_loop_2026-07-27.md`](docs/HANDOFF_coral_repair_loop_2026-07-27.md).

---

## What this wave was

Every bot profile had to start flat at its starting cash. There was no way to
hand the bot an existing book and say "manage this". `bot_manager.import_positions()`
plus `POST /api/v1/bot/profiles/{bot_id}/import` is that path.

## What is live

`app/services/bot_manager.py::import_positions(bot_id, positions, cash, mode,
set_starting_cash)`, exposed at `POST /api/v1/bot/profiles/{bot_id}/import`.
`mode` is `replace` (wipe the profile's positions/orders/fills/lots/closures/
snapshots first) or `merge` (share-weighted average into what is there).

Each imported holding writes **three** rows, not one:

- `positions` — qty, `avg_entry_price` = real cost basis, `stop_source='imported'`
- `trade_fills` — a synthetic BUY, `source='import'`, zero fees
- `position_lots` — one open lot, `is_legacy=TRUE`

**The lot is not optional.** Without it the first SELL has nothing to close
against and lot-level realized P&L is wrong for the life of the profile. The
schema already had `is_legacy` for exactly this case.

## Three decisions that are easy to get wrong later

**Entry price is the REAL cost basis** (the user chose this over rebasing to
import-day price). It keeps P&L honest and makes stops relative to a price that
may be years old, which cuts both ways: a long-held winner's stop sits far below
the market, and **a long-held loser is already through its stop the moment it
lands**.

So — **imported positions get `exit_style='reanalyze_on_breach'`.** With the
default `hard_stop`, importing an underwater book would have the background
monitor liquidate every position past its stop on the first pass after import.
`check_stop_losses()` skips `reanalyze_on_breach` positions and hands the breach
to the agent instead. Do not "normalise" imported positions to `hard_stop`.

**The ATR stop is deliberately NOT used.** `_compute_stop_loss_pct` is
`ATR*k / entry_price`; against an entry price from 2019 that produces a
sub-1% stop on a 10x winner. Imports use the asset-class default from
`_STOP_BOUNDS` (see `_default_stop_pct`) unless the file supplies a per-row
`stop_loss_pct`.

**`total_pnl` and `total_trades` are left alone.** An import is not a trade the
bot made and the scorecard must not count it as one. Only `cash_balance` and
(optionally) `starting_cash` move. `starting_cash` becomes `cash + total cost
basis` — the capital actually put in — so equity-vs-starting reads as true
lifetime return.

## Gotchas

- **Blocked while a cycle is running**, same as reset/delete. It wipes and
  rewrites the same tables a cycle is mid-way through using.
- **`replace` mode deletes `portfolio_snapshots` too**, which resets the
  drawdown breaker's peak reference for that profile. Same behaviour as
  `reset_bot_profile`; intentional, but it means the breaker starts fresh.
- **File parsing is NOT here.** It lives in trading-client
  (`app/services/portfolio_import.py`) because it is pure CPU on a small upload
  and stays responsive mid-cycle, when this service's event loop stalls for
  seconds. This endpoint takes already-normalised JSON. Do not move the parser
  in "for consistency".
- The endpoint re-validates positive quantity and positive cost per row and
  raises `ValueError` → 400. The transaction is all-or-nothing.

## Still open

- **No dedupe against an existing import.** Running `replace` twice is fine;
  running `merge` twice silently doubles the book. There is no import-batch id
  and no undo beyond `reset`.
- **Cost basis is one blended lot per ticker**, not the real tax lots. Brokers
  can export per-lot detail (Schwab's lot view, Fidelity's cost-basis page) and
  the `position_lots` table could hold them faithfully; the importer collapses
  them to a single weighted lot today.
- **No price sanity check at import.** A cost basis wildly off from the current
  market is accepted silently. A "this is 40x the current price, sure?" warning
  in the preview would catch a units mistake the parser's guards miss.
