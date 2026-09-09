# VoltDesk – Modular Refactor v0.9.35

First-stage decomposition of the former monolithic `app.py` into six files.

## Files
- `app.py` – application entry point/orchestration and remaining cross-cutting logic
- `core.py` – shared rules, session/venue logic, risk helpers, reporting helpers
- `data.py` – market, quote, intraday, news and event data access
- `trading.py` – positions, execution simulation, stops/takes, risk governance
- `analytics.py` – backtest, metrics, Monte Carlo, parameter comparison, walk-forward
- `ui.py` – larger Streamlit UI components and dashboard rendering

## Migration approach
This first stage intentionally uses a small `bind_context()` compatibility bridge. The
extracted modules receive the fully initialized application namespace immediately before
runtime initialization. This avoids changing VoltDesk behavior while moving code physically.
The bridge is transitional and can be removed gradually as dependencies are made explicit.

No database files, secrets or runtime-generated assets are included.
