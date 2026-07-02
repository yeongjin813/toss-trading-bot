# Research Log

Phase-by-phase strategy research, ablations, and improvement history.  
**For operators:** use [OPERATIONS.md](OPERATIONS.md). **For full legacy detail:** [REFERENCE.md](REFERENCE.md).

---

## Improvement journey (summary)

| Step | Problem | What we did | Verify |
|------|---------|-------------|--------|
| 1–2 | Single-ticker SMA whipsaw; over-filtered grid | CSV backtest baseline; SMA/RSI grid | `python run_backtest.py --isolated` |
| 3 | No live KIS path | OAuth, `.env`, `trading_state.json` | `python main.py` startup |
| 4 | Look-ahead, duplicate orders, holidays | Same-bar ATR order, cache, pending lock, NYSE calendar | `python test_analytics.py` |
| 5 | One global config hurt NVDA | Ticker regimes in `config.py` | Per-ticker backtests |
| 6 | `rt_cd=0` before fill | `OrderFillMonitor`, `RiskGuard` | `python test_execution_engine.py` |
| 7+ | Ops gaps (Telegram, RTH-only, retries) | `telegram_notifier.py`, `kis_http.py`, retry queue | Logs + `--diagnose` |
| 9 | Fixed universe dilution | Weekly momentum Top-N | `momentum_ranker.py` |
| 14 | Tech-only concentration | 25-ticker diversified watchlist | `scripts/compare_watchlist.py` |
| 15 | State corruption risk | Atomic writes in `state_persistence.py` | `test_state_persistence.py` |
| 17 | Optimistic backtest fills | 0.1% commission + 5 bps slippage default | `execution_friction.py` |
| 19–26 | Dual split, filters, Top4, capital | See phase table below | `scripts/*_sweep.py`, `scripts/oos_validation.py` |

---

## Phase timeline (2026)

| Phase | Topic | Outcome |
|-------|--------|---------|
| **17** | Realistic costs + filter combo | **SPY 200MA only** in prod; VIX / entry-confirm OFF |
| **18** | External quant feedback | Walk-forward tooling; enhanced momentum not promoted |
| **19** | Dual capital split | **70/30** Legacy/Top3 beats 60/40 on tested window |
| **20** | TSM absolute momentum gates | **OFF** in prod |
| **21** | Entry filter ablation | **52w-high OFF** (+CAGR); scale-in ON |
| **22** | Top-N + turnover band | **Top4**, **band=1** adopted |
| **23** | OOS validation (2023–25) | OOS gains held for tested configs; **overfitting risk reduced, not eliminated** |
| **24** | Aggressive vs prod | Semi-aggressive best MAR; aggressive fails bear2022 |
| **25** | Golden-cross ablation | **`USE_REGIME_GOLDEN_CROSS=false`** |
| **26** | Capital sweep | **$100k** `CAPITAL_AT_RISK` for VTS |

### Current prod defaults (VTS)

| Setting | Value | Note |
|---------|-------|------|
| `USE_52W_HIGH_FILTER` | false | Phase 21 |
| `MOMENTUM_TOP_N` | 4 | Phase 22 |
| `MOMENTUM_TOP_N_HOLD_BAND` | 1 | Phase 22 |
| `USE_REGIME_GOLDEN_CROSS` | false | Phase 25 |
| `LEGACY_CAPITAL_PCT` / `TOP3_CAPITAL_PCT` | 70 / 30 | Phase 19 |
| `ENTRY_CONFIRMATION_DAYS` | 0 | Phase 17 |
| `USE_VIX_REGIME_FILTER` | false | Phase 17 |

**Not adopted:** TSM gates (Ph20), aggressive Config B (Ph24), auto git pull on EC2.

### Research scripts

```powershell
python scripts/entry_filter_ablation.py
python scripts/oos_validation.py
python scripts/golden_cross_ablation.py
python scripts/aggressive_sweep.py
python scripts/capital_sweep.py
python scripts/filter_combo_backtest.py
```

---

## Still open (research / ops)

- Sustained live alpha vs buy-and-hold over long windows
- Full intraday backtest parity with live ATR stops
- Off-watchlist broker holdings edge cases
- VTS mock session **90-day renewal** (manual)

See [RISK.md](RISK.md) for operational residual risks.
