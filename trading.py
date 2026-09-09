# VoltDesk modular component — extracted from app.py v0.9.35
# This first-stage split uses a context binder for safe, incremental refactoring.
from __future__ import annotations

import streamlit as st


def bind_context(context: dict) -> None:
    """Bind app-level dependencies during the transition to modular architecture."""
    globals().update(context)

def estimate_stop_risk_eur(amount: float, price: Optional[float], stop: Optional[float], leverage: float) -> float:
    """Erwarteter Verlust in €, wenn der Stop getroffen wird (Einsatz × Hebel × Stop-Distanz)."""
    try:
        amt = float(amount or 0.0)
        px = float(price or 0.0)
        stp = float(stop or 0.0)
        lev = float(leverage or 1.0)
    except (TypeError, ValueError):
        return float(amount or 0.0)
    if amt <= 0 or px <= 0 or stp <= 0 or lev <= 0:
        return max(amt, 0.0)
    dist = abs(px - stp) / px
    return amt * lev * dist


def position_risk_snapshot(pos: dict, reference_price: Optional[float] = None) -> dict:
    """Centraler Risk-Snapshot für eine offene Position.

    planned_risk = Verlust bis zum aktuellen Paper-Stop nach dem VoltDesk-Modell.
    ko_risk = modellierter Verlust bis zur KO-Schwelle, sofern bekannt.
    gap_risk bleibt bewusst None, solange kein tatsächlich beobachtetes Session-Gap
    vorliegt; ein frei erfundener Stress-Gap würde dem Nutzer Scheingenauigkeit geben.
    """
    amount = _as_float(pos.get("amount"), 0.0) or 0.0
    entry = _as_float(pos.get("entry"), reference_price)
    stop = _as_float(pos.get("stop"))
    ko = _as_float(pos.get("ko"))
    lev = _as_float(pos.get("leverage"), 1.0) or 1.0
    out = {"planned_risk_eur": None, "ko_risk_eur": None, "gap_risk_eur": None}
    if amount <= 0 or not entry or entry <= 0 or lev <= 0:
        return out
    if stop and stop > 0:
        out["planned_risk_eur"] = estimate_stop_risk_eur(amount, entry, stop, lev)
    if ko and ko > 0:
        out["ko_risk_eur"] = estimate_stop_risk_eur(amount, entry, ko, lev)
    return out


def position_stop_risk_eur(pos: dict) -> float:
    """Stop-Risiko einer offenen Position; rechnet Altbestand (risk_eur == amount) nach."""
    amount = float(pos.get("amount") or 0.0)
    stored = pos.get("risk_eur")
    price = pos.get("entry") or pos.get("last")
    stop = pos.get("stop")
    lev = float(pos.get("leverage") or 1.0)
    computed = estimate_stop_risk_eur(amount, price, stop, lev)
    try:
        stored_f = float(stored) if stored is not None else None
    except (TypeError, ValueError):
        stored_f = None
    # Alte Positionen speicherten den Einsatz als risk_eur.
    if stored_f is None or (amount > 0 and abs(stored_f - amount) < 1e-6 and computed > 0):
        return computed
    return stored_f if stored_f > 0 else computed


def remaining_daily_risk_budget() -> float:
    """Noch verfügbares Tages-Risiko in € (Stop-Risiko, nicht Einsatz)."""
    risk_pct = float(st.session_state.get("daily_risk_pct", 2.0))
    cap = current_capital() * risk_pct / 100.0
    used = float(st.session_state.get("risk_used_today_eur", 0.0))
    return max(cap - used, 0.0)


def scale_in_count(ticker: str, side: str) -> int:
    """Anzahl bereits offener Zusatzkäufe derselben Seite auf dem Ticker."""
    n = sum(
        1
        for p in (st.session_state.get("positions") or [])
        if p.get("ticker") == ticker and p.get("side") == side
    )
    return max(n - 1, 0)


def calc_position_size(
    cash: float,
    atr: float,
    last_px: float,
    stop: Optional[float] = None,
    leverage: Optional[float] = None,
    risk_budget: Optional[float] = None,
) -> float:
    """Max. Einsatz: Positionslimit, Cap, und R-Sizing über Stop-Distanz × Hebel."""
    max_pct = get_position_limit_pct() / 100.0
    max_cash = min(cash * max_pct, cash)
    if atr and last_px and last_px > 0:
        vol_factor = max(0.3, min(1.0, 1.0 - (atr / last_px) * 10))
        max_cash = min(max_cash, cash * max_pct * vol_factor, cash)

    px = float(last_px or 0.0)
    stp = float(stop or 0.0) if stop else 0.0
    lev = float(leverage or 1.0)
    if px > 0 and stp > 0 and lev > 0:
        stop_pct = abs(px - stp) / px
        if stop_pct > 1e-9:
            if risk_budget is None:
                max_loss_cap = float(st.session_state.get("max_loss_per_trade_eur", 150.0))
                risk_budget = min(max_loss_cap, remaining_daily_risk_budget())
                if risk_budget <= 0:
                    risk_budget = max_loss_cap
            r_based = float(risk_budget) / (lev * stop_pct)
            max_cash = min(max_cash, r_based)

    return max(0.0, min(max_cash, cash))


def _as_float(val, default=None):
    try:
        if val is None or val == "" or (isinstance(val, float) and pd.isna(val)):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def extract_product_ko(product) -> Optional[float]:
    if not product:
        return None
    if isinstance(product, dict):
        return _as_float(product.get("KO", product.get("ko")))
    return _as_float(getattr(product, "KO", None) or getattr(product, "ko", None))


def extract_product_spread_pct(product) -> float:
    if not isinstance(product, dict):
        return 0.0
    return _as_float(product.get("Spread %", product.get("spread_pct")), 0.0) or 0.0


def apply_adverse_slippage(price: float, side: str, is_entry: bool, spread_pct: float = 0.0) -> float:
    """Paper-Fill gegen den Trader: Half-Spread + LIVE_SLIPPAGE_PCT."""
    px = _as_float(price, 0.0) or 0.0
    if px <= 0:
        return px
    slip = (float(spread_pct or 0.0) / 200.0) + (LIVE_SLIPPAGE_PCT / 100.0)
    long_pays_up = (side == "LONG" and is_entry) or (side == "SHORT" and not is_entry)
    return px * (1.0 + slip) if long_pays_up else px * (1.0 - slip)


def stop_is_beyond_ko(side: str, stop: float, ko: Optional[float]) -> bool:
    if ko is None or stop is None:
        return False
    if side == "LONG":
        return float(stop) <= float(ko)
    return float(stop) >= float(ko)


def _session_gap_open(pos: dict, fallback_last: float):
    """Erste Open-Notiz nach einer Session-Pause (>= SESSION_GAP_MINUTES) seit Entry."""
    entry_iso = pos.get("entry_at")
    meta = find_meta(pos.get("ticker")) if pos.get("ticker") else None
    yf_symbol = meta.get("yf") if meta else None
    if not entry_iso or not yf_symbol:
        return None
    try:
        entry_dt = datetime.fromisoformat(entry_iso).astimezone(timezone.utc)
        intra = fetch_intraday(yf_symbol)
        if intra is None or intra.empty:
            return None
        idx = pd.to_datetime(intra.index, utc=True)
        post = intra.loc[idx >= entry_dt]
        if post.empty:
            return None
        times = pd.to_datetime(post.index, utc=True)
        opens = pd.to_numeric(post.get("Open"), errors="coerce")
        prev_t = entry_dt
        for i, ts in enumerate(times):
            delta_min = (ts.to_pydatetime() - prev_t).total_seconds() / 60.0
            if delta_min >= SESSION_GAP_MINUTES:
                op = opens.iloc[i]
                if pd.notna(op):
                    return float(op)
            prev_t = ts.to_pydatetime()
        return None
    except Exception:
        return None


def calc_atr_stop(price: float, side: str, atr: Optional[float]) -> float:
    """Dynamischer Stop auf Basis der Volatilität statt eines starren Prozentwerts.

    Bisher war der Default-Stop überall hart auf 3% verdrahtet - unabhängig davon,
    ob ein Titel eng oder sehr volatil handelt. Ein 3%-Stop ist bei einem ruhigen
    Blue Chip oft zu weit (unnötig große Positionsgröße/Risiko) und bei einem sehr
    volatilen Titel oft zu eng (wird durch normales Rauschen ausgestoppt, bevor die
    Bewegung überhaupt beginnt). Die Distanz wird deshalb an den ATR(14) gekoppelt:
    Stop = Preis ∓ ATR_STOP_MULTIPLIER × ATR. Ohne ATR (z.B. Datenausfall) fällt die
    Funktion auf den bisherigen festen Prozentsatz zurück, damit ein Kauf nie an
    einem fehlenden ATR-Wert scheitert.
    """
    if atr and atr > 0 and price and price > 0:
        distance = ATR_STOP_MULTIPLIER * atr
        # Sicherheitsnetz: die ATR-Distanz nie über einen sehr weiten Prozentsatz
        # hinauswachsen lassen (z.B. bei Datenausreißern) und nie auf 0 fallen.
        distance = max(price * 0.005, min(distance, price * 0.15))
        return price - distance if side == "LONG" else price + distance
    return price * (1 - DEFAULT_STOP_PCT) if side == "LONG" else price * (1 + DEFAULT_STOP_PCT)


def _post_entry_extrema(pos: dict, fallback_last: float):
    """Return intraday high/low strictly from bars at/after position entry.

    The watchlist High/Low values come from the full current trading day. Using them
    directly for a newly opened position can incorrectly trigger a gap-stop because
    the day's high/low may have occurred BEFORE the position was opened.
    """
    entry_iso = pos.get("entry_at")
    meta = find_meta(pos.get("ticker")) if pos.get("ticker") else None
    yf_symbol = meta.get("yf") if meta else None
    if not entry_iso or not yf_symbol:
        return fallback_last, fallback_last
    try:
        entry_dt = datetime.fromisoformat(entry_iso).astimezone(timezone.utc)
        intra = fetch_intraday(yf_symbol)
        if intra is None or intra.empty:
            return fallback_last, fallback_last
        idx = pd.to_datetime(intra.index, utc=True)
        post = intra.loc[idx >= entry_dt]
        if post.empty:
            return fallback_last, fallback_last
        highs = pd.to_numeric(post.get("High"), errors="coerce").dropna()
        lows = pd.to_numeric(post.get("Low"), errors="coerce").dropna()
        day_high = float(highs.max()) if not highs.empty else fallback_last
        day_low = float(lows.min()) if not lows.empty else fallback_last
        return day_high, day_low
    except Exception:
        # Never let a data-format/timezone problem close a live paper position.
        return fallback_last, fallback_last


@st.cache_data(ttl=30, show_spinner=False)
def fetch_paper_mark(yf_symbol: str) -> dict:
    """Best-effort intraday mark for risk/execution simulation.

    Die bisherige Watchlist-Kursquelle basiert auf Daily-History und kann intraday
    naturgemäß veraltet sein. Für offene Positionen verwenden wir deshalb bevorzugt
    die letzte 5-Minuten-Candle und geben Alter/Quelle explizit zurück.
    """
    now = datetime.now(timezone.utc)
    try:
        intra = _fetch_intraday_raw(yf_symbol)
        if intra is not None and not intra.empty and "Close" in intra:
            ts = intra.index[-1]
            ts = pd.Timestamp(ts)
            if ts.tzinfo is None:
                ts = ts.tz_localize(timezone.utc)
            else:
                ts = ts.tz_convert(timezone.utc)
            px = float(intra["Close"].iloc[-1])
            age = max(0.0, (now - ts.to_pydatetime()).total_seconds())
            quality = "intraday" if age <= 15 * 60 else "stale-intraday"
            if px > 0:
                return {"price": px, "timestamp": ts.to_pydatetime(),
                        "age_seconds": age, "source": "yfinance-5m", "quality": quality}
    except Exception:
        pass
    q = fetch_quote(yf_symbol) or {}
    return {"price": q.get("price"), "timestamp": None, "age_seconds": None,
            "source": "fallback-daily", "quality": "delayed"}


def mark_positions(watch: pd.DataFrame) -> float:
    px = (
        {r["Ticker"]: (r["Kurs"], r.get("High"), r.get("Low")) for _, r in watch.iterrows()}
        if not watch.empty
        else {}
    )
    open_pnl = 0.0
    for pos in st.session_state.positions:
        meta = find_meta(pos.get("ticker")) if pos.get("ticker") else None
        symbol = meta.get("yf") if meta else None
        mark = fetch_paper_mark(symbol) if symbol else {}
        last = mark.get("price")
        _day_high = _day_low = None
        if last is None:
            row = px.get(pos["ticker"])
            last, _day_high, _day_low = row if row else (None, None, None)
        if last is None:
            continue
        # Kritische Positionsbewertung bevorzugt Intraday-Daten. Wenn nur Daily-Fallback
        # verfügbar ist, niemals behaupten, der Mark sei aktuell. Die Position bleibt
        # im Paper-Depot bestehen; ein Close/Stop darf nicht aus einem veralteten Preis
        # als "live" abgeleitet werden.
        pos["mark_source"] = mark.get("source")
        pos["mark_age_seconds"] = mark.get("age_seconds")
        pos["mark_timestamp"] = mark.get("timestamp").isoformat() if mark.get("timestamp") else None
        pos["mark_quality"] = mark.get("quality")
        pos["stale_for_execution"] = mark.get("quality") != "intraday"
        # IMPORTANT: watchlist High/Low are full-day values and can predate entry.
        # Gap-stop detection must only consider intraday bars at/after entry.
        day_high, day_low = _post_entry_extrema(pos, float(last))
        direction = 1 if pos["side"] == "LONG" else -1
        # Gap-Slippage: der letzte Tick (last) kann sich schon wieder vom Stop entfernt
        # haben, obwohl der Kurs zwischenzeitlich (Tageshoch/-tief seit Kauf) klar durch
        # den Stop gerutscht ist - ohne diese Prüfung würde ein Gap-Move, der sich bis zum
        # nächsten Refresh wieder erholt hat, unbemerkt bleiben und der Stop nie auslösen.
        # Fill-Preis in diesem Fall: das (schlechtere) Extremum, nicht der Stop selbst -
        # ein echter Gap wird i.d.R. nicht exakt am Stop-Kurs gefüllt, sondern dort, wo
        # der Markt als nächstes tatsächlich gehandelt hat.
        # Gap-Stop: hier WIRD das Extremum seit Entry verwendet (day_high/day_low aus
        # _post_entry_extrema, die bereits um Kursaktion VOR dem Kauf bereinigt sind) -
        # nicht der volle Tages-High/Low. Ein Gap liegt vor, wenn der Kurs seit Kauf
        # bereits klar durch den Stop gerutscht ist, unabhängig davon, ob sich der
        # aktuellste Tick (last) zwischenzeitlich wieder erholt hat. Eine bloße
        # Berührung des Stops (Extremum == Stop) gilt bewusst nicht als Gap.
        stop = _as_float(pos.get("stop"))
        ko = _as_float(pos.get("ko"))
        spread_pct = _as_float(pos.get("spread_pct"), 0.0) or 0.0
        gap_open = _session_gap_open(pos, float(last)) if not pos.get("stale_for_execution") else None

        knocked = False
        if ko is not None:
            knocked = (day_low <= ko) if pos["side"] == "LONG" else (day_high >= ko)
            if gap_open is not None:
                knocked = knocked or (
                    (gap_open <= ko) if pos["side"] == "LONG" else (gap_open >= ko)
                )

        gap_open_hit = False
        gapped = False
        if stop is not None:
            if pos["side"] == "LONG":
                gapped = day_low < stop
                gap_open_hit = gap_open is not None and gap_open < stop
            else:
                gapped = day_high > stop
                gap_open_hit = gap_open is not None and gap_open > stop

        fill_px = float(last)
        if knocked:
            raw = day_low if pos["side"] == "LONG" else day_high
            if gap_open is not None:
                raw = min(raw, gap_open) if pos["side"] == "LONG" else max(raw, gap_open)
            fill_px = min(raw, ko) if pos["side"] == "LONG" else max(raw, ko)
            fill_px = apply_adverse_slippage(fill_px, pos["side"], is_entry=False, spread_pct=spread_pct)
        elif gap_open_hit:
            fill_px = apply_adverse_slippage(gap_open, pos["side"], is_entry=False, spread_pct=spread_pct)
        elif gapped:
            raw = day_low if pos["side"] == "LONG" else day_high
            fill_px = apply_adverse_slippage(raw, pos["side"], is_entry=False, spread_pct=spread_pct)
        elif stop is not None and (
            (pos["side"] == "LONG" and float(last) <= stop)
            or (pos["side"] == "SHORT" and float(last) >= stop)
        ):
            fill_px = apply_adverse_slippage(stop, pos["side"], is_entry=False, spread_pct=spread_pct)

        pos["last"] = fill_px
        if pos.get("amount"):
            pos["pnl"] = direction * (pos["last"] / pos["entry"] - 1.0) * pos["amount"] * float(pos.get("leverage") or 1)
        else:
            pos["pnl"] = (pos["last"] - pos["entry"]) * direction * pos["shares"]
        open_pnl += pos["pnl"]
        # Stop-Nähe- und KO-Nähe-Alerts hier, da an dieser Stelle pos["last"] final
        # für diesen Durchlauf gesetzt ist (vor evtl. Take-Profit-Neuberechnung unten,
        # die den Fill-Preis noch verändern kann, aber die Nähe-Warnung soll auf dem
        # regulären laufenden Kurs basieren, nicht auf einem bereits realisierten Fill).
        check_stop_alerts(pos, pos["last"])
        check_ko_alerts(pos, pos["last"])
        take = _as_float(pos.get("take"))
        taken = False
        if take is not None and not knocked:
            if pos["side"] == "LONG" and (day_high >= take or (gap_open is not None and gap_open >= take)):
                taken = True
                raw = day_high if day_high >= take else gap_open
                fill_px = apply_adverse_slippage(max(float(take), float(raw or take)), pos["side"], is_entry=False, spread_pct=spread_pct)
            elif pos["side"] == "SHORT" and (day_low <= take or (gap_open is not None and gap_open <= take)):
                taken = True
                raw = day_low if day_low <= take else gap_open
                fill_px = apply_adverse_slippage(min(float(take), float(raw or take)), pos["side"], is_entry=False, spread_pct=spread_pct)
            if taken:
                pos["last"] = fill_px
                if pos.get("amount"):
                    pos["pnl"] = direction * (pos["last"] / pos["entry"] - 1.0) * pos["amount"] * float(pos.get("leverage") or 1)
                else:
                    pos["pnl"] = (pos["last"] - pos["entry"]) * direction * pos["shares"]

        hit = False if pos.get("stale_for_execution") else (knocked or taken or gap_open_hit or gapped or (
            stop is not None and (
                (pos["side"] == "LONG" and float(pos["last"]) <= stop)
                or (pos["side"] == "SHORT" and float(pos["last"]) >= stop)
            )
        ))
        pos["stopped"] = hit
        pos["gapped"] = gapped
        pos["gap_open_hit"] = gap_open_hit
        pos["knocked"] = knocked
        pos["taken"] = taken
        if ko is not None and float(last) > 0:
            pos["ko_distance_pct"] = abs(float(last) - ko) / float(last) * 100.0
        else:
            pos["ko_distance_pct"] = None
    return open_pnl


def close_stopped():
    keep = []
    for pos in st.session_state.positions:
        if pos.get("stopped"):
            st.session_state.closed_pnl += pos.get("pnl", 0.0)
            if pos.get("knocked"):
                reason = "ko"
            elif pos.get("taken"):
                reason = "take"
            elif pos.get("gap_open_hit"):
                reason = "gap-open"
            elif pos.get("gapped"):
                reason = "gap-stop"
            else:
                reason = "stop"
            record_fill(pos, reason)
        else:
            keep.append(pos)
    st.session_state.positions = keep


def queue_close_note(fill: dict):
    """Eine Pflichtzeile nach vollständigem Close — keine Broker-Wahrheit, nur Vollständigkeit."""
    if (fill.get("reason") or "") not in CLOSE_NOTE_REASONS:
        return
    if fill.get("action") not in (None, "Close"):
        return
    pending = list(st.session_state.get("pending_close_notes") or [])
    fid = fill.get("fill_id") or secrets.token_hex(8)
    fill["fill_id"] = fid
    pending.append({
        "fill_id": fid,
        "ticker": fill.get("ticker"),
        "reason": fill.get("reason"),
        "note": CLOSE_NOTE_PREFILL.get(fill.get("reason"), fill.get("reason") or "Close"),
        "mood": fill.get("mood"),
        "time": fill.get("time"),
    })
    st.session_state.pending_close_notes = pending


def pending_close_notes():
    return list(st.session_state.get("pending_close_notes") or [])


def render_pending_close_notes():
    items = pending_close_notes()
    if not items:
        return
    st.warning(f"{len(items)} Close-Notiz(en) offen — Tagesabschluss erst danach.")
    item = items[0]
    st.caption(f"{item.get('time') or ''} · {item.get('ticker') or ''} · {item.get('reason') or ''}")
    note = st.text_input(
        "Close-Notiz",
        value=item.get("note") or "",
        key=f"close_note_{item['fill_id']}",
        help="Technischer Grund ist vorausgefüllt. Eine Zeile reicht.",
    )
    mood_val = item.get("mood") or "calm"
    mood_keys = list(MOOD_OPTIONS.keys())
    mood_idx = mood_keys.index(mood_val) if mood_val in mood_keys else 0
    mood = st.selectbox(
        "Mood",
        mood_keys,
        index=mood_idx,
        format_func=lambda k: MOOD_OPTIONS.get(k, k),
        key=f"close_mood_{item['fill_id']}",
    )
    if st.button("Notiz speichern", key=f"save_close_note_{item['fill_id']}"):
        text = (note or "").strip()
        if not text:
            st.error("Eine Zeile Text ist Pflicht.")
        else:
            for fill in st.session_state.get("fills") or []:
                if fill.get("fill_id") == item["fill_id"]:
                    fill["close_note"] = text
                    fill["mood"] = mood
                    break
            st.session_state.pending_close_notes = [
                p for p in items if p.get("fill_id") != item["fill_id"]
            ]
            save_app_state()
            st.rerun()


def record_fill(pos: dict, reason: str, mood=None, setup_quality=None, setup_label=None):
    fee = charge_fee()
    pnl = pos.get("pnl", 0.0)
    setup_label = setup_label or pos.get("setup_label")
    mood = mood or pos.get("mood")
    setup_quality = setup_quality or pos.get("setup_quality")
    if pnl < 0:
        # Cooldown-Trigger: jede verlustreiche Schließung setzt den Zeitstempel, egal
        # ob durch Stop, Gap-Stop oder manuellen Verkauf ausgelöst - record_fill() ist
        # der gemeinsame Punkt, den alle drei Schließungspfade durchlaufen.
        st.session_state.last_loss_close_at = datetime.now(TZ_BERLIN).isoformat()
    # CRITICAL TRADE EVENT (2026-09): nur für automatische/erzwungene Schließungen
    # (reason ist Key in CRITICAL_EVENT_LABELS) - manuelle Verkäufe (reason="sell")
    # und der manuelle "Handelstag abschließen"-Button (reason="manual"/"day-end")
    # laufen bereits über einen Bestätigungsdialog VOR der Ausführung und brauchen
    # keine zusätzliche Nach-Bestätigung.
    if reason in CRITICAL_EVENT_LABELS:
        log_critical_trade_event(
            event_type=reason,
            ticker=pos.get("ticker") or "",
            side=pos.get("side"),
            details={"price": pos.get("last"), "pnl": pnl},
        )
    fill_row = {
            "fill_id": secrets.token_hex(8),
            "time": datetime.now(TZ_BERLIN).strftime("%H:%M:%S"),
            "ticker": pos.get("ticker"),
            "side": "SELL" if pos.get("side") == "LONG" else "BUY",
            "action": "Close",
            "reason": reason,
            "qty": pos.get("shares"),
            "amount": pos.get("amount"),
            "wkn": pos.get("wkn"),
            "px": pos.get("last"),
            "pnl": pnl,
            "fee": fee,
            "setup_label": setup_label,
            "mood": mood,
            "setup_quality": setup_quality,
            "close_note": None,
        }
    st.session_state.fills.insert(0, fill_row)
    queue_close_note(fill_row)
    save_trade_to_db(
        ticker=pos.get("ticker"), wkn=pos.get("wkn"), side=pos.get("side"),
        action="Close", entry=pos.get("entry"), exit_px=pos.get("last"),
        amount=pos.get("amount"), leverage=pos.get("leverage"),
        pnl=pnl, fees=fee, reason=reason,
        setup_label=setup_label, mood=mood, setup_quality=setup_quality,
    )


def close_partial(pid: str, fraction: float, watch: pd.DataFrame, reason: str = "partial-sell",
                  mood=None, setup_quality=None, setup_label=None):
    mark_positions(watch)
    for pos in st.session_state.positions:
        if pos.get("id") != pid:
            continue
        fraction = min(max(fraction, 0.0), 1.0)
        if fraction >= 0.999:
            close_one(pid, watch, reason, mood=mood, setup_quality=setup_quality, setup_label=setup_label)
            return
        qty = pos["shares"] * fraction
        realized = pos.get("pnl", 0.0) * fraction
        # Bugfix: "amount" wurde bisher versehentlich mit `realized` (= dem PnL-Betrag,
        # identisch zum "pnl"-Feld weiter unten) belegt statt mit dem tatsächlich
        # verkauften €-Betrag. Dadurch zeigte die Tagesbericht-Tabelle bei Teilverkäufen
        # in der Amount-Spalte den PnL statt des Verkaufsbetrags.
        sold_amount = float(pos.get("amount") or 0.0) * fraction
        fee = charge_fee()
        st.session_state.closed_pnl += realized
        if realized < 0:
            st.session_state.last_loss_close_at = datetime.now(TZ_BERLIN).isoformat()
        st.session_state.fills.insert(
            0,
            {
                "time": datetime.now(TZ_BERLIN).strftime("%H:%M:%S"),
                "ticker": pos.get("ticker"),
                "side": "SELL" if pos.get("side") == "LONG" else "BUY",
                "action": f"Teilverkauf {fraction:.0%}",
                "reason": reason,
                "qty": qty,
                "amount": sold_amount if sold_amount else None,
                "wkn": pos.get("wkn"),
                "px": pos.get("last"),
                "pnl": realized,
                "fee": fee,
                "setup_label": setup_label or pos.get("setup_label"),
                "mood": mood,
                "setup_quality": setup_quality,
            },
        )
        save_trade_to_db(
            ticker=pos.get("ticker"), wkn=pos.get("wkn"), side=pos.get("side"),
            action=f"Teilverkauf {fraction:.0%}", entry=pos.get("entry"),
            exit_px=pos.get("last"), amount=sold_amount,
            leverage=pos.get("leverage"), pnl=realized, fees=fee, reason=reason,
            setup_label=setup_label or pos.get("setup_label"), mood=mood, setup_quality=setup_quality,
        )
        pos["shares"] *= 1 - fraction
        if pos.get("amount"):
            pos["amount"] *= 1 - fraction
        pos["risk_eur"] = pos.get("risk_eur", 0.0) * (1 - fraction)
        pos["pnl"] = pos.get("pnl", 0.0) * (1 - fraction)
        save_app_state()
        return


def invested_amount() -> float:
    return sum(float(p.get("amount") or 0) for p in st.session_state.get("positions") or [])


def cash_available() -> float:
    return float(
        current_capital()
        + st.session_state.get("closed_pnl", 0.0)
        - invested_amount()
        - st.session_state.get("fees_paid", 0.0)
    )


def current_equity() -> float:
    """Equity = Cash + offener P&L der laufenden Positionen (nicht zu verwechseln mit
    current_capital(), der reinen Kapitalbasis, oder cash_available(), dem liquiden
    Cash ohne offene Positionen)."""
    open_pnl = sum(float(p.get("pnl", 0.0) or 0.0) for p in st.session_state.get("positions") or [])
    return float(cash_available() + invested_amount() + open_pnl)


def record_equity_snapshot(min_interval_seconds: int = 60):
    """Schreibt einen Equity-Snapshot in equity_history - gedrosselt (Standard: max.
    alle 60s), damit nicht bei jedem Rerun/Refresh ein neuer Datensatz entsteht.
    Gast-Modus: kein Snapshot (keine Persistenz ohne Login)."""
    user_id = current_user_id()
    if not user_id:
        return
    now = datetime.now(TZ_BERLIN)
    last_ts = st.session_state.get("_last_equity_snapshot_at")
    if last_ts and (now - last_ts).total_seconds() < min_interval_seconds:
        return
    st.session_state["_last_equity_snapshot_at"] = now
    equity = current_equity()
    open_pnl = sum(float(p.get("pnl", 0.0) or 0.0) for p in st.session_state.get("positions") or [])
    realized_pnl = float(st.session_state.get("closed_pnl", 0.0))
    peak = max(float(st.session_state.get("_equity_peak", equity)), equity)
    st.session_state["_equity_peak"] = peak
    drawdown = equity - peak
    drawdown_pct = (drawdown / peak * 100) if peak else 0.0
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO equity_history (timestamp, equity, cash, open_pnl, realized_pnl, drawdown, drawdown_pct, user_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (now.isoformat(), equity, cash_available(), open_pnl, realized_pnl, drawdown, drawdown_pct, user_id),
            )
    except Exception as e:
        # Equity-Tracking darf die App nie zum Absturz bringen - aber nicht mehr
        # komplett lautlos: Fehler landen (wie an den anderen geprueften Stellen)
        # im error_log, statt ohne jede Spur zu verschwinden.
        log_error("record_equity_snapshot", str(e), user_id=user_id)


@st.cache_data(ttl=30, show_spinner=False)  # Caching-Audit (2026-09) - ROLLBACK: diese
# Zeile entfernen. Kein Cache-Clear-Aufruf nötig (anders als bei Trades/Tages-
# berichten): record_equity_snapshot() ist ohnehin auf max. 1x/Min. gedrosselt,
# 30s TTL kostet also keine wahrnehmbare Aktualität.
def get_equity_history(limit: int = 500, user_id: str = None) -> pd.DataFrame:
    # SICHERHEITS-FIX (2026-09): user_id MUSS als Argument hereinkommen - dadurch
    # landet sie im st.cache_data-Schluessel. Vorher las die Funktion current_user_id()
    # selbst, der Session-Kontext ging aber NICHT in den Cache-Key ein: Nutzer B bekam
    # bei denselben Argumenten die gecachten Daten von Nutzer A (Cross-User-
    # Datenleck - auf Streamlit Cloud teilen sich alle Nutzer einen Prozess/Cache).

    if not user_id:
        return pd.DataFrame()
    df = _read_sql(
        "SELECT timestamp, equity, cash, open_pnl, realized_pnl, drawdown, drawdown_pct "
        "FROM equity_history WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
    )
    if not df.empty:
        df = df.iloc[::-1].reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def compute_recovery_time(df: pd.DataFrame) -> Optional[str]:
    """Zeit seit dem letzten Equity-Hoch, bis dieses wieder erreicht/übertroffen wurde -
    bzw. 'läuft noch' wenn der Drawdown aktuell noch anhält."""
    if df.empty or len(df) < 2:
        return None
    running_peak = df["equity"].cummax()
    peak_idx = None
    for i in range(len(df) - 1, -1, -1):
        if df["equity"].iloc[i] >= running_peak.iloc[i] - 1e-9:
            peak_idx = i
            break
    if peak_idx is None:
        return None
    if peak_idx == len(df) - 1:
        return "kein aktiver Drawdown"
    delta = df["timestamp"].iloc[-1] - df["timestamp"].iloc[peak_idx]
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() / 60)} Min. (läuft noch)"
    return f"{hours:.1f} Std. (läuft noch)"


def set_cash_balance(new_cash: float):
    """Setzt den verfügbaren Cash-Bestand manuell (nur Pro Modus). Da cash_available()
    sich aus account_base + closed_pnl - invested - fees_paid ergibt, wird die
    Differenz auf account_base verrechnet - offene Positionen/P&L bleiben unangetastet,
    nur die Kapitalbasis wird angepasst."""
    delta = float(new_cash) - cash_available()
    st.session_state.account_base = float(st.session_state.get("account_base", CAPITAL)) + delta


def charge_fee() -> float:
    # Pro Modus: Gebühr über Sidebar-Eingabefeld einstellbar; sonst fest 1€ (Standard).
    fee = float(st.session_state.get("fee_per_trade", FEE_PER_TRADE)) if effective_pro_mode() else float(FEE_PER_TRADE)
    st.session_state.fees_paid = float(st.session_state.get("fees_paid", 0.0)) + fee
    return fee


def validate_take_profit_distance(side: str, entry: float, take: Optional[float], amount: float, leverage: float, ko: Optional[float] = None) -> Optional[str]:
    """Hard TP guardrail: TP must be directionally valid and gross profit > round-trip fees."""
    if take is None:
        return None
    entry = float(entry); take = float(take); amount = float(amount); leverage = float(leverage or 1.0)
    if side == "LONG" and take <= entry:
        return "Take-Profit muss bei LONG oberhalb des Einstiegs liegen."
    if side == "SHORT" and take >= entry:
        return "Take-Profit muss bei SHORT unterhalb des Einstiegs liegen."
    if ko is not None:
        if side == "LONG" and take >= float(ko):
            return f"Take-Profit liegt bei LONG hinter/auf der KO-Schwelle ({float(ko):.4f})."
        if side == "SHORT" and take <= float(ko):
            return f"Take-Profit liegt bei SHORT hinter/auf der KO-Schwelle ({float(ko):.4f})."
    gross = abs(take - entry) / max(entry, 1e-9) * amount * leverage
    fee = float(st.session_state.get("fee_per_trade", FEE_PER_TRADE)) if effective_pro_mode() else float(FEE_PER_TRADE)
    round_trip = 2.0 * fee
    if gross <= round_trip + 1e-9:
        return f"Take-Profit zu nah: erwarteter Bruttogewinn {gross:.2f} € muss größer als Round-Trip-Gebühr {round_trip:.2f} € sein."
    return None


def position_r_multiple(pos: dict) -> Optional[float]:
    try:
        entry = float(pos.get("entry")); stop = float(pos.get("stop")); last = float(pos.get("last"))
        if pos.get("side") == "LONG":
            risk = entry - stop
            return (last - entry) / risk if risk > 0 else None
        risk = stop - entry
        return (entry - last) / risk if risk > 0 else None
    except (TypeError, ValueError):
        return None


def render_r_milestones(pos: dict) -> None:
    r = position_r_multiple(pos)
    if r is None:
        return
    if r >= 2.0:
        st.success(f"⚡ +{r:.1f}R · Teilgewinn prüfen (2R erreicht).")
    elif r >= 1.5:
        st.info(f"🟢 +{r:.1f}R · Teilgewinn prüfen (1,5R erreicht).")
    else:
        st.caption(f"R-Multiple: {r:+.1f}R")


def execute_paper_buy(ticker, side, last_px, stop, amount, leverage, typ, wkn, reason="ticket",
                      setup_label=None, mood=None, setup_quality=None, ko=None, spread_pct=None,
                      take=None, off_plan=False):
    amount = float(amount)
    # last_px<=0 explizit prüfen, nicht nur "is None": ein Datenfeed-Aussetzer liefert
    # manchmal 0.0 statt None (z.B. vorbörslich ohne Umsatz), und amount/last_px würde
    # dann sofort mit ZeroDivisionError crashen statt die Aktion sauber abzulehnen.
    if amount <= 0 or last_px is None or last_px <= 0:
        return False, "Ungültiger Betrag oder Kurs."
    if stop is None or stop <= 0:
        # Harte Ablehnung ohne Stop: verhindert ungeschützte Positionen, die bei einem
        # Gap-Move (s. mark_positions()) ohne jede Verlustbegrenzung offenstehen würden.
        return False, "Ticket abgelehnt: Kein Stop hinterlegt. Jede Position braucht einen Stop-Kurs."
    ko = _as_float(ko)
    spread_pct = _as_float(spread_pct, 0.0) or 0.0
    take = _as_float(take)
    if ko is not None and stop_is_beyond_ko(side, stop, ko):
        return False, (
            f"Ticket abgelehnt: Stop {float(stop):.4f} liegt hinter der KO-Schwelle "
            f"{ko:.4f}. Der Stop muss vor dem Knock-out greifen."
        )
    # Entry-Fill: Half-Spread + Slippage gegen den Trader, bevor Risiko gerechnet wird.
    last_px = apply_adverse_slippage(float(last_px), side, is_entry=True, spread_pct=spread_pct)
    # Stop must remain on the loss side of the actual paper fill. This catches
    # stale/UI-calculated stops that become invalid after adverse entry slippage.
    if side == "LONG" and float(stop) >= last_px:
        return False, "Ticket abgelehnt: LONG-Stop liegt nicht unter dem tatsächlichen Entry-Fill."
    if side == "SHORT" and float(stop) <= last_px:
        return False, "Ticket abgelehnt: SHORT-Stop liegt nicht über dem tatsächlichen Entry-Fill."

    pro_mode = effective_pro_mode()

    if st.session_state.get("trading_pause", False):
        return False, "Trading Pause aktiv: keine neuen Positionen werden eröffnet. Bestehende Positionen dürfen weiterhin gemanagt werden."

    if st.session_state.get("killed", False):
        return False, "Kill Switch aktiv: Handel ist vollständig gesperrt."
    if st.session_state.get("risk_lock", False):
        return False, "Daily Loss Limit / Risk Lock: keine neuen Positionen bis zum nächsten Paper-Tag."
    if st.session_state.get("tilt_enabled", True) and st.session_state.get("tilt_lock", False):
        why = st.session_state.get("tilt_lock_reason") or "Tilt-Score über der Sperrschwelle"
        return False, (
            "Tilt-Lock aktiv: keine neuen Positionen. "
            f"{why}. Lock in den Einstellungen oder mit „Tilt-Lock aufheben“ lösen."
        )

    # Guardrail 1: Hard Cap Trade-Eröffnungen/Tag - Wert kommt jetzt aus dem Sidebar-Slider
    # (max_trades_per_day), Pro Modus setzt ihn auf None = unbegrenzt.
    max_trades = st.session_state.get("max_trades_per_day", 5)
    if not pro_mode and max_trades is not None and int(st.session_state.get("trades_opened_today", 0)) >= int(max_trades):
        return False, f"Tageslimit erreicht: max. {int(max_trades)} eröffnete Trades/Tag (Pro Modus schaltet das frei)."

    # Guardrail 2: 10-Minuten-Cooldown nach jeder verlustreichen Schließung - verhindert
    # sofortiges Revenge-Trading direkt nach einem Verlust (Pro Modus hebt das auf).
    if not pro_mode:
        last_loss_iso = st.session_state.get("last_loss_close_at")
        if last_loss_iso:
            last_loss_at = datetime.fromisoformat(last_loss_iso)
            cooldown_left = timedelta(minutes=10) - (datetime.now(TZ_BERLIN) - last_loss_at)
            if cooldown_left.total_seconds() > 0:
                mins = int(cooldown_left.total_seconds() // 60) + 1
                return False, f"Cooldown nach Verlust aktiv: noch ~{mins} Min. (Pro Modus schaltet das frei)."

    # Guardrail 3.6 zuerst: Stop-Risiko in € (Einsatz × Hebel × Stop-Distanz).
    stop_distance_pct = abs(last_px - stop) / last_px
    est_loss_eur = estimate_stop_risk_eur(amount, last_px, stop, leverage)

    # Guardrail 3: Tages-Risiko-Cap auf das echte Stop-Risiko, nicht auf den Einsatz.
    if not pro_mode:
        risk_pct = float(st.session_state.get("daily_risk_pct", 2.0))
        risk_cap = current_capital() * risk_pct / 100.0
        risk_used = float(st.session_state.get("risk_used_today_eur", 0.0))
        if risk_used + est_loss_eur > risk_cap + 1e-6:
            remaining = max(risk_cap - risk_used, 0.0)
            return False, (
                f"Tages-Risiko-Limit erreicht: max. {risk_cap:.0f} € Stop-Risiko "
                f"({risk_pct:.1f}% vom Kapital), dieser Trade {est_loss_eur:.0f} €, "
                f"noch {remaining:.0f} € frei (Pro Modus schaltet das frei)."
            )
    max_loss_cap = float(st.session_state.get("max_loss_per_trade_eur", 150.0))
    if est_loss_eur > max_loss_cap + 1e-6:
        return False, (
            f"Max. Verlust pro Trade überschritten: geschätzter Verlust bei Stop "
            f"{est_loss_eur:.0f} € > Limit {max_loss_cap:.0f} € "
            f"(Einsatz × Hebel × Stop-Distanz {stop_distance_pct*100:.1f}%). "
            f"Einsatz, Hebel oder Stop-Distanz reduzieren."
        )

    # Guardrail 3.5: Konto-weites Exposure-Limit - bisher gab es keine Prüfung über alle
    # offenen Positionen hinweg, nur Einzel-Trade-Limits (Guardrail 3). Ein Konto konnte
    # so durch viele kleine, einzeln erlaubte Trades in Summe weit über sein Kapital
    # hinaus gehebelt werden. Notional = Einsatz * Hebel (der tatsächlich bewegte
    # Positionswert), gilt unabhängig vom Pro Modus, da es die Konto-Existenz schützt,
    # nicht nur die Tages-Disziplin.
    open_notional = sum(
        float(p.get("amount") or 0) * float(p.get("leverage") or 1)
        for p in st.session_state.get("positions", [])
    )
    new_notional = amount * float(leverage or 1)
    equity = current_capital()
    exposure_cap = equity * MAX_TOTAL_EXPOSURE_MULT
    if equity > 0 and (open_notional + new_notional) > exposure_cap + 1e-6:
        return False, (
            f"Exposure-Limit erreicht: Gesamt-Positionswert (Einsatz × Hebel) darf max. "
            f"{MAX_TOTAL_EXPOSURE_MULT:.1f}× das Kapital betragen ({exposure_cap:.0f} €). "
            f"Bereits offen: {open_notional:.0f} €, dieser Trade: {new_notional:.0f} €."
        )

    # Guardrail Tilt: nach den harten Risiko-Caps, vor Boredom Guard.
    if st.session_state.get("tilt_enabled", True):
        tilt = detect_tilt(st.session_state.get("fills") or [], current_mood=mood)
        apply_tilt_lock_if_needed(tilt)
        if tilt.get("suggested_action") == "lock" or st.session_state.get("tilt_lock"):
            why = " · ".join(tilt.get("reasons") or []) or st.session_state.get("tilt_lock_reason") or "Tilt-Score kritisch"
            return False, (
                f"Tilt-Lock (Score {tilt.get('tilt_score', 0)}): Trade abgelehnt. {why}. "
                "Pause einlegen oder Lock manuell aufheben."
            )

    # Guardrail 4 ("Boredom Guard"): manuelle Käufe ohne frische Empfehlung (Katalog-Kauf,
    # nicht aus dem Empfehlungs-Flow) verlangen einen eingetragenen Grund in der Tagesnotiz -
    # soll unreflektiertes "Klicken aus Langeweile" bewusst bremsen, ohne es hart zu verbieten.
    if reason == "katalog":
        today_key = datetime.now(TZ_BERLIN).strftime("%Y-%m-%d")
        today_note = (st.session_state.get("day_notes") or {}).get(today_key, "").strip()
        if not today_note:
            return False, (
                "Manueller Kauf ohne Empfehlung: bitte zuerst einen Grund in der "
                "Tagesnotiz (weiter unten) eintragen und speichern."
            )

    if reason == "nachkauf":
        adds = scale_in_count(ticker, side)
        if adds >= MAX_SCALE_INS:
            return False, (
                f"Nachkauf-Limit erreicht: max. {MAX_SCALE_INS} Scale-in(s) "
                f"je Ticker und Seite."
            )
        twins = [
            p for p in (st.session_state.get("positions") or [])
            if p.get("ticker") == ticker and p.get("side") == side
        ]
        if twins:
            if not effective_pro_mode() and any(
                float(p.get("pnl", 0.0) or 0.0) < 0 for p in twins
            ):
                return False, "Nachkauf blockiert: bestehende Position derselben Seite ist im Verlust."
            parent_stop = next((p.get("stop") for p in twins if p.get("stop") is not None), None)
            if parent_stop is not None:
                # Stop der Zusatzposition darf das Risiko nicht lockern.
                if side == "LONG" and stop < float(parent_stop):
                    stop = float(parent_stop)
                elif side == "SHORT" and stop > float(parent_stop):
                    stop = float(parent_stop)
                est_loss_eur = estimate_stop_risk_eur(amount, last_px, stop, leverage)

        # Re-check after Scale-in stop normalization: the inherited parent stop can
        # materially change the € risk and must not bypass the hard risk caps above.
        if not pro_mode:
            risk_pct = float(st.session_state.get("daily_risk_pct", 2.0))
            risk_cap = current_capital() * risk_pct / 100.0
            risk_used = float(st.session_state.get("risk_used_today_eur", 0.0))
            if risk_used + est_loss_eur > risk_cap + 1e-6:
                return False, f"Tages-Risiko-Limit nach Scale-in-Stop-Prüfung überschritten: {est_loss_eur:.0f} € Risiko."
        if est_loss_eur > float(st.session_state.get("max_loss_per_trade_eur", 150.0)) + 1e-6:
            return False, f"Max. Verlust pro Trade nach Scale-in-Stop-Prüfung überschritten: {est_loss_eur:.0f} €."

    if amount + FEE_PER_TRADE > cash_available() + 1e-6:
        return False, "Nicht genug Paper-Cash (inkl. 1 $ Gebühr)."
    if take is None:
        try:
            meta = find_meta(ticker)
            lv = fetch_levels(meta["yf"]) if meta else {}
            intra = fetch_intraday(meta["yf"]) if meta else None
            take = suggest_take_profit(side, last_px, lv, intra)
        except Exception:
            take = None
    tp_error = validate_take_profit_distance(side, last_px, take, amount, leverage, ko)
    if tp_error:
        return False, tp_error
    fee = charge_fee()
    shares = amount / last_px
    now_berlin = datetime.now(TZ_BERLIN)
    st.session_state.trades_opened_today = int(st.session_state.get("trades_opened_today", 0)) + 1
    st.session_state.risk_used_today_eur = float(st.session_state.get("risk_used_today_eur", 0.0)) + est_loss_eur
    pos = {
        "id": now_berlin.strftime("%H%M%S%f"),
        "ticker": ticker,
        "side": side,
        "entry": last_px,
        "entry_at": now_berlin.isoformat(),
        "stop": stop,
        "shares": shares,
        "amount": amount,
        "leverage": leverage,
        "typ": typ,
        "wkn": wkn,
        "risk_eur": est_loss_eur,
        "last": last_px,
        "pnl": 0.0,
        "time": now_berlin.strftime("%H:%M:%S"),
        "breakeven_secured": False,
        "setup_label": setup_label,
        "mood": mood,
        "setup_quality": setup_quality,
        "ko": ko,
        "spread_pct": spread_pct,
        "take": take,
        "off_plan": bool(off_plan),
    }
    pos.update(position_risk_snapshot(pos))
    st.session_state.positions.append(pos)
    st.session_state.fills.insert(
        0,
        {
            "time": pos["time"],
            "ticker": ticker,
            "side": "BUY" if side == "LONG" else "SELL",
            "action": "Open",
            "reason": reason,
            "qty": shares,
            "amount": amount,
            "px": last_px,
            "wkn": wkn,
            "pnl": 0.0,
            "fee": fee,
            "setup_label": setup_label,
            "mood": mood,
            "setup_quality": setup_quality,
            "off_plan": bool(off_plan),
        },
    )
    save_trade_to_db(
        ticker=ticker, wkn=wkn, side=side, action="Open",
        entry=last_px, exit_px=None, amount=amount,
        leverage=leverage, pnl=0.0, fees=fee, reason=reason,
        setup_label=setup_label, mood=mood, setup_quality=setup_quality,
    )
    save_app_state()
    return True, "ok"


def update_stop(pid: str, new_stop: float, event_type: str = "trail"):
    """Setzt einen Stop nur in sicherer Richtung und erzeugt ein Critical Event.

    Invariante: LONG-Stop darf niemals sinken, SHORT-Stop niemals steigen.
    Zusätzlich darf der Stop nicht hinter die KO-Schwelle gelegt werden. Diese Regeln
    liegen bewusst hier in der tiefsten Änderungsfunktion und nicht nur in der UI.
    """
    try:
        new_stop = float(new_stop)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(new_stop) or new_stop <= 0:
        return False
    for pos in st.session_state.positions:
        if pos.get("id") != pid:
            continue
        try:
            old_stop = float(pos.get("stop")) if pos.get("stop") is not None else None
        except (TypeError, ValueError):
            old_stop = None
        side = str(pos.get("side") or "").upper()
        ko = _as_float(pos.get("ko"))
        if old_stop is not None:
            if side == "LONG" and new_stop < old_stop - 1e-9:
                return False
            if side == "SHORT" and new_stop > old_stop + 1e-9:
                return False
        if ko is not None and stop_is_beyond_ko(side, new_stop, ko):
            return False
        entry = _as_float(pos.get("entry"))
        if entry is not None:
            # Ein Stop auf der falschen Seite des Marktes ist fast immer ein Daten-/UI-Fehler.
            # Breakeven und profitable Trailing-Stops bleiben ausdrücklich erlaubt.
            if side == "LONG" and new_stop >= entry and old_stop is None:
                pass
            elif side == "SHORT" and new_stop <= entry and old_stop is None:
                pass
        if old_stop is not None and abs(old_stop - new_stop) < 1e-9:
            return True
        pos["stop"] = new_stop
        risk = position_risk_snapshot(pos)
        pos.update(risk)
        kind = event_type if event_type in CRITICAL_EVENT_LABELS else "trail"
        log_critical_trade_event(
            event_type=kind,
            ticker=pos.get("ticker") or "",
            side=pos.get("side"),
            details={
                "old_stop": old_stop,
                "new_stop": new_stop,
                "price": pos.get("last") or pos.get("entry"),
                "planned_risk_eur": risk.get("planned_risk_eur"),
                "ko_risk_eur": risk.get("ko_risk_eur"),
            },
        )
        save_app_state()
        return True
    return False


def update_take(pid: str, new_take: float, event_type: str = "take-set"):
    for pos in st.session_state.get("positions") or []:
        if pos.get("id") != pid:
            continue
        new_take = _as_float(new_take)
        if new_take is not None and new_take <= 0:
            pos["take"] = None
            log_critical_trade_event("take-clear", pos.get("ticker") or "", pos.get("side"), {"take": None, "price": pos.get("last")})
            save_app_state()
            return True
        error = validate_take_profit_distance(pos.get("side"), pos.get("entry"), new_take, pos.get("amount", 0), pos.get("leverage", 1), pos.get("ko"))
        if error:
            st.error(error)
            return False
        pos["take"] = float(new_take)
        log_critical_trade_event(event_type, pos.get("ticker") or "", pos.get("side"), {"take": float(new_take), "price": pos.get("last")})
        save_app_state()
        return True
    return False


def close_one(pid: str, watch: pd.DataFrame, reason: str = "sell",
              mood=None, setup_quality=None, setup_label=None):
    mark_positions(watch)
    keep = []
    closed_pnl = 0.0
    for pos in st.session_state.positions:
        if pos.get("id") == pid:
            closed_pnl = pos.get("pnl", 0.0)
            st.session_state.closed_pnl += closed_pnl
            record_fill(pos, reason, mood=mood, setup_quality=setup_quality, setup_label=setup_label)
        else:
            keep.append(pos)
    st.session_state.positions = keep
    save_app_state()


def flatten_all(watch: pd.DataFrame, reason: str = "manual",
               mood=None, setup_quality=None, setup_label=None):
    mark_positions(watch)
    total_pnl = 0.0
    tickers = []
    for pos in st.session_state.positions:
        pnl = pos.get("pnl", 0.0)
        total_pnl += pnl
        tickers.append(pos.get("ticker", ""))
        st.session_state.closed_pnl += pnl
        record_fill(pos, reason, mood=mood, setup_quality=setup_quality,
                    setup_label=setup_label or pos.get("setup_label"))
    st.session_state.positions = []
    save_app_state()


def manage_position(pos: dict, rec: dict, levels: dict, mode: str, close_subphase: Optional[str] = None) -> dict:
    last = pos.get("last") or pos.get("entry")
    entry = pos.get("entry") or last
    side = pos.get("side")
    pnl = float(pos.get("pnl", 0.0) or 0.0)
    stop = pos.get("stop")
    rec_side = (rec or {}).get("side")
    rec_action = (rec or {}).get("action")
    ticker = pos.get("ticker") or "?"

    def _px(x):
        try:
            return f"{float(x):.2f}"
        except (TypeError, ValueError):
            return "—"

    if mode == "CLOSE":
        return {
            "action": "CLOSE",
            "label": (
                f"{ticker}: Session Close — glattstellen oder Auto-Close "
                f"(Last {_px(last)}, P&L {pnl:+.2f} €)."
            ),
            "trail": last,
            "reason_code": "session_close",
        }

    scenario_ok = rec_action == "BUY" and rec_side == side
    scenario_dead = rec_action == "BUY" and rec_side and rec_side != side

    if scenario_dead:
        return {
            "action": "EXIT",
            "label": (
                f"{ticker}: EXIT — Szenario tot. Seite {side}, Empfehlung {rec_side}. "
                f"Glattstellen (P&L {pnl:+.2f} €)."
            ),
            "trail": None,
            "reason_code": "scenario_dead",
        }

    if last is not None and stop is not None:
        try:
            stop_f, last_f = float(stop), float(last)
            if side == "LONG" and last_f <= stop_f:
                return {
                    "action": "EXIT",
                    "label": (
                        f"{ticker}: EXIT — Stop verletzt (Last {_px(last)} ≤ Stop {_px(stop)}). "
                        f"Sofort glattstellen (P&L {pnl:+.2f} €)."
                    ),
                    "trail": None,
                    "reason_code": "stop_hit",
                }
            if side == "SHORT" and last_f >= stop_f:
                return {
                    "action": "EXIT",
                    "label": (
                        f"{ticker}: EXIT — Stop verletzt (Last {_px(last)} ≥ Stop {_px(stop)}). "
                        f"Sofort glattstellen (P&L {pnl:+.2f} €)."
                    ),
                    "trail": None,
                    "reason_code": "stop_hit",
                }
        except (TypeError, ValueError):
            pass

    if pnl > 0 and last:
        # Regelbasiertes Nachziehen: NICHT mehr bei jedem Kurs-Update den Stop hinter
        # dem Kurs herschieben (das kann gerade bei gehebelten Produkten eine intakte
        # Position durch normales Intraday-Rauschen aus dem Trade werfen), sondern nur
        # bei einer bestätigten neuen Marktstruktur (Higher Low bei LONG, Lower High
        # bei SHORT, s. _confirmed_swing_trail()).
        swing = _confirmed_swing_trail(pos)
        risk_eur = abs(float(pos.get("risk_eur") or 0.0))
        breakeven_done = bool(pos.get("breakeven_secured"))
        stop_f = float(stop) if stop is not None else None
        still_at_risk = stop_f is None or (
            (stop_f < float(entry)) if side == "LONG" else (stop_f > float(entry))
        )
        # Close Preparation (s. session_status()/CLOSE_FINAL_MINUTES): Trailing wird
        # bewusst verschärft - statt bis zu +1R zu warten, wird JEDE offene Position mit
        # Gewinn sofort auf Breakeven gesichert. Close ist Risikoreduktionsphase, kein
        # Raum mehr, um auf die nächste Struktur-Bestätigung zu warten.
        breakeven_r_threshold = 0.0 if close_subphase == "prep" else TRAIL_BREAKEVEN_MIN_R

        if swing is not None:
            trail = swing["level"]
            pos["breakeven_secured"] = True  # Struktur-Trail liegt ohnehin jenseits von Breakeven
            trail_reason = f"{swing['structure_label']} bestätigt @ {_px(swing['swing_price'])}"
        elif (
            not breakeven_done
            and still_at_risk
            and risk_eur > 0
            and pnl >= risk_eur * breakeven_r_threshold
        ):
            # Einmaliger Sicherheitsschritt, bevor die erste bestätigte Struktur vorliegt:
            # ab einem vollen R Gewinn auf Breakeven sichern, statt bis zur ersten Swing-
            # Bestätigung das komplette Anfangsrisiko offen zu lassen. In Close
            # Preparation entfällt die R-Schwelle komplett (s.o.).
            trail = float(entry)
            pos["breakeven_secured"] = True
            trail_reason = (
                "Close Preparation: Breakeven sofort gesichert" if close_subphase == "prep"
                else f"Breakeven gesichert (+{pnl / risk_eur:.1f}R erreicht)"
            )
        else:
            trail = None
            trail_reason = None

        if trail is not None:
            if scenario_ok:
                # ADD nur in MID, nur nach bestätigter Struktur, nur wenn das
                # Scale-in-Limit (MAX_SCALE_INS) noch nicht erreicht ist.
                adds = scale_in_count(ticker, side)
                if mode == "MID" and swing is not None and adds < MAX_SCALE_INS:
                    return {
                        "action": "ADD",
                        "label": (
                            f"{ticker}: ADD — {trail_reason}, Plus {pnl:+.2f} €. "
                            f"Ein Nachkauf mit eigenem Stop-Risiko, Stop nicht lockern "
                            f"(→ {_px(trail)}). Scale-ins {adds}/{MAX_SCALE_INS}."
                        ),
                        "trail": trail,
                        "reason_code": "add_mid_trail",
                    }
                return {
                    "action": "TRAIL",
                    "label": (
                        f"{ticker}: TRAIL — {trail_reason}, Plus {pnl:+.2f} €. "
                        f"Stop → {_px(trail)} (Entry {_px(entry)}, Last {_px(last)})."
                    ),
                    "trail": trail,
                    "reason_code": "trail_profit",
                }
            return {
                "action": "HOLD",
                "label": (
                    f"{ticker}: HOLD — Plus {pnl:+.2f} €, kein Gegensignal. "
                    f"Stop sichern ({trail_reason}, jetzt {_px(stop)}, Vorschlag {_px(trail)})."
                ),
                "trail": trail,
                "reason_code": "hold_profit",
            }
        return {
            "action": "HOLD",
            "label": (
                f"{ticker}: HOLD — Plus {pnl:+.2f} €, noch keine bestätigte neue Struktur "
                f"seit Kauf. Kein Nachkauf ohne Higher Low / Lower High. Stop bleibt bei {_px(stop)}."
            ),
            "trail": None,
            "reason_code": "hold_profit_no_structure",
        }
    if pnl < 0 and not scenario_ok:
        return {
            "action": "REDUCE",
            "label": (
                f"{ticker}: REDUCE — Minus {pnl:+.2f} € ohne Bestätigung. "
                f"Reduzieren der Position prüfen (Last {_px(last)}, Stop {_px(stop)})."
            ),
            "trail": stop,
            "reason_code": "reduce_unconfirmed",
        }
    return {
        "action": "HOLD",
        "label": (
            f"{ticker}: HOLD — Setup prüfen, nicht automatisch nachlegen "
            f"(P&L {pnl:+.2f} €, Stop {_px(stop)})."
        ),
        "trail": stop,
        "reason_code": "hold_default",
    }


def auto_close_due_positions(watch: pd.DataFrame):
    """Schließt nur Positionen, deren eigener Markt die Final-Close-Unterphase erreicht
    hat (letzte CLOSE_FINAL_MINUTES vor Handelsschluss) - nicht schon zu Beginn der
    Close-Vorbereitung. Vorher wurde bereits beim vollen "Puffer vor Schluss"
    (flatten_buffer_min, Default 60 Min) sofort zwangsweise glattgestellt - das ließ
    keine Zeit für reine Risikoreduktion (Stops verschärfen, Positionen prüfen, ohne
    sofort alles zu verkaufen). Genau das übernimmt jetzt die "Close Preparation"-Phase
    davor (s. session_status()); der harte Zwangs-Close bleibt der kurzen "Final Close"-
    Phase am Ende vorbehalten.

    Wichtig: Ein Xetra-Close darf niemals eine noch laufende US-Position schließen.
    """
    if not st.session_state.get("auto_flatten", True) or not st.session_state.positions:
        return []
    now = datetime.now(TZ_BERLIN)
    buffer_min = int(st.session_state.get("flatten_buffer_min", 60))
    final_buffer = timedelta(minutes=min(CLOSE_FINAL_MINUTES, buffer_min))
    due = []
    for pos in list(st.session_state.positions):
        meta = find_meta(pos.get("ticker", "")) or catalog_lookup(pos.get("ticker", ""))
        yf_sym = (meta or {}).get("yf") or pos.get("ticker", "")
        close_t, venue = session_close_for(yf_sym)
        if now.weekday() >= 5 or is_market_holiday(venue, now.date()):
            due.append(pos)
            continue
        close_at = datetime.combine(now.date(), close_t, tzinfo=TZ_BERLIN)
        final_at = close_at - final_buffer
        if now >= final_at:
            due.append(pos)
    if not due:
        return []
    closed = []
    for pos in due:
        if pos not in st.session_state.positions:
            continue
        st.session_state.closed_pnl += float(pos.get("pnl", 0.0) or 0.0)
        record_fill(pos, "session-close")
        closed.append(pos.get("ticker", ""))
    st.session_state.positions = [p for p in st.session_state.positions if p not in due]
    if closed:
        save_app_state()
    return closed


