# VoltDesk modular component — extracted from app.py v0.9.35
# This first-stage split uses a context binder for safe, incremental refactoring.
from __future__ import annotations

import streamlit as st


def bind_context(context: dict) -> None:
    """Bind app-level dependencies during the transition to modular architecture."""
    globals().update(context)

@st.cache_data(ttl=900, show_spinner=False)
def fetch_backtest_history(yf_symbol: str, interval: str = "5m") -> dict:
    """Lädt die tatsächlich verfügbare Intraday-Historie für den Backtest.

    Fordert NICHT mehr blind einen festen period-String an - das war der Root
    Cause des ursprünglichen Fehlers (s. BT_FALLBACK_DAYS oben). Stattdessen wird
    mit dem für dieses Intervall maximal zulässigen Zeitraum begonnen und bei
    leerem Ergebnis schrittweise verkürzt (dünne Intraday-Abdeckung kommt bei
    manchen Nicht-US-Titeln vor, auch innerhalb des Yahoo-Limits).

    Gibt bei Erfolg ein dict zurück: df + tatsächlicher Start/Ende/Handelstage/
    Candle-Anzahl aus den ECHTEN gelieferten Daten (nicht aus dem angeforderten
    period-Wert). Bei endgültigem Fehlschlag wird eine BacktestDataError mit
    kategorisiertem Grund GEWORFEN statt (leeres df, "irgendein Fehler")
    zurückgegeben - st.cache_data cached keine Exceptions, ein leerer/fehlge-
    schlagener Versuch wird also nicht dauerhaft als "gültiges Ergebnis" gecacht.
    Zusätzliche Absicherung: der Aufrufer kann bei Bedarf
    fetch_backtest_history.clear() rufen, falls das Caching-Verhalten der
    Streamlit-Version doch von der Dokumentation abweichen sollte.
    """
    fallback_days = BT_FALLBACK_DAYS.get(interval, [59, 30, 10, 5])
    last_err = None
    any_network_error = False
    for days in fallback_days:
        period = f"{days}d"
        for attempt in range(2):
            try:
                df = yf.Ticker(yf_symbol).history(period=period, interval=interval, auto_adjust=False)
            except Exception as e:
                last_err = str(e) or type(e).__name__
                any_network_error = True
                if attempt == 0:
                    _sleep(1.0)
                continue
            if df is not None and not df.empty:
                df = df.dropna().copy()
                idx = df.index
                trading_days = 0
                if isinstance(idx, pd.DatetimeIndex) and len(idx):
                    try:
                        idx_local = idx.tz_convert(TZ_BERLIN) if idx.tz is not None else idx
                    except Exception:
                        idx_local = idx
                    trading_days = len(pd.Series(idx_local.date).unique())
                return {
                    "df": df,
                    "start": idx[0] if len(idx) else None,
                    "end": idx[-1] if len(idx) else None,
                    "trading_days": trading_days,
                    "candles": int(len(df)),
                    "requested_days": days,
                }
            last_err = "leer"
            break  # leer ist i.d.R. kein transienter Fehler - direkt kürzeren Zeitraum probieren
    # Ab hier: kein Zeitraum hat Daten geliefert.
    if any_network_error:
        raise BacktestDataError(
            "api",
            f"Yahoo Finance nicht erreichbar oder API-Fehler ({last_err}). "
            "Vermutlich vorübergehend - später erneut versuchen.",
        )
    # Ticker selbst prüfen: liefert er überhaupt TAGESDATEN? Falls ja, existiert
    # der Titel bei Yahoo, nur die Intraday-Historie für dieses Intervall fehlt.
    try:
        daily = yf.Ticker(yf_symbol).history(period="5d", interval="1d", auto_adjust=False)
    except Exception:
        daily = pd.DataFrame()
    if daily is None or daily.empty:
        raise BacktestDataError(
            "ticker",
            f"Kein Ticker/keine Daten bei Yahoo Finance für „{yf_symbol}“ gefunden "
            "(auch Tagesdaten leer - vermutlich falsches Symbol/Suffix).",
        )
    raise BacktestDataError(
        "interval",
        f"„{yf_symbol}“ existiert bei Yahoo Finance, aber für Intervall {interval} sind "
        f"aktuell keine Intraday-Daten verfügbar (getestet bis {max(fallback_days)} Tage zurück).",
    )


def _bt_prepare(df: pd.DataFrame, opening_minutes: int = 30) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x=df.copy()
    if not isinstance(x.index, pd.DatetimeIndex):
        return pd.DataFrame()
    idx=x.index
    try:
        idx_local=idx.tz_convert(TZ_BERLIN) if idx.tz is not None else idx.tz_localize(TZ_BERLIN)
    except Exception:
        idx_local=idx
    x['_day']=pd.Series(idx_local.date,index=x.index)
    x['_minute']=idx_local.hour*60+idx_local.minute
    typical=(x['High']+x['Low']+x['Close'])/3.0
    vol=x['Volume'].fillna(0.0)
    x['_pv']=typical*vol
    x['VWAP']=x.groupby('_day')['_pv'].cumsum()/x.groupby('_day')['Volume'].cumsum().replace(0,float('nan'))
    x['VWAP']=x['VWAP'].ffill().bfill()
    x['EMA20']=x['Close'].ewm(span=20,adjust=False).mean()
    x['TR']=(x['High']-x['Low']).rolling(14,min_periods=3).mean().bfill()
    orh=[]; orl=[]; active=[]
    for _,g in x.groupby('_day',sort=False):
        start=int(g['_minute'].iloc[0])
        win=g[g['_minute']<=start+opening_minutes]
        h=float(win['High'].max()) if not win.empty else float(g['High'].iloc[0])
        l=float(win['Low'].min()) if not win.empty else float(g['Low'].iloc[0])
        orh.extend([h]*len(g)); orl.extend([l]*len(g)); active.extend(list(g['_minute']>start+opening_minutes))
    x['ORH']=orh; x['ORL']=orl; x['_active']=active
    return x


def run_backtest(df: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run the OR/VWAP backtest with conservative execution modelling.

    Important modelling rules:
    - Entries/exits pay configurable slippage plus half-spread.
    - A gap through a stop/take executes at the opening price, not at the requested level.
    - If both stop and take are touched by the same OHLC candle, ``intrabar_policy``
      controls the result: conservative (stop first), optimistic (take first), or skip.
    - The default is conservative because OHLC data cannot reveal the intrabar path.
    """
    x = _bt_prepare(df, int(params.get('opening_minutes', 30)))
    if x.empty or len(x) < 20:
        return pd.DataFrame(), pd.DataFrame()

    fee = float(params.get('fee_pct', 0.0025))
    slip = max(0.0, float(params.get('slippage_pct', 0.0010)))
    spread = max(0.0, float(params.get('spread_pct', 0.0000)))
    half_spread = spread / 2.0
    risk_pct = float(params.get('risk_pct', 0.01))
    take_r = float(params.get('take_r', 2.0))
    capital = float(params.get('capital', 10000.0))
    max_pos = float(params.get('max_position_pct', 0.35))
    stop_mult = float(params.get('stop_atr', 1.2))
    one_per_day = bool(params.get('one_trade_per_day', True))
    intrabar_policy = str(params.get('intrabar_policy', 'conservative')).lower()
    if intrabar_policy not in {'conservative', 'optimistic', 'skip'}:
        intrabar_policy = 'conservative'

    def adverse_price(price: float, side: str, is_entry: bool) -> float:
        """Apply spread + slippage in the adverse direction."""
        # For a LONG position we buy on entry and sell on exit.
        # For SHORT we sell on entry and buy on exit.
        buy = (side == 'LONG' and is_entry) or (side == 'SHORT' and not is_entry)
        impact = slip + half_spread
        return price * (1.0 + impact if buy else 1.0 - impact)

    def exit_price_at_level(level: float, side: str) -> float:
        return adverse_price(float(level), side, is_entry=False)

    trades = []
    equity = []
    eq = capital
    pos = None
    traded_days = set()

    for i in range(1, len(x)):
        row = x.iloc[i]
        day = row['_day']
        prev = x.iloc[i - 1]

        if pos is not None:
            high = float(row['High'])
            low = float(row['Low'])
            close = float(row['Close'])
            open_px = float(row['Open'])
            stop_hit = (low <= pos['stop']) if pos['side'] == 'LONG' else (high >= pos['stop'])
            take_hit = (high >= pos['take']) if pos['side'] == 'LONG' else (low <= pos['take'])

            exit_px = None
            reason = None
            ambiguous = bool(stop_hit and take_hit)

            # First model a gap through either exit level. The opening auction/print
            # is the best OHLC-only proxy for the executable price in that case.
            if pos['side'] == 'LONG':
                gap_stop = open_px <= pos['stop']
                gap_take = open_px >= pos['take']
            else:
                gap_stop = open_px >= pos['stop']
                gap_take = open_px <= pos['take']

            if gap_stop:
                exit_px = adverse_price(open_px, pos['side'], is_entry=False)
                reason = 'gap-stop'
                ambiguous = False
            elif gap_take:
                exit_px = adverse_price(open_px, pos['side'], is_entry=False)
                reason = 'gap-take'
                ambiguous = False
            elif ambiguous:
                if intrabar_policy == 'skip':
                    # We cannot know which level was reached first from OHLC alone.
                    # Do not manufacture an edge; keep the position open.
                    continue
                if intrabar_policy == 'optimistic':
                    exit_px = exit_price_at_level(pos['take'], pos['side'])
                    reason = 'take-ambiguous'
                else:
                    exit_px = exit_price_at_level(pos['stop'], pos['side'])
                    reason = 'stop-ambiguous'
            elif stop_hit:
                exit_px = exit_price_at_level(pos['stop'], pos['side'])
                reason = 'stop'
            elif take_hit:
                exit_px = exit_price_at_level(pos['take'], pos['side'])
                reason = 'take'

            next_day = (i == len(x) - 1) or (x.iloc[i + 1]['_day'] != day)
            if exit_px is None and next_day:
                exit_px = adverse_price(close, pos['side'], is_entry=False)
                reason = 'eod'

            if exit_px is not None:
                gross = (exit_px / pos['entry'] - 1.0) * (1 if pos['side'] == 'LONG' else -1) * pos['notional']
                exit_fee = pos['notional'] * fee
                net = gross - pos['entry_fee'] - exit_fee
                r = net / max(pos['risk_eur'], 1e-9)
                eq += net
                trades.append({
                    'date': str(day), 'side': pos['side'], 'entry': pos['entry'], 'exit': exit_px,
                    'reason': reason, 'ambiguous_bar': ambiguous, 'gross_pnl': gross,
                    'fees': pos['entry_fee'] + exit_fee, 'net_pnl': net, 'R': r, 'equity': eq,
                })
                equity.append({'time': x.index[i], 'equity': eq})
                pos = None
            continue

        if one_per_day and day in traded_days:
            continue
        if not bool(row['_active']):
            continue
        if pd.isna(row['VWAP']) or pd.isna(row['TR']):
            continue

        long_sig = (
            float(row['Close']) > float(row['ORH'])
            and float(row['Close']) > float(row['VWAP'])
            and float(prev['Close']) <= float(prev['ORH'])
        )
        short_sig = (
            float(row['Close']) < float(row['ORL'])
            and float(row['Close']) < float(row['VWAP'])
            and float(prev['Close']) >= float(prev['ORL'])
        )
        if not (long_sig or short_sig):
            continue

        side = 'LONG' if long_sig else 'SHORT'
        entry = adverse_price(float(row['Close']), side, is_entry=True)
        atr = max(float(row['TR']), entry * 0.002)
        stop_dist = max(atr * stop_mult, entry * 0.001)
        risk_eur = eq * risk_pct
        notional = min(eq * max_pos, risk_eur / (stop_dist / entry))
        if notional <= 0:
            continue

        stop = entry - stop_dist if side == 'LONG' else entry + stop_dist
        take = entry + take_r * stop_dist if side == 'LONG' else entry - take_r * stop_dist
        entry_fee = notional * fee
        pos = {
            'side': side, 'entry': entry, 'stop': stop, 'take': take,
            'notional': notional, 'risk_eur': risk_eur, 'entry_fee': entry_fee,
        }
        traded_days.add(day)

    t = pd.DataFrame(trades)
    e = pd.DataFrame(equity)
    if not e.empty:
        e['peak'] = e['equity'].cummax()
        e['drawdown'] = e['equity'] - e['peak']
        e['drawdown_pct'] = e['drawdown'] / e['peak'].replace(0, pd.NA) * 100
    return t, e


def metrics_from_trades(t: pd.DataFrame) -> dict:
    if t is None or t.empty:
        return {'trades': 0, 'win_rate': 0.0, 'profit_factor': 0.0, 'expectancy': 0.0,
                'avg_r': 0.0, 'net': 0.0, 'gross': 0.0, 'fees': 0.0, 'max_dd': 0.0,
                'ambiguous_bars': 0}
    pnl = t['net_pnl'].astype(float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gp = float(wins.sum())
    gl = float(abs(losses.sum()))
    max_dd = 0.0
    if 'equity' in t.columns:
        eq = t['equity'].astype(float)
        peak = eq.cummax()
        dd = (eq - peak) / peak.replace(0, pd.NA) * 100
        max_dd = float(abs(dd.min())) if len(dd) else 0.0
    ambiguous = int(t['ambiguous_bar'].sum()) if 'ambiguous_bar' in t.columns else 0
    return {
        'trades': int(len(t)),
        'win_rate': float((pnl > 0).mean() * 100),
        'profit_factor': float(gp / gl) if gl > 0 else float('inf'),
        'expectancy': float(pnl.mean()),
        'avg_r': float(t['R'].mean()),
        'net': float(pnl.sum()),
        'gross': float(t['gross_pnl'].sum()),
        'fees': float(t['fees'].sum()),
        'max_dd': max_dd,
        'ambiguous_bars': ambiguous,
    }


def monte_carlo_from_trades(t: pd.DataFrame, runs: int = 1000, seed: int = 42) -> pd.DataFrame:
    if t is None or t.empty: return pd.DataFrame()
    vals=list(t['net_pnl'].astype(float)); rng=random.Random(seed); rows=[]
    for i in range(int(runs)):
        sample=[rng.choice(vals) for _ in vals]; eq=0.0; peak=0.0; dd=0.0
        for v in sample:
            eq+=v; peak=max(peak,eq); dd=min(dd,eq-peak)
        rows.append({'run':i+1,'final_pnl':eq,'max_drawdown':abs(dd),'win_rate':sum(1 for v in sample if v>0)/len(sample)*100})
    return pd.DataFrame(rows)


def compare_parameters(df: pd.DataFrame, base: dict) -> pd.DataFrame:
    rows = []
    for opening, take, stop in product([15, 30, 45], [1.5, 2.0, 2.5], [0.8, 1.2, 1.6]):
        p = {**base, 'opening_minutes': opening, 'take_r': take, 'stop_atr': stop}
        t, e = run_backtest(df, p)
        m = metrics_from_trades(t)
        # Robustness score: reward expectancy and sample size, penalise drawdown.
        # It deliberately does not let a tiny sample with an extreme PF dominate.
        n = max(int(m['trades']), 0)
        robust = 0.0
        if n > 0:
            robust = float(m['expectancy']) * math.sqrt(n) / (1.0 + float(m['max_dd']) / 100.0)
        rows.append({'OR (Min)': opening, 'Take R': take, 'Stop ATR': stop, **m,
                     'robust_score': robust})
    out = pd.DataFrame(rows)
    return out.sort_values(['robust_score', 'profit_factor', 'net'], ascending=[False, False, False]) if not out.empty else out


def walk_forward_backtest(df: pd.DataFrame, base: dict, train_days: int = 20, test_days: int = 10) -> tuple[pd.DataFrame, pd.DataFrame]:
    if df is None or df.empty:
        return pd.DataFrame(), pd.DataFrame()
    x = df.copy()
    days = sorted(pd.Series(x.index.date).unique())
    results = []
    all_test = []
    min_train_trades = int(base.get('min_train_trades', 5))

    for start in range(0, max(0, len(days) - train_days - test_days + 1), test_days):
        tr_days = days[start:start + train_days]
        te_days = days[start + train_days:start + train_days + test_days]
        if len(tr_days) < train_days or len(te_days) < test_days:
            break
        tr_mask = pd.Series(x.index.date, index=x.index).isin(tr_days)
        te_mask = pd.Series(x.index.date, index=x.index).isin(te_days)
        tr = x[tr_mask]
        te = x[te_mask]

        grid = compare_parameters(tr, base)
        if grid.empty:
            continue
        eligible = grid[grid['trades'] >= min_train_trades]
        if eligible.empty:
            # Do not silently fail a window; flag that the sample was too small and
            # use the best available candidate only as a fallback.
            best = grid.iloc[0]
            selection_status = 'fallback_low_sample'
        else:
            best = eligible.iloc[0]
            selection_status = 'eligible'

        p = {
            **base,
            'opening_minutes': int(best['OR (Min)']),
            'take_r': float(best['Take R']),
            'stop_atr': float(best['Stop ATR']),
        }
        tt, ee = run_backtest(te, p)
        m = metrics_from_trades(tt)
        results.append({
            'train_start': str(tr_days[0]), 'train_end': str(tr_days[-1]),
            'test_start': str(te_days[0]), 'test_end': str(te_days[-1]),
            'OR': p['opening_minutes'], 'TakeR': p['take_r'], 'StopATR': p['stop_atr'],
            'train_trades': int(best['trades']),
            'train_max_dd': float(best['max_dd']),
            'train_robust_score': float(best['robust_score']),
            'selection': selection_status,
            **m,
        })
        if not tt.empty:
            all_test.append(tt)

    return pd.DataFrame(results), (pd.concat(all_test, ignore_index=True) if all_test else pd.DataFrame())


def export_backtest_pdf(trades: pd.DataFrame, equity: pd.DataFrame, params: dict, m: dict, ticker: str) -> bytes:
    """Erzeugt ein PDF mit Backtest-Kennzahlen + Equity-Curve. Benötigt fpdf2 +
    matplotlib (s. HAS_FPDF/HAS_MATPLOTLIB) - der Aufrufer prüft das vorher."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", size=14)
    pdf.cell(0, 10, text=f"Backtest-Ergebnisse - {ticker}", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", size=9)
    pdf.cell(0, 6, text=f"Parameter: {params}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", size=10)
    display_keys = {
        "trades": "Trades", "win_rate": "Win Rate (%)", "profit_factor": "Profit Factor",
        "expectancy": "Erwartungswert (€)", "avg_r": "Ø R", "net": "Netto P&L (€)",
        "gross": "Brutto P&L (€)", "fees": "Gebühren (€)", "max_dd": "Max. Drawdown (%)",
    }
    for key, label in display_keys.items():
        value = m.get(key)
        if value is None:
            continue
        txt = f"{label}: {value:.2f}" if isinstance(value, float) else f"{label}: {value}"
        pdf.cell(0, 7, text=txt, new_x="LMARGIN", new_y="NEXT")
    if not equity.empty:
        pdf.ln(6)
        pdf.set_font("Helvetica", "B", size=11)
        pdf.cell(0, 8, text="Equity Curve", new_x="LMARGIN", new_y="NEXT")
        fig, ax = plt.subplots(figsize=(7, 3))
        ax.plot(equity["time"], equity["equity"], color="#00e5ff")
        ax.set_title("Equity Curve")
        ax.grid(alpha=0.3)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        pdf.image(buf, x=10, w=180)
    return bytes(pdf.output())


def render_prio3_analytics(meta):
    st.subheader('Backtest & Analytics')
    st.caption('Historische Simulation auf Yahoo-Finance-Daten. Paper-Research, keine Renditegarantie.')
    if not meta:
        st.info('Bitte zuerst einen Fokus-Titel wählen.')
        return
    c1,c2,c3,c4,c5,c6=st.columns(6)
    interval=c1.selectbox('Intervall',['5m','15m','30m','60m'],index=0,key='bt_interval')
    opening=c2.selectbox('OR Min',[15,30,45],index=1,key='bt_open')
    take_r=c3.selectbox('Take R',[1.5,2.0,2.5,3.0],index=1,key='bt_take')
    stop_atr=c4.slider('Stop-ATR-Faktor',0.5,3.0,1.2,0.1,key='bt_stop')
    risk=c5.slider('Risiko %',0.25,2.0,1.0,0.25,key='bt_risk')
    one=c6.checkbox('1 Trade/Tag',value=True,key='bt_one')
    c7,c8=st.columns(2)
    intrabar_policy=c7.selectbox(
        'Wenn Stop & Take in derselben Candle getroffen werden',
        ['conservative','optimistic','skip'],
        format_func=lambda v: {'conservative':'Konservativ — Stop zuerst','optimistic':'Optimistisch — Take zuerst','skip':'Nicht werten — Position offen lassen'}[v],
        index=0,key='bt_intrabar_policy',
        help='OHLC-Daten zeigen die Reihenfolge innerhalb der Candle nicht. Standard ist bewusst konservativ.'
    )
    spread_pct=c8.number_input(
        'Modellierter Spread (%)', min_value=0.0, max_value=2.0, value=0.0, step=0.01,
        key='bt_spread_pct', help='Zusätzlich zur Slippage wird je Seite die halbe Spreadbreite als Ausführungskosten modelliert.'
    )
    _bt_run_col1, _bt_run_col2, _bt_run_col3 = st.columns([2, 1, 2])
    with _bt_run_col2:
        _bt_run_clicked = st.button('Backtest starten', type='primary', key='bt_run', use_container_width=True)
    if _bt_run_clicked:
        with st.spinner(f'Backtest läuft (lädt verfügbare {interval}-Historie für {meta["ticker"]})...'):
            try:
                bt_result = fetch_backtest_history(meta['yf'], interval)
                bt_err = None
            except BacktestDataError as exc:
                bt_result = None
                bt_err = exc
        if bt_result is None:
            st.error(f'Keine historischen Daten für {meta["ticker"]} ({meta["yf"]}, {interval}) geladen: {bt_err}')
            if st.button('🔁 Erneut versuchen (Cache leeren)', key='bt_retry_clear_cache'):
                fetch_backtest_history.clear()
                st.rerun()
        else:
            st.session_state.bt_df = bt_result["df"]
            st.session_state.bt_data_meta = bt_result
            st.session_state.bt_params={
                'opening_minutes':opening,'take_r':take_r,'stop_atr':stop_atr,'risk_pct':risk/100,
                'fee_pct':0.0025,'slippage_pct':0.001,'spread_pct':spread_pct/100,
                'capital':CAPITAL,'max_position_pct':0.35,'one_trade_per_day':one,
                'intrabar_policy':intrabar_policy,'min_train_trades':5,
            }
            st.session_state.bt_trades,st.session_state.bt_equity=run_backtest(st.session_state.bt_df,st.session_state.bt_params)
            if st.session_state.bt_trades.empty:
                st.warning('Keine Trades mit den aktuellen Parametern erzeugt. Versuche andere Einstellungen (z.B. längere OR-Minuten oder niedrigeren Stop-ATR).')
    if 'bt_trades' not in st.session_state: return
    bt_meta = st.session_state.get("bt_data_meta")
    if bt_meta:
        _start = bt_meta.get("start")
        _end = bt_meta.get("end")
        _start_s = _start.strftime("%d.%m.%Y") if _start is not None else "—"
        _end_s = _end.strftime("%d.%m.%Y") if _end is not None else "—"
        st.caption(
            f"📅 Historische Daten (tatsächlich geladen): {_start_s} – {_end_s} · "
            f"{bt_meta.get('trading_days', 0)} Handelstage · {bt_meta.get('candles', 0):,} Candles".replace(",", ".")
        )
    t=st.session_state.bt_trades; e=st.session_state.bt_equity; m=metrics_from_trades(t)
    if t.empty:
        st.warning('Keine Trades mit den aktuellen Parametern erzeugt.')
        return
    if not e.empty: m['max_dd']=float(abs(e['drawdown_pct'].min()))
    st.markdown('#### 📊 Backtest-Ergebnisse')
    col1,col2,col3,col4=st.columns(4)
    col1.metric('Trades',m['trades'])
    col2.metric('Win Rate',f"{m['win_rate']:.1f}%")
    col3.metric('Profit Factor','∞' if math.isinf(m['profit_factor']) else f"{m['profit_factor']:.2f}")
    col4.metric('Netto P&L',f"{m['net']:+.2f} €")
    col5,col6,col7=st.columns(3)
    col5.metric('Max. Drawdown',f"{m.get('max_dd', 0):.2f}%")
    col6.metric('Erwartungswert',f"{m['expectancy']:+.2f} €")
    col7.metric('Ø R',f"{m['avg_r']:+.2f}")
    if HAS_FPDF and HAS_MATPLOTLIB:
        if st.button("📄 Backtest als PDF exportieren", key="export_bt_pdf"):
            try:
                pdf_bytes = export_backtest_pdf(t, e, st.session_state.bt_params, m, meta["ticker"])
                st.download_button(
                    label="📥 PDF herunterladen",
                    data=pdf_bytes,
                    file_name=f"Backtest_{meta['ticker']}_{datetime.now().strftime('%Y%m%d')}.pdf",
                    mime="application/pdf",
                    key="download_bt_pdf",
                )
            except Exception as exc:
                st.error(f"PDF-Export fehlgeschlagen: {exc}")
    else:
        st.caption(
            "🔒 PDF-Export benötigt die Pakete `fpdf2` und `matplotlib` in requirements.txt "
            "(matplotlib ist hier vorhanden, fpdf2 fehlt noch)."
            if not HAS_FPDF else
            "🔒 PDF-Export benötigt `matplotlib` in requirements.txt."
        )
    st.markdown('#### Equity Curve')
    if HAS_PLOTLY:
        fig=go.Figure(); fig.add_trace(go.Scatter(x=e['time'],y=e['equity'],name='Equity')); fig.update_layout(height=320,margin=dict(l=10,r=10,t=20,b=10)); st.plotly_chart(fig,use_container_width=True)
        fig2=go.Figure(); fig2.add_trace(go.Scatter(x=e['time'],y=e['drawdown_pct'],fill='tozeroy',name='Drawdown %')); fig2.update_layout(height=220,margin=dict(l=10,r=10,t=20,b=10)); st.plotly_chart(fig2,use_container_width=True)
    else:
        st.line_chart(e.set_index('time')['equity']); st.area_chart(e.set_index('time')['drawdown_pct'])
    st.markdown('#### Trades')
    st.dataframe(t,use_container_width=True,hide_index=True)
    tab1,tab2,tab3=st.tabs(['Monte-Carlo','Parametervergleich','Walk-Forward'])
    with tab1:
        runs=st.slider('Simulationen',200,5000,1000,200,key='mc_runs')
        if st.button('Monte-Carlo berechnen',key='mc_btn'):
            st.session_state.mc=monte_carlo_from_trades(t,runs)
        mc=st.session_state.get('mc')
        if isinstance(mc,pd.DataFrame) and not mc.empty:
            q=mc['final_pnl'].quantile([.05,.5,.95]); a,b,c=st.columns(3); a.metric('5%-Szenario',f"{q.loc[.05]:+.2f} €"); b.metric('Median',f"{q.loc[.5]:+.2f} €"); c.metric('95%-Szenario',f"{q.loc[.95]:+.2f} €")
            st.bar_chart(mc['final_pnl'].value_counts(bins=30).sort_index())
            st.dataframe(mc.describe().round(2),use_container_width=True)
    with tab2:
        if st.button('18 Parameter testen',key='param_btn'):
            st.session_state.param=compare_parameters(st.session_state.bt_df,st.session_state.bt_params)
        cp=st.session_state.get('param')
        if isinstance(cp,pd.DataFrame) and not cp.empty:
            st.dataframe(cp.round(3),use_container_width=True,hide_index=True)
    with tab3:
        train=st.selectbox('Train Tage',[20,30,40],index=0,key='wf_train'); test=st.selectbox('Test Tage',[5,10,15],index=1,key='wf_test')
        if st.button('Walk-Forward starten',key='wf_btn'):
            st.session_state.wf_summary,st.session_state.wf_trades=walk_forward_backtest(st.session_state.bt_df,st.session_state.bt_params,train,test)
        wf=st.session_state.get('wf_summary')
        if isinstance(wf,pd.DataFrame) and not wf.empty:
            st.dataframe(wf.round(3),use_container_width=True,hide_index=True)
            wft=st.session_state.get('wf_trades')
            if isinstance(wft,pd.DataFrame) and not wft.empty:
                wm=metrics_from_trades(wft); st.success(f"Out-of-Sample: {wm['trades']} Trades · Win Rate {wm['win_rate']:.1f}% · PF {'∞' if math.isinf(wm['profit_factor']) else f'{wm['profit_factor']:.2f}'} · Netto {wm['net']:+.2f} €")


