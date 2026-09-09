# VoltDesk modular component — extracted from app.py v0.9.35
# This first-stage split uses a context binder for safe, incremental refactoring.
from __future__ import annotations

import streamlit as st


def bind_context(context: dict) -> None:
    """Bind app-level dependencies during the transition to modular architecture."""
    globals().update(context)

def _universe_ticker_count() -> int:
    """Anzahl eindeutiger Ticker im gesamten UNIVERSE-Katalog, unabhängig von
    Kategorie-/Region-Verschachtelung. Einmalig beim Modul-Import berechnet
    (UNIVERSE ändert sich nicht zur Laufzeit)."""
    seen = set()
    for _category in UNIVERSE.values():
        for _region_list in _category.values():
            for _item in _region_list:
                seen.add(_item["ticker"])
    return len(seen)


def current_capital() -> float:
    """Tatsächliche Kapitalbasis des laufenden Paper-Tages. Vorher wurde überall die
    fixe Konstante CAPITAL (10.000 €) direkt verwendet - dadurch sprang der Cash-Bestand
    bei jedem neuen Paper-Tag unabhängig vom bisherigen Ergebnis wieder auf 10.000 €
    zurück, statt Gewinn/Verlust vom Vortag zu übernehmen (s. _reset_paper_day_state()).
    """
    return float(st.session_state.get("account_base", CAPITAL))


def _safe_pct(numerator: float, base: float) -> float:
    """Prozent-Kennzahl mit Schutz vor Kapitalbasis <= 0. Ohne diese Absicherung führt
    ein bei gehebelten Positionen leergehandeltes oder negatives Konto zu einem
    ZeroDivisionError (Crash) bzw. bei negativer Basis zu einem invertierten Vorzeichen,
    das einen Totalverlust fälschlich als Gewinn anzeigen würde."""
    if base <= 0:
        return -100.0 if numerator < 0 else 0.0
    return numerator / base * 100


def get_build() -> str:
    return BUILD_TIMESTAMP


def _normalize_mood(mood) -> str:
    """'😤 Frustrated' / 'Frustrated' / 'frustrated' -> 'frustrated'."""
    if not mood:
        return ""
    text = str(mood).strip().lower()
    for ch in ("😌", "🤔", "😟", "🚀", "😤", "😴"):
        text = text.replace(ch, "")
    text = text.strip()
    aliases = {
        "calm": "calm",
        "neutral": "neutral",
        "anxious": "anxious",
        "euphoric": "euphoric",
        "frustrated": "frustrated",
        "tired": "tired",
        "ängstlich": "anxious",
        "frustriert": "frustrated",
        "müde": "tired",
        "euphorisch": "euphoric",
        "ruhig": "calm",
    }
    return aliases.get(text, text)


def _fill_is_closed(fill: dict) -> bool:
    action = str(fill.get("action") or "").strip().lower()
    if action in {"open", "kauf"}:
        return False
    if action in {"close", "verkauf", "teilverkauf", "teilverkauf 50%", "flatten"}:
        return True
    # Fallback: abgeschlossene Fills haben i.d.R. ein reales P&L
    try:
        return fill.get("pnl") is not None and action != "open"
    except Exception:
        return False


def detect_tilt(fills: list | None = None, lookback: int | None = None,
                current_mood: str | None = None) -> dict:
    """Erkennt Revenge-/Tilt-Muster aus Mood + Verlustserie.

    Score 0-100. Schwellen kommen aus session_state (Settings), mit Defaults.
    """
    fills = fills if fills is not None else list(st.session_state.get("fills") or [])
    lookback = int(lookback or st.session_state.get("tilt_lookback", TILT_LOOKBACK_DEFAULT) or TILT_LOOKBACK_DEFAULT)
    loss_rate_thr = float(st.session_state.get("tilt_loss_rate_threshold", TILT_LOSS_RATE_DEFAULT)) / 100.0
    consec_thr = int(st.session_state.get("tilt_consecutive_losses", TILT_CONSEC_DEFAULT))
    warn_thr = int(st.session_state.get("tilt_warn_threshold", TILT_WARN_DEFAULT))
    lock_thr = int(st.session_state.get("tilt_lock_threshold", TILT_LOCK_DEFAULT))
    enabled = bool(st.session_state.get("tilt_enabled", True))

    empty = {
        "tilt_score": 0,
        "reasons": [],
        "tilt_detected": False,
        "suggested_action": None,
        "consecutive_losses": 0,
        "loss_rate": 0.0,
        "neg_mood_count": 0,
        "enabled": enabled,
    }
    if not enabled:
        return empty

    closed = [f for f in fills if _fill_is_closed(f)]
    if len(closed) < 2 and not current_mood:
        return empty

    recent = closed[: max(lookback, 1)]  # fills sind newest-first
    losses = 0
    consecutive_losses = 0
    still_losing = True
    recent_moods = []
    loss_sum = 0.0
    times = []

    for trade in recent:
        try:
            pnl = float(trade.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        mood_n = _normalize_mood(trade.get("mood"))
        if mood_n:
            recent_moods.append(mood_n)
        if pnl < 0:
            losses += 1
            loss_sum += abs(pnl)
            if still_losing:
                consecutive_losses += 1
        else:
            still_losing = False
        t_raw = trade.get("time") or trade.get("timestamp")
        if t_raw:
            times.append(str(t_raw))

    if current_mood:
        cm = _normalize_mood(current_mood)
        if cm:
            recent_moods.append(cm)

    reasons = []
    tilt_score = 0
    n = max(len(recent), 1)
    loss_rate = losses / n if recent else 0.0

    if recent and loss_rate >= loss_rate_thr:
        tilt_score += 30
        reasons.append(f"Hohe Verlustrate ({loss_rate * 100:.0f}% der letzten {len(recent)})")

    if consecutive_losses >= consec_thr:
        tilt_score += 25
        reasons.append(f"{consecutive_losses} Verluste in Folge")
    elif consecutive_losses >= max(consec_thr - 1, 2):
        tilt_score += 12
        reasons.append(f"{consecutive_losses} Verluste in Folge (Vorstufe)")

    neg_mood_count = sum(1 for m in recent_moods if m in TILT_NEGATIVE_MOODS)
    if neg_mood_count >= 2:
        tilt_score += 20
        reasons.append(f"Negative Stimmung ({neg_mood_count}× Frustrated/Anxious/Tired)")
    elif neg_mood_count == 1 and consecutive_losses >= 2:
        tilt_score += 12
        reasons.append("Verlustserie bei negativer Stimmung")

    last_mood = recent_moods[-1] if recent_moods else ""
    if last_mood in TILT_NEGATIVE_MOODS and consecutive_losses >= 2:
        tilt_score += 10
        reasons.append(f"Aktuelle Stimmung: {last_mood}")
    if last_mood == "euphoric" and consecutive_losses >= 2:
        tilt_score += 15
        reasons.append("Euphorie nach Verlustserie (Overconfidence/Revenge)")

    cap = current_capital() if "current_capital" in globals() else float(st.session_state.get("account_base") or 10000)
    avg_loss = (loss_sum / losses) if losses else 0.0
    if losses and cap > 0 and avg_loss > cap * 0.01:
        tilt_score += 15
        reasons.append(f"Ø Verlust {avg_loss:.0f} € ({avg_loss / cap * 100:.1f}% Kapital)")
    elif avg_loss > 100:
        tilt_score += 8
        reasons.append(f"Ø Verlust {avg_loss:.0f} €")

    # Burst-Trading: mehrere Closes in kurzem Fenster (nur HH:MM:SS am selben Tag)
    if len(times) >= 4:
        try:
            parsed = []
            for t in times[:6]:
                part = t.replace("T", " ").split(" ")[-1][:8]
                parsed.append(datetime.strptime(part, "%H:%M:%S"))
            span = (max(parsed) - min(parsed)).total_seconds()
            if span < 0:
                # Mitternachts-Ueberlauf: Fills speichern nur HH:MM:SS - ohne den
                # Ausgleich wuerden z.B. 23:59 -> 00:01 als negativer Span (also
                # "kein Burst") gewertet, obwohl die Closes nur 2 Min. auseinanderlagen.
                span += 24 * 3600
            if span <= 20 * 60:
                tilt_score += 10
                reasons.append(f"{len(parsed)} Closes in {int(span // 60) + 1} Min.")
        except Exception:
            pass

    tilt_score = int(min(tilt_score, 100))
    suggested = None
    if tilt_score >= lock_thr:
        suggested = "lock"
    elif tilt_score >= warn_thr:
        suggested = "pause"
    elif tilt_score >= max(warn_thr - 20, 25):
        suggested = "warn"

    return {
        "tilt_score": tilt_score,
        "reasons": reasons,
        "tilt_detected": tilt_score >= warn_thr,
        "suggested_action": suggested,
        "consecutive_losses": consecutive_losses,
        "loss_rate": loss_rate,
        "neg_mood_count": neg_mood_count,
        "enabled": enabled,
    }


def apply_tilt_lock_if_needed(tilt: dict) -> None:
    """Setzt tilt_lock bei Score >= Lock-Schwelle (einmalig, persistiert)."""
    if not tilt or not tilt.get("enabled"):
        return
    if tilt.get("suggested_action") == "lock" and not st.session_state.get("tilt_lock"):
        st.session_state.tilt_lock = True
        st.session_state.tilt_lock_reason = " · ".join(tilt.get("reasons") or [])
        st.session_state.tilt_lock_score = tilt.get("tilt_score")


def _log_tilt_unlock(note: str) -> None:
    """Manuelles Aufheben des Tilt-Locks jetzt mit Pflichtnotiz + Log (2026-09):
    vorher ein Ein-Klick-Button ohne jede Spur - jetzt landet jede Aufhebung mit
    Zeitstempel, Score, Sperrgrund und der eingetragenen Begründung im Log, damit
    sich später nachvollziehen lässt, wie oft/warum entsperrt wurde."""
    log = list(st.session_state.get("tilt_unlock_log") or [])
    log.append({
        "at": datetime.now(TZ_BERLIN).isoformat(),
        "score": st.session_state.get("tilt_lock_score"),
        "lock_reason": st.session_state.get("tilt_lock_reason"),
        "note": (note or "").strip(),
    })
    st.session_state.tilt_unlock_log = log[-50:]  # kein unbegrenztes Wachstum
    st.session_state.tilt_lock = False
    st.session_state.tilt_lock_reason = None
    st.session_state.tilt_lock_score = None
    save_app_state()


def buy_halt_reason() -> Optional[str]:
    """UI-Gate für neue Positionen (2026-09): kurze Sperr-Meldung, falls Kill Switch,
    Risk Lock, Trading Pause, Tilt-Lock oder ein fehlender Tages-Setup-Vertrag aktiv
    sind. Nutzt dieselben Flags wie die harten Prüfungen in execute_paper_buy() -
    hier nur, damit Kauf-Buttons und der Bestätigungsdialog schon VORHER sichtbar
    tot sind, statt einen aktiven Button zu zeigen, der erst beim Klick ablehnt."""
    if st.session_state.get("killed", False):
        return "Kill Switch aktiv"
    if st.session_state.get("trading_pause", False):
        return "Trading Pause aktiv"
    if st.session_state.get("risk_lock", False):
        return "Daily Loss Limit / Risk Lock aktiv"
    if st.session_state.get("tilt_enabled", True) and st.session_state.get("tilt_lock", False):
        why = st.session_state.get("tilt_lock_reason") or "Tilt-Score über der Sperrschwelle"
        return f"Tilt-Lock aktiv ({why})"
    if not get_daily_setup_contract():
        return "Kein Tages-Setup gewählt (siehe Hinweis oben)"
    return None


def effective_max_leverage() -> float:
    """Ohne Pro Modus bleibt der Hebel-Cap bei 5x - 8x ist bereits sehr hoch und wird nur
    im Pro Modus freigeschaltet, analog zu den anderen Pro-Modus-Guardrails."""
    return MAX_LEVERAGE if effective_pro_mode() else DEFAULT_LEVERAGE_CAP


def get_sector(ticker: str) -> str:
    return SECTOR_MAP.get(ticker, "Sonstige")


def sector_confirmation(ticker: str, side: str) -> dict:
    """Sektor-Konfirmation als Fake-Breakout-Filter: bewegt sich der Sektor (via
    SPDR-ETF) am selben Tag in dieselbe Richtung wie der Titel? Wenn nicht, ist ein
    Breakout/Breakdown häufiger ein Fehlausbruch (nur Einzeltitel-getrieben, ohne
    breitere Bestätigung) - Standard-Erklärung für 'Fake-Breakouts'."""
    sector = get_sector(ticker)
    bench = SECTOR_BENCH.get(sector)
    if not bench:
        return {"available": False, "confirmed": None, "sector": sector, "sector_chg": None}
    q = fetch_quote(bench)
    sector_chg = q.get("chg") if q.get("ok") else None
    if sector_chg is None:
        return {"available": False, "confirmed": None, "sector": sector, "sector_chg": None}
    is_long = side == "LONG"
    confirmed = (sector_chg >= 0.15) if is_long else (sector_chg <= -0.15)
    return {"available": True, "confirmed": confirmed, "sector": sector, "sector_chg": sector_chg}


def _current_breakout_side(intra: pd.DataFrame, levels: dict) -> Optional[str]:
    """Einfache Breakout-Erkennung fürs Sektor-Bestätigungs-Badge (2026-09, 'Option C'):
    aktueller Schlusskurs über PDH -> 'LONG', unter PDL -> 'SHORT', sonst None. Bewusst
    simpel (kein Pattern-Scoring wie die Empfehlungs-Engine) - das Badge ist ein
    Hinweis, kein Signal, und soll immer sichtbar sein, ohne die restliche
    Recommendation-Logik zu duplizieren."""
    if intra is None or intra.empty:
        return None
    try:
        last_close = float(intra["Close"].iloc[-1])
    except (TypeError, ValueError, IndexError):
        return None
    pdh = levels.get("prev_high")
    pdl = levels.get("prev_low")
    if pdh and last_close > float(pdh):
        return "LONG"
    if pdl and last_close < float(pdl):
        return "SHORT"
    return None


def get_position_limit_pct() -> float:
    """Aktuelles Positionslimit in % vom Cash.

    Liefert 100% (= faktisch kein Limit), wenn der Schalter in der Sidebar
    das Limit deaktiviert hat, sonst den eingestellten Slider-Wert
    (0-10%, Default MAX_POSITION_PCT)."""
    if not st.session_state.get("pos_limit_enabled", True):
        return 100.0
    return float(st.session_state.get("pos_limit_pct", MAX_POSITION_PCT))


def all_universe_items():
    items = []
    for category, subgroup in UNIVERSE.items():
        for subgroup_key, rows in subgroup.items():
            for row in rows:
                if category == "Indizes":
                    # s. INDEX_TICKER_REGION-Kommentar bei UNIVERSE - "Indizes" ist
                    # keine Region, subgroup_key ("USA"/"Europa") keine Index-Bezeichnung.
                    region = INDEX_TICKER_REGION.get(row["ticker"], "USA")
                    index_label = "Index"
                else:
                    region, index_label = category, subgroup_key
                items.append({**row, "region": region, "index": index_label})
    return items


def _universe_index() -> dict:
    """Einmalig gebauter Lookup (ticker/yf -> Katalogeintrag) plus Region-Index
    (ticker -> (region, index)). Ersetzt die bisherigen O(n)-Linearscans in
    catalog_lookup()/find_region_index() - beide wurden mehrfach pro Refresh
    aufgerufen, find_region_index() zusaetzlich in Schleifen (z.B. Sektor-
    Peering bis zu 8x pro Ticker). Der Katalog aendert sich zur Laufzeit nie,
    daher reicht der Lazy-Build beim ersten Zugriff."""
    global _UNIVERSE_INDEX, _UNIVERSE_REGION_INDEX
    if _UNIVERSE_INDEX is None:
        _UNIVERSE_INDEX = {}
        _UNIVERSE_REGION_INDEX = {}
        for item in all_universe_items():
            _UNIVERSE_INDEX[item["ticker"].upper()] = item
            _UNIVERSE_INDEX[item["yf"].upper()] = item
            _UNIVERSE_REGION_INDEX[item["ticker"].upper()] = (item["region"], item["index"])
    return _UNIVERSE_INDEX


def catalog_lookup(ticker: str):
    ticker = (ticker or "").upper()
    return _universe_index().get(ticker)


def is_week_end():
    now = datetime.now(TZ_BERLIN)
    return now.weekday() == 4 and now.hour >= 22


def is_month_start():
    return datetime.now(TZ_BERLIN).day == 1


def is_year_start():
    now = datetime.now(TZ_BERLIN)
    return now.month == 1 and now.day == 1


def get_week_bounds(ref_date=None):
    d = ref_date or datetime.now(TZ_BERLIN)
    mon = d - timedelta(days=d.weekday())
    fri = mon + timedelta(days=4)
    return mon.strftime("%Y-%m-%d"), fri.strftime("%Y-%m-%d")


def get_prev_month_bounds():
    now = datetime.now(TZ_BERLIN)
    return (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)


def get_prev_year():
    return datetime.now(TZ_BERLIN).year - 1


def build_week_report():
    mon, fri = get_week_bounds()
    df = get_trades_for_week(mon, fri, current_user_id())
    if df.empty: return None
    # Gespeicherte Tagesberichte (build_day_report()-Kennzahlen, per "Handelstag
    # abschließen" in day_reports gesichert) für diese Woche laden - get_day_reports_for_
    # week() existierte bereits, wurde aber nirgends aufgerufen. Die Tagesübersicht im
    # Wochenbericht rechnete die Kennzahlen bisher komplett neu aus den Rohtrades und
    # ließ dabei day_pct/Best/Worst unter den Tisch fallen, obwohl der jeweilige
    # Tagesbericht diese Werte längst und korrekt (inkl. Gebühren-Vorwegnahme etc.)
    # berechnet hatte.
    saved_reports = {r["date"]: r for r in get_day_reports_for_week(mon, fri, current_user_id())}
    days = sorted(df["date"].unique())
    day_summaries = []; total_pnl = 0.0; total_fees = 0.0; total_trades = 0; wins = 0
    for day in days:
        day_df = df[df["date"] == day]
        day_trades = len(day_df); day_wins = int((day_df["pnl"] > 0).sum())
        saved = saved_reports.get(day)
        if saved:
            day_fees = float(saved.get("fees", day_df["fees"].sum()))
            day_gross = float(saved.get("gross_pnl", day_df["pnl"].sum()))
            day_net = float(saved.get("net_pnl", saved.get("day_pnl", day_gross - day_fees)))
            day_pct = float(saved.get("day_pct", 0.0))
            day_best = float(saved.get("best", day_df["pnl"].max() if day_trades else 0.0))
            day_worst = float(saved.get("worst", day_df["pnl"].min() if day_trades else 0.0))
            day_win_rate = float(saved.get("win_rate", (day_wins / day_trades * 100) if day_trades else 0.0))
        else:
            day_gross = float(day_df["pnl"].sum())
            day_fees = float(day_df["fees"].sum())
            day_net = day_gross - day_fees
            day_pct = 0.0
            day_best = float(day_df["pnl"].max()) if day_trades else 0.0
            day_worst = float(day_df["pnl"].min()) if day_trades else 0.0
            day_win_rate = (day_wins / day_trades * 100) if day_trades > 0 else 0.0
        total_pnl += day_gross; total_fees += day_fees; total_trades += day_trades; wins += day_wins
        day_summaries.append({"date": day, "trades": day_trades, "pnl": day_net, "gross_pnl": day_gross, "fees": day_fees,
            "pct": day_pct, "best": day_best, "worst": day_worst,
            "win_rate": day_win_rate,
            "fills": day_df[["ticker","side","action","pnl","fees","reason"]].to_dict("records")})
    pnl_s = df["pnl"].astype(float)
    gp = float(pnl_s[pnl_s > 0].sum()); gl = float(abs(pnl_s[pnl_s < 0].sum()))
    pf = gp/gl if gl > 0 else float("inf")
    return {"week_start": mon, "week_end": fri, "days": day_summaries, "total_trades": total_trades,
        "total_pnl": total_pnl, "total_fees": total_fees, "gross_pnl": total_pnl, "net_pnl": total_pnl - total_fees,
        "win_rate": (wins/total_trades*100) if total_trades > 0 else 0, "profit_factor": pf,
        "avg_win": float(pnl_s[pnl_s > 0].mean()) if (pnl_s > 0).any() else 0,
        "avg_loss": float(pnl_s[pnl_s < 0].mean()) if (pnl_s < 0).any() else 0,
        "max_win": float(pnl_s.max()), "max_loss": float(pnl_s.min()),
        "time": datetime.now(TZ_BERLIN).strftime("%d.%m.%Y %H:%M")}


def build_month_report():
    year, month = get_prev_month_bounds()
    ym = f"{year:04d}-{month:02d}"
    df = get_trades_for_month(year, month, current_user_id())
    if df.empty: return None
    df["week"] = pd.to_datetime(df["date"]).dt.isocalendar().week
    weeks = sorted(df["week"].unique())
    week_summaries = []; total_pnl = 0.0; total_fees = 0.0; total_trades = 0; wins = 0
    for week in weeks:
        wdf = df[df["week"] == week]
        wp = float(wdf["pnl"].sum()); wf = float(wdf["fees"].sum())
        wt = len(wdf); ww = int((wdf["pnl"] > 0).sum())
        total_pnl += wp; total_fees += wf; total_trades += wt; wins += ww
        week_summaries.append({"week": int(week), "trades": wt, "pnl": wp, "fees": wf,
            "win_rate": (ww/wt*100) if wt > 0 else 0})
    pnl_s = df["pnl"].astype(float)
    gp = float(pnl_s[pnl_s > 0].sum()); gl = float(abs(pnl_s[pnl_s < 0].sum()))
    pf = gp/gl if gl > 0 else float("inf")
    return {"year_month": ym, "month_name": datetime(year, month, 1).strftime("%B %Y"),
        "weeks": week_summaries, "total_trades": total_trades, "total_pnl": total_pnl,
        "total_fees": total_fees, "net_pnl": total_pnl - total_fees,
        "win_rate": (wins/total_trades*100) if total_trades > 0 else 0, "profit_factor": pf,
        "time": datetime.now(TZ_BERLIN).strftime("%d.%m.%Y %H:%M")}


def build_year_report():
    year = get_prev_year()
    df = get_trades_for_year(year, current_user_id())
    if df.empty: return None
    df["month"] = pd.to_datetime(df["date"]).dt.month
    months = sorted(df["month"].unique())
    month_summaries = []; total_pnl = 0.0; total_fees = 0.0; total_trades = 0; wins = 0
    for m in months:
        mdf = df[df["month"] == m]
        mp = float(mdf["pnl"].sum()); mf = float(mdf["fees"].sum())
        mt = len(mdf); mw = int((mdf["pnl"] > 0).sum())
        total_pnl += mp; total_fees += mf; total_trades += mt; wins += mw
        month_summaries.append({"month": int(m), "month_name": datetime(year, int(m), 1).strftime("%B"),
            "trades": mt, "pnl": mp, "fees": mf,
            "win_rate": (mw/mt*100) if mt > 0 else 0})
    pnl_s = df["pnl"].astype(float)
    gp = float(pnl_s[pnl_s > 0].sum()); gl = float(abs(pnl_s[pnl_s < 0].sum()))
    pf = gp/gl if gl > 0 else float("inf")
    return {"year": year, "months": month_summaries, "total_trades": total_trades,
        "total_pnl": total_pnl, "total_fees": total_fees, "gross_pnl": total_pnl, "net_pnl": total_pnl - total_fees,
        "win_rate": (wins/total_trades*100) if total_trades > 0 else 0, "profit_factor": pf,
        "avg_win": float(pnl_s[pnl_s > 0].mean()) if (pnl_s > 0).any() else 0,
        "avg_loss": float(pnl_s[pnl_s < 0].mean()) if (pnl_s < 0).any() else 0,
        "max_win": float(pnl_s.max()), "max_loss": float(pnl_s.min()),
        "time": datetime.now(TZ_BERLIN).strftime("%d.%m.%Y %H:%M")}


def find_region_index(ticker: str) -> tuple[str, str]:
    """Find region/index in the fixed universe first, then in watchlist metadata.

    Manually added tickers are not necessarily part of UNIVERSE, so their stored
    watchlist metadata is the authoritative fallback for UI/ranker classification.
    """
    ticker = (ticker or "").upper().strip()
    # S. INDEX_TICKER_REGION-Kommentar oben - "Indizes" ist keine Region; der
    # Index traegt die Aufloesung ueber INDEX_TICKER_REGION bereits mit sich.
    hit = _universe_index() and _UNIVERSE_REGION_INDEX.get(ticker)
    if hit:
        return hit
    for w in st.session_state.get("watchlist") or []:
        if str(w.get("ticker", "")).upper() == ticker:
            return w.get("region") or "—", w.get("index") or "—"
    return "—", "—"


def color_1d(val):
    if pd.isna(val):
        return ""
    if val >= 3:
        return "background-color: #1a5c1a; color: white; font-weight: bold;"
    elif val >= 1.5:
        return "background-color: #2d8a2d; color: white; font-weight: bold;"
    elif val > 0:
        return "background-color: #4db84d; color: white; font-weight: bold;"
    elif val <= -3:
        return "background-color: #8a1a1a; color: white; font-weight: bold;"
    elif val <= -1.5:
        return "background-color: #b82d2d; color: white; font-weight: bold;"
    elif val < 0:
        return "background-color: #e06666; color: white; font-weight: bold;"
    return ""


@st.cache_data(ttl=30, show_spinner=False)
def _fmt_compact_number(x) -> str:
    """Kompakte Anzeige für große Zahlen (Market Cap, Volumen) - 1_234_000_000 -> '1.2B'.
    Bewusst als vorformatierter String statt column_config-NumberColumn-Format (s.
    Begründung an der Render-Stelle in dashboard())."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "—"
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= div:
            return f"{x/div:.1f}{suf}"
    return f"{x:.0f}"


def rec_key(ticker: str, rec: dict) -> str:
    prod = rec.get("product") or {}
    wkn = prod.get("WKN") or prod.get("Typ") or "NA"
    return f"{ticker}|{rec.get('side')}|{wkn}"


def session_close_for(yf_symbol: Optional[str]):
    """Schlusszeit (CET) und Venue anhand Yahoo-Suffix."""
    sym = str(yf_symbol or "")
    if sym.endswith(".L"):
        # LSE 16:30 London ≈ 17:30 CET (Winter/Sommer jeweils 1h Differenz)
        return time(17, 30), "LSE"
    # Alle kontinentaleuropäischen Börsenplätze (Xetra sowie Euronext Paris/
    # Amsterdam/Brüssel/Lissabon, Madrid, Mailand) haben praktisch identische
    # Handelszeiten (ca. 09:00-17:30 CET) und werden deshalb einheitlich unter der
    # Venue "Xetra" geführt. Vorher wurde nur ".DE" erkannt - dadurch fielen die
    # neu ergänzten EURO STOXX 50-/Euronext 100-Titel (.PA/.AS/.BR/.MC/.MI/.LS)
    # fälschlich in den US-Zweig unten, mit falschen Handelszeiten und falscher
    # Phasenanzeige (PRE/OPEN/MID/CLOSE) für diese Titel.
    eu_suffixes = (".DE", ".PA", ".AS", ".BR", ".LS", ".MC", ".MI", ".SW")
    if sym.endswith(eu_suffixes):
        return time(17, 30), "Xetra"
    return time(22, 0), "US Regular"


def session_windows(venue: str):
    """Handelsfenster in Europe/Berlin-Zeit."""
    if venue == "Xetra":
        return {
            "pre": (time(0, 0), time(9, 0)),
            "open": (time(9, 0), time(10, 30)),
            "mid": (time(10, 30), time(17, 20)),
            "close": (time(17, 20), time(17, 30)),
        }
    if venue == "LSE":
        return {
            "pre": (time(0, 0), time(9, 0)),
            "open": (time(9, 0), time(10, 30)),
            "mid": (time(10, 30), time(17, 20)),
            "close": (time(17, 20), time(17, 30)),
        }
    return {
        "pre": (time(0, 0), time(15, 30)),
        "open": (time(15, 30), time(16, 30)),
        "mid": (time(16, 30), time(21, 50)),
        "close": (time(21, 50), time(22, 0)),
    }


def is_market_holiday(venue: str, d) -> bool:
    key = d.isoformat() if hasattr(d, "isoformat") else str(d)
    return key in MARKET_HOLIDAYS.get(venue, set())


def detect_mode(now: datetime, venue: str, flatten_at: Optional[datetime] = None) -> str:
    """Session-Modus anhand Wochentag, Feiertag und Börsenfenster.

    Nach 00:00 auf Werktagen bis Open = PRE. CLOSE am Abend und am Wochenende
    sowie an Börsenfeiertagen des Fokus-Venues.
    """
    if now.weekday() >= 5:
        return "CLOSE"
    if is_market_holiday(venue, now.date()):
        return "CLOSE"
    t = now.time()
    windows = session_windows(venue)
    open0, open1 = windows["open"]
    mid0, mid1 = windows["mid"]

    if open0 <= t < open1:
        return "OPEN"
    if mid0 <= t < mid1:
        if flatten_at is not None and now >= flatten_at:
            return "CLOSE"
        return "MID"
    if t >= mid1:
        return "CLOSE"
    return "PRE"


def get_daily_setup_contract() -> Optional[dict]:
    """Liefert den Tages-Setup-Vertrag, falls er HEUTE gesetzt wurde - sonst None
    (auch wenn noch ein Vertrag vom Vortag im Session-State hängt)."""
    contract = st.session_state.get("daily_setup_contract")
    today_str = datetime.now(TZ_BERLIN).date().isoformat()
    if contract and contract.get("date") == today_str:
        return contract
    return None


def close_subphase_label(subphase: Optional[str]) -> str:
    return {"prep": "Close Preparation", "final": "Final Close"}.get(subphase, "Close")


def region_is_trading(region: str) -> bool:
    """True, wenn die Region gerade aktiv handelt (OPEN oder MID) - für die Tür-Icons im
    Katalog (Sidebar). Wochenende/Feiertag/PRE/CLOSE zählen als 'geschlossen'."""
    venue = REGION_VENUE.get(region)
    if not venue:
        return False
    now = datetime.now(TZ_BERLIN)
    if now.weekday() >= 5 or is_market_holiday(venue, now.date()):
        return False
    return detect_mode(now, venue) in ("OPEN", "MID")


def region_status_color(region: str) -> str:
    """Ampel exakt aus der jeweiligen Session ableiten.

    Grün = OPEN/MID, Gelb = CLOSE-Phase, Rot = PRE/geschlossen/Feiertag.
    Damit kann z.B. EU/UK nach 17:30 nicht mehr fälschlich grün erscheinen.
    """
    venue = REGION_VENUE.get(region)
    if not venue:
        return "red"
    now = datetime.now(TZ_BERLIN)
    if now.weekday() >= 5 or is_market_holiday(venue, now.date()):
        return "red"
    mode = detect_mode(now, venue)
    # Region-Ampel: grün nur während des regulären Handels (OPEN/MID).
    # PRE und CLOSE sind geschlossen und werden deshalb rot dargestellt.
    return "green" if mode in ("OPEN", "MID") else "red"


def _watchlist_has_venue(venue_names) -> bool:
    """True, wenn mindestens ein Titel der Watchlist zu einer der genannten Venues gehört -
    Grundlage für den EU-Open-Override in session_status()."""
    for item in st.session_state.get("watchlist") or []:
        _, v = session_close_for(item.get("yf"))
        if v in venue_names:
            return True
    return False


def session_status(yf_symbol: Optional[str]):
    now = datetime.now(TZ_BERLIN)
    close_t, venue = session_close_for(yf_symbol)
    close_at = datetime.combine(now.date(), close_t, tzinfo=TZ_BERLIN)
    buffer = timedelta(minutes=int(st.session_state.get("flatten_buffer_min", 60)))
    flatten_at = close_at - buffer
    auto_mode = detect_mode(now, venue, flatten_at)

    # EU-Open-Override (nur im Auto-Modus): Ist der Fokus-Titel ein US-Wert und die
    # US-Session noch in PRE, aber Xetra/LSE bereits im OPEN-Fenster UND es liegen
    # tatsächlich EU-Titel in der Watchlist - dann soll EU den Modus bestimmen, statt
    # dass man in "US PRE" hängen bleibt, obwohl in Europa längst gehandelt wird.
    # Maßgeblich ist bewusst die Watchlist (nicht nur der Fokus-Titel), da der Fokus
    # zufällig auf einem US-Titel liegen kann, während parallel EU-Titel gehandelt werden.
    override = st.session_state.get("mode_override", "Auto")
    if (
        override == "Auto"
        and venue == "US Regular"
        and auto_mode == "PRE"
        and _watchlist_has_venue({"Xetra", "LSE"})
    ):
        eu_close_t, eu_venue = time(17, 30), "Xetra"
        eu_close_at = datetime.combine(now.date(), eu_close_t, tzinfo=TZ_BERLIN)
        eu_flatten_at = eu_close_at - buffer
        eu_auto_mode = detect_mode(now, eu_venue, eu_flatten_at)
        # Bugfix: vorher nur "OPEN" akzeptiert - dadurch griff der Override z.B. um 11:00
        # Uhr NICHT (Xetra ist dann längst in der MID-Phase, open-Fenster ist nur
        # 09:00-10:30), obwohl der EU-Markt zu dem Zeitpunkt eindeutig aktiv handelt.
        # "MID" zählt daher genauso als "aktiv" wie "OPEN".
        if eu_auto_mode in ("OPEN", "MID"):
            venue, close_at, flatten_at, auto_mode = eu_venue, eu_close_at, eu_flatten_at, eu_auto_mode

    holiday = is_market_holiday(venue, now.date())
    weekend = now.weekday() >= 5
    mode = override if override != "Auto" else auto_mode

    # Struktur-basierter MID-Übergang: reiner Zeitablauf der Opening Range reicht nicht -
    # zusätzlich müssen VWAP, Momentum und Volumen die Marktstruktur bestätigen (s.
    # _mid_structure_signal()), sonst bleibt VoltDesk in der (dann verlängerten) OPEN-
    # Phase. Ein Notfall-Deckel (MID_STRUCTURE_MAX_DELAY_MIN) verhindert ein dauerhaftes
    # Hängenbleiben in OPEN, falls die Struktur nie eindeutig wird.
    mid_structure = None
    if override == "Auto" and mode == "MID" and yf_symbol:
        mid0 = session_windows(venue)["mid"][0]
        mid_start = datetime.combine(now.date(), mid0, tzinfo=TZ_BERLIN)
        minutes_into_mid = (now - mid_start).total_seconds() / 60.0
        if minutes_into_mid < MID_STRUCTURE_MAX_DELAY_MIN:
            mid_structure = _mid_structure_signal(yf_symbol)
            if not mid_structure["ready"]:
                mode = "OPEN"
                auto_mode = "OPEN"

    # Close-Unterphasen (s. CLOSE_FINAL_MINUTES): "prep" = Risiko reduzieren, aber noch
    # kein Zwangs-Close; "final" = letzte Minuten vor Schluss, hier greift der tatsächliche
    # Zwangs-Close in auto_close_due_positions().
    close_subphase = None
    final_at = None
    if mode == "CLOSE":
        effective_final_min = min(CLOSE_FINAL_MINUTES, int(st.session_state.get("flatten_buffer_min", 60)))
        final_at = close_at - timedelta(minutes=effective_final_min)
        close_subphase = "final" if now >= final_at else "prep"

    book = MODE_PLAYBOOK.get(mode, MODE_PLAYBOOK["CLOSE"])
    market_closed_reason = None
    if weekend:
        market_closed_reason = "Wochenende — Börse geschlossen"
    elif holiday:
        market_closed_reason = f"Feiertag ({venue}) — kein regulärer Handel"
    return {
        "now": now,
        "venue": venue,
        "close_at": close_at,
        "flatten_at": flatten_at,
        "final_at": final_at,
        "close_subphase": close_subphase,
        "mid_structure": mid_structure,
        "minutes_left": (flatten_at - now).total_seconds() / 60,
        "past_flatten": now >= flatten_at,
        "past_close": now >= close_at,
        "weekend": weekend,
        "holiday": holiday,
        "market_closed_reason": market_closed_reason,
        "auto_mode": auto_mode,
        "mode": mode,
        "playbook": book,
    }


