# VoltDesk modular component — extracted from app.py v0.9.35
# This first-stage split uses a context binder for safe, incremental refactoring.
from __future__ import annotations

import streamlit as st


def bind_context(context: dict) -> None:
    """Bind app-level dependencies during the transition to modular architecture."""
    globals().update(context)

def add_to_watchlist(ticker: str, yf_symbol: str, name: str) -> bool:
    ticker = ticker.upper().strip()
    yf_symbol = yf_symbol.upper().strip()
    if any(w["ticker"] == ticker for w in st.session_state.watchlist):
        # Bugfix (2026-09): vorher setzte nur der Limit-Fall unten eine
        # watchlist_add_flash-Meldung - manche Aufrufer hatten daher einen
        # eigenen, hart auf "bereits in der Watchlist" codierten else-Zweig,
        # der bei einem Limit-Treffer die (korrekte) Limit-Meldung fälschlich
        # überschrieb. Jetzt setzen BEIDE Fehlerfälle konsistent dieselbe
        # Flash-Meldung, Aufrufer brauchen keine eigene Sonderbehandlung mehr.
        st.session_state["watchlist_add_flash"] = f"{ticker} ist bereits in der Watchlist."
        return False
    # Watchlist-Limit (2026-09): Free 5, Pro 10 Titel - primär aus Performance-
    # Gründen (jeder Titel kostet 3 Netzwerk-Calls pro Refresh, s. Redundanz-
    # Auflösung weiter oben). "Plus" (unbegrenzt) ist als Subscription-Tier noch
    # nicht eingeführt, daher hier noch nicht abgebildet.
    _wl_limit = WATCHLIST_LIMIT_PRO if effective_pro_mode() else WATCHLIST_LIMIT_FREE
    if len(st.session_state.watchlist) >= _wl_limit:
        st.session_state["watchlist_add_flash"] = (
            f"Watchlist-Limit erreicht ({_wl_limit} Titel"
            f"{' im Pro-Modus' if effective_pro_mode() else ' im Free-Modus - mit Pro bis zu ' + str(WATCHLIST_LIMIT_PRO)}"
            f"). Erst einen Titel entfernen, um einen neuen hinzuzufügen."
        )
        return False
    wkn = None
    for prod in PRODUCT_CATALOG:
        if prod["ticker"] == ticker and prod.get("wkn"):
            wkn = prod["wkn"]
            break
    if not wkn:
        # PRODUCT_CATALOG (Onvista-Stammdaten) deckt aktuell nur AMD/OHB ab.
        # Für alle anderen Ticker bisher keinen Fallback -> WKN blieb None und
        # die Watchlist zeigte "—". Gleiche Desk-Modell-Logik wie im
        # Produktkatalog (live_catalog/_model_products) nutzen, damit jeder
        # Ticker zumindest eine synthetische Referenz-WKN erhält.
        modeled = _model_products(ticker)
        standard_long = next(
            (p for p in modeled if p["side"] == "LONG" and p.get("profil") == "Standard"),
            None,
        )
        if standard_long:
            wkn = standard_long["wkn"]
        elif modeled:
            wkn = modeled[0]["wkn"]
    # Persist region/index metadata so manually resolved tickers remain classifiable
    # even when they are not part of the fixed UNIVERSE.
    region, index = find_region_index(ticker)
    if region == "—":
        if yf_symbol.endswith(".L") or ticker.endswith(".L"):
            region, index = "UK", "FTSE 100"
        elif yf_symbol.endswith(".SW") or ticker.endswith(".SW"):
            # Bugfix (2026-09): .SW (Schweizer Börse SIX) stand vorher in der EU-Gruppe -
            # "Schweiz" ist aber ein eigenes Region-Bucket mit eigener Flagge (REGION_FLAG_ONLY).
            region, index = "Schweiz", "SMI"
        elif any(yf_symbol.endswith(sfx) or ticker.endswith(sfx) for sfx in (".DE", ".PA", ".AS", ".MI", ".MC", ".BR")):
            region, index = "EU", "DAX"
        else:
            region, index = "USA", "S&P 500"
    st.session_state.watchlist.append(
        {"ticker": ticker, "yf": yf_symbol, "name": name, "wkn": wkn, "region": region, "index": index}
    )
    st.session_state.focus = ticker
    save_app_state()
    return True


def remove_from_watchlist(ticker: str):
    st.session_state.watchlist = [
        w for w in st.session_state.watchlist if w["ticker"] != ticker
    ]
    if st.session_state.focus == ticker:
        st.session_state.focus = (
            st.session_state.watchlist[0]["ticker"] if st.session_state.watchlist else None
        )
    save_app_state()


@st.cache_data(ttl=90, show_spinner=False)
def resolve_symbol(raw: str):
    raw = (raw or "").strip().upper()
    if not raw:
        return None
    known = catalog_lookup(raw)
    if known:
        return {"ticker": known["ticker"], "yf": known["yf"], "name": known["name"]}
    candidates = [raw]
    if "." not in raw:
        candidates.append(f"{raw}.DE")
    for symbol in candidates:
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="5d", interval="1d")
            if hist.empty:
                continue
            name = symbol
            try:
                info = t.fast_info
                name = getattr(info, "shortName", None) or name
            except Exception:
                pass
            if name == symbol:
                try:
                    info = t.info or {}
                    name = info.get("shortName") or info.get("longName") or symbol
                except Exception:
                    name = symbol
            ticker = symbol.split(".")[0]
            return {"ticker": ticker, "yf": symbol, "name": name}
        except Exception:
            continue
    return None


def _valid_ohlc(hist: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Verwirft unfertige Tageskerzen (typisch .DE vor Xetra-Open: Close=NaN)."""
    if hist is None or hist.empty:
        return pd.DataFrame()
    need = [c for c in ("Open", "High", "Low", "Close") if c in hist.columns]
    if not need:
        return pd.DataFrame()
    return hist.dropna(subset=need)


def _finnhub_api_key() -> Optional[str]:
    """Optionaler Fallback-Datenfeed, falls Yahoo Finance keine Werte liefert (leerer
    Feed, Timeout, Rate-Limit o.ä.). Key kommt aus st.secrets oder einer Umgebungs-
    variable - ohne Key bleibt der Fallback einfach inaktiv (kein Fehler)."""
    try:
        key = st.secrets.get("FINNHUB_API_KEY")
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("FINNHUB_API_KEY")


def _finnhub_symbol(yf_symbol: str) -> Optional[str]:
    """Finnhubs kostenloser Plan deckt zuverlässig nur US-Ticker ohne Börsen-Suffix ab
    (z.B. 'AMD', nicht 'SAP.DE' oder 'BATS.L') - bei anderen Suffixen liefern wir kein
    Symbol zurück, damit der Aufrufer den Fallback sauber überspringt statt falsche
    Daten für den falschen Titel zu holen."""
    if not yf_symbol or "." in yf_symbol:
        return None
    return yf_symbol


def _finnhub_quote(yf_symbol: str) -> Optional[dict]:
    """Fallback-Kursabruf über die Finnhub-API (/quote). Liefert None bei fehlendem
    Key, nicht unterstütztem Symbol oder jedem Fehler - der Aufrufer fällt dann auf
    den bisherigen 'leeren' Zustand zurück, es gibt also keinen harten Absturz.
    Ein kurzer Retry (2 Versuche, analog fetch_intraday()) fängt transiente
    Netzwerk-/Timeout-Aussetzer ab, statt beim ersten Fehlversuch sofort aufzugeben."""
    key = _finnhub_api_key()
    symbol = _finnhub_symbol(yf_symbol)
    if not key or not symbol:
        return None
    url = (
        "https://finnhub.io/api/v1/quote?symbol="
        + urllib.parse.quote(symbol) + "&token=" + urllib.parse.quote(key)
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            last = data.get("c")
            prev = data.get("pc")
            high = data.get("h")
            low = data.get("l")
            if not last or last <= 0:
                if attempt == 0:
                    _sleep(0.6)
                    continue
                return None
            chg = (last / prev - 1.0) * 100 if prev else 0.0
            return {
                "price": float(last),
                "chg": float(chg),
                "high": float(high) if high else float(last),
                "low": float(low) if low else float(last),
                "vol_1y": None,
                "ok": True,
            }
        except Exception:
            if attempt == 0:
                _sleep(0.6)
                continue
            return None
    return None


def _parallel_map(fn, items: list, max_workers: int = PARALLEL_FETCH_WORKERS) -> list:
    """Wendet fn auf jedes Element von items parallel an (ThreadPoolExecutor -
    items sind I/O-gebundene Netzwerk-Calls, GIL wird während der Wartezeit
    freigegeben, echte Threads bringen hier also echten Speedup).

    Reihenfolge des Ergebnisses entspricht IMMER der Eingabe-Reihenfolge
    (wichtig, da Aufrufer die Ergebnisse oft wieder ihren Tickern zuordnen).
    Ein einzelner fehlschlagender Task reißt die anderen nicht mit - fn sollte
    im Idealfall selbst schon defensiv sein (wie fetch_quote/fetch_levels/
    fetch_intraday es bereits sind), zur Sicherheit aber hier nochmal gefangen.
    """
    if not items:
        return []
    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {executor.submit(fn, item): i for i, item in enumerate(items)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception:
                results[idx] = None
    return results


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_daily_history(yf_symbol: str) -> pd.DataFrame:
    """Gemeinsame Tages-Historie (1 Jahr) für fetch_quote() und fetch_levels().
    Redundanz-Fix: beide Funktionen holten vorher unabhängig voneinander sich
    überlappende Fenster (5d bzw. 30d) separat vom yfinance-Backend - 1y deckt
    beide ab (5d/30d sind reine Teilmengen der letzten Zeilen), macht also
    einen der beiden Calls überflüssig. TTL=60s = das strengere der beiden
    vorherigen TTLs (fetch_quote hatte 60s, fetch_levels 120s), damit niemand
    ältere Daten bekommt als vorher."""
    try:
        t = yf.Ticker(yf_symbol)
        return _valid_ohlc(t.history(period="1y", interval="1d"))
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_intraday_raw(yf_symbol: str) -> pd.DataFrame:
    """Gemeinsame Intraday-Historie (1 Tag, 5-Min-Kerzen) für fetch_levels()
    (vwap/today_volume) UND fetch_intraday() (Chart/Momentum-Fade).
    TTL=60s: 5-Min-Kerzen ändern sich ohnehin nur alle 5 Min.; 20s hat
    denselben Feed unnötig oft gezogen."""
    df = None
    for attempt in range(2):
        try:
            df = yf.Ticker(yf_symbol).history(period="1d", interval="5m")
            if df is not None and not df.empty:
                return df
        except Exception:
            df = None
        if attempt == 0:
            _sleep(0.6)
    return df if df is not None else pd.DataFrame()


@st.cache_data(ttl=60, show_spinner=False)
def fetch_quote(yf_symbol: str) -> dict:
    empty = {
        "price": None,
        "chg": None,
        "high": None,
        "low": None,
        "vol_1y": None,
        "ok": False,
    }
    try:
        year = _fetch_daily_history(yf_symbol)
        if year is None or year.empty:
            fb = _finnhub_quote(yf_symbol)
            return fb if fb else empty
        last = float(year["Close"].iloc[-1])
        prev = float(year["Close"].iloc[-2]) if len(year) > 1 else last
        chg = (last / prev - 1.0) * 100 if prev else 0.0
        vol = None
        if len(year) > 20:
            rets = year["Close"].pct_change().dropna()
            vol = float(rets.std() * math.sqrt(252) * 100)
        return {
            "price": last,
            "chg": chg,
            "high": float(year["High"].iloc[-1]),
            "low": float(year["Low"].iloc[-1]),
            "vol_1y": vol,
            "ok": True,
        }
    except Exception:
        fb = _finnhub_quote(yf_symbol)
        return fb if fb else empty


@st.cache_data(ttl=30, show_spinner=False)
def fetch_focus_quote(yf_symbol: str) -> dict:
    empty = {
        "price": None,
        "chg": None,
        "high": None,
        "low": None,
        "vol_1y": None,
        "ok": False,
        "stamp": None,
        "feed": "DELAYED",
    }
    try:
        hist = _valid_ohlc(yf.Ticker(yf_symbol).history(period="5d", interval="1d"))
        if hist.empty:
            fb = _finnhub_quote(yf_symbol)
            if fb:
                fb["stamp"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                fb["feed"] = "DELAYED (Finnhub-Fallback)"
                return fb
            return empty
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else last
        chg = (last / prev - 1.0) * 100 if prev else 0.0
        return {
            "price": last,
            "chg": chg,
            "high": float(hist["High"].iloc[-1]),
            "low": float(hist["Low"].iloc[-1]),
            "vol_1y": None,
            "ok": True,
            "stamp": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
            "feed": "DELAYED",
        }
    except Exception:
        fb = _finnhub_quote(yf_symbol)
        if fb:
            fb["stamp"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            fb["feed"] = "DELAYED (Finnhub-Fallback)"
            return fb
        return empty


@st.cache_data(ttl=60, show_spinner=False)
def fetch_intraday(yf_symbol: str) -> pd.DataFrame:
    # Redundanz-Fix: Retry-Logik und der eigentliche Fetch stecken jetzt in
    # _fetch_intraday_raw() (geteilt mit fetch_levels()) - hier bleibt nur
    # noch die Zeitzonen-Umstellung übrig (s. Kommentar unten).
    df = _fetch_intraday_raw(yf_symbol)
    try:
        if df is not None and not df.empty:
            # yfinance liefert den Index in der Börsen-Zeitzone des jeweiligen Tickers
            # (z.B. "America/New_York" bei US-Werten, "Europe/Berlin" bei Xetra-Titeln).
            # Der Rest der App rechnet/zeigt durchgängig in TZ_BERLIN an - dadurch waren
            # die Uhrzeiten im Chart bei US-Titeln um die Zeitzonen-Differenz (5-6h)
            # versetzt und stimmten nicht mit der Sitzungslogik (session_windows) überein.
            # Hier einheitlich auf Berliner Zeit umstellen (verschiebt nur die Anzeige,
            # ändert nicht den zugrunde liegenden Zeitpunkt).
            idx = pd.to_datetime(df.index)
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            df = df.copy()
            df.index = idx.tz_convert(TZ_BERLIN)
        return df if df is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=120, show_spinner=False)
def fetch_levels(yf_symbol: str) -> dict:
    out = {
        "prev_high": None,
        "prev_low": None,
        "prev_open": None,
        "prev_close": None,
        "open": None,
        "vwap": None,
        "atr": None,
        "bid": None,
        "ask": None,
        "spread_pct": None,
        "prev_volume": None,
        "today_volume": None,
        "volume_delta_pct": None,
        "market_cap": None,
        "avg_volume_20d": None,
        "week52_low": None,
        "week52_high": None,
    }
    try:
        # Redundanz-Fix: vorher eigener 30d-Call + eigener Intraday-Call,
        # unabhängig von fetch_quote()/fetch_intraday(). Beide Fenster sind
        # Teilmengen dessen, was die geteilten Helper ohnehin holen - tail(14)/
        # tail(20)/iloc[-2:]-Zugriffe unten sind identisch, ob man aus einem
        # 30-Tage- oder einem 1-Jahres-Rahmen schneidet (bei 250+ Zeilen gibt
        # es immer genug Historie für ATR(14)/Ø-Volumen(20)).
        daily = _fetch_daily_history(yf_symbol)
        intra = _fetch_intraday_raw(yf_symbol)
        if daily is not None and len(daily) >= 2:
            prev = daily.iloc[-2]
            today = daily.iloc[-1]
            out["prev_high"] = float(prev["High"])
            out["prev_low"] = float(prev["Low"])
            out["prev_open"] = float(prev["Open"])
            out["prev_close"] = float(prev["Close"])
            out["open"] = float(today["Open"])
            if "Volume" in daily.columns:
                pv = prev.get("Volume")
                if pd.notna(pv):
                    out["prev_volume"] = float(pv)
                # Ø-Volumen der letzten 20 abgeschlossenen Handelstage (ohne den
                # laufenden, noch unvollständigen heutigen Tag) als Baseline für
                # das relative Volumen (Stock Volume %).
                completed = daily.iloc[:-1]
                vol_hist = completed["Volume"].dropna().tail(20)
                if len(vol_hist) >= 5:
                    out["avg_volume_20d"] = float(vol_hist.mean())
            tr = pd.concat(
                [
                    daily["High"] - daily["Low"],
                    (daily["High"] - daily["Close"].shift()).abs(),
                    (daily["Low"] - daily["Close"].shift()).abs(),
                ],
                axis=1,
            ).max(axis=1)
            out["atr"] = float(tr.tail(14).mean())
            # 52-Wochen-Tief/Hoch (2026-09): "daily" deckt bereits period="1y" ab
            # (s. _fetch_daily_history) - kein zusätzlicher API-Call nötig, nur
            # Min/Max über eine Spalte, die hier schon im Speicher liegt.
            if not daily["Low"].dropna().empty:
                out["week52_low"] = float(daily["Low"].dropna().min())
            if not daily["High"].dropna().empty:
                out["week52_high"] = float(daily["High"].dropna().max())
        if intra is not None and not intra.empty:
            tp = (intra["High"] + intra["Low"] + intra["Close"]) / 3.0
            vol = intra["Volume"].replace(0, pd.NA).fillna(0)
            if float(vol.sum()) > 0:
                out["vwap"] = float((tp * vol).sum() / vol.sum())
                out["today_volume"] = float(vol.sum())
            if out["open"] is None:
                out["open"] = float(intra["Open"].iloc[0])
        if out["today_volume"] is not None and out["prev_volume"]:
            out["volume_delta_pct"] = (out["today_volume"] - out["prev_volume"]) / out["prev_volume"] * 100.0
        try:
            info = yf.Ticker(yf_symbol).fast_info
            bid = getattr(info, "last_bid", None) or getattr(info, "bid", None)
            ask = getattr(info, "last_ask", None) or getattr(info, "ask", None)
            last = getattr(info, "last_price", None)
            if bid and ask and bid > 0 and ask > 0:
                out["bid"] = float(bid)
                out["ask"] = float(ask)
                mid = (out["bid"] + out["ask"]) / 2
                out["spread_pct"] = (out["ask"] - out["bid"]) / mid * 100 if mid else None
            elif last:
                pass
            mcap = getattr(info, "market_cap", None)
            if mcap:
                out["market_cap"] = float(mcap)
        except Exception:
            pass
    except Exception:
        return out
    return out


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_events_base(yf_symbol: str, ticker: str) -> dict:
    events = {"earnings": None, "earnings_note": None, "session": None, "macro": []}
    if yf_symbol.endswith(".L"):
        events["session"] = "LSE 08:00–16:30 London (≈09:00–17:30 Europe/Berlin)"
        events["macro"] = [
            "UK-Makro/BoE oft vormittags London-Zeit",
            "US-Open 15:30 Europe/Berlin kann UK-Werte nachziehen",
        ]
    elif yf_symbol.endswith((".DE", ".PA", ".AS", ".BR", ".LS", ".MC", ".MI", ".SW")):
        events["session"] = "Euronext/Xetra 09:00–17:30 Europe/Berlin"
        events["macro"] = [
            "EZB / EU-Makro oft 10:00–11:00 Europe/Berlin",
            "US-Open 15:30 Europe/Berlin kann EU-Werte nachziehen",
        ]
    else:
        events["session"] = "US Regular 15:30–22:00 Europe/Berlin"
        events["macro"] = [
            "US-Makro meist 14:30 Europe/Berlin",
            "Fed-Reden / FOMC nachmittags US-Zeit",
        ]
    try:
        t = yf.Ticker(yf_symbol)
        dates = None
        try:
            dates = t.get_earnings_dates(limit=8)
        except Exception:
            dates = None
        if dates is not None and not dates.empty:
            idx = pd.to_datetime(dates.index, utc=True, errors="coerce")
            dates = dates.copy()
            dates.index = idx
            now = pd.Timestamp.now(tz="UTC")
            future = dates[dates.index >= now - pd.Timedelta(days=1)]
            row = future.iloc[0] if not future.empty else dates.iloc[0]
            when = pd.Timestamp(row.name)
            events["earnings"] = when.strftime("%Y-%m-%d %H:%M UTC")
            extra = []
            for col in row.index:
                val = row[col]
                if pd.notna(val) and col.lower() in {
                    "eps estimate",
                    "reported eps",
                    "surprise(%)",
                    "eps estimate",
                }:
                    extra.append(f"{col}: {val}")
            events["earnings_note"] = " · ".join(extra) if extra else ticker
        else:
            try:
                cal = t.calendar
                if cal is not None:
                    events["earnings_note"] = str(cal)[:180]
            except Exception:
                pass
    except Exception:
        pass
    return events


def fetch_events(yf_symbol: str, ticker: str) -> dict:
    """Existing Event-Uhr data plus official macro/SEC enrichment."""
    base = _fetch_events_base(yf_symbol, ticker) or {}
    flags = st.session_state.get("event_source_flags", {"fred": True, "bls": True, "eurostat": True, "sec": True})
    macro = list(base.get("macro") or [])
    if flags.get("fred") and FRED_API_KEY:
        macro.extend(event_sources.fetch_fred_latest(FRED_API_KEY))
    if flags.get("bls"):
        macro.extend(event_sources.fetch_bls_latest())
    if flags.get("eurostat"):
        macro.extend(event_sources.fetch_eurostat_latest())
    insider = event_sources.fetch_sec_insider_events(ticker, SEC_USER_AGENT) if flags.get("sec") else []
    insider += event_sources.insider_clusters(insider)
    base["macro"] = macro
    base["insider"] = insider
    base["insider_score"] = event_sources.insider_score(insider)
    return base


@st.cache_data(ttl=90, show_spinner=False)
def fetch_index_day_chg(yf_symbol: str) -> Optional[float]:
    try:
        hist = _valid_ohlc(yf.Ticker(yf_symbol).history(period="5d", interval="1d"))
        if hist is None or len(hist) < 2:
            return None
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        if prev == 0:
            return None
        return (last / prev - 1.0) * 100.0
    except Exception:
        return None


@st.cache_data(ttl=45, show_spinner=False)
def fetch_news(yf_symbol: str, ticker: str = "", name: str = "", sources: tuple = ()) -> list:
    """Merged, deduplizierte, bewertete Headlines aller aktiven Quellen."""
    if not sources:
        sources = tuple(k for k, v in NEWS_SOURCE_DEFS.items() if v["default"])
    ticker = (ticker or yf_symbol or "").split(".")[0]
    needles = _news_needles(ticker, yf_symbol, name)
    raw = []
    for sid in sources:
        spec = NEWS_SOURCE_DEFS.get(sid)
        if not spec:
            continue
        if spec["kind"] == "yahoo":
            for it in fetch_yahoo_news(yf_symbol):
                raw.append(it)
        elif spec["kind"] == "finnhub":
            for it in fetch_finnhub_news(yf_symbol, ticker, get_finnhub_key()):
                raw.append(it)
        elif spec["kind"] == "rss":
            for it in fetch_rss_feed(spec["url"]):
                title = it.get("title") or ""
                # Ticker-Feeds: nur relevante Zeilen, Ad-hoc immer wenn Match
                if not _headline_matches(title, needles):
                    continue
                raw.append({
                    **it,
                    "source": spec["label"],
                    "source_id": sid,
                    "force_tags": spec.get("force_tags") or (),
                })
    annotated = [annotate_news_item(it) for it in raw if it.get("title")]
    # 72h Rolling Window (deckt Wochenenden/Feiertage ab, damit am nächsten
    # Handelstag noch relevante News verfügbar sind); High-Impact (Score/Tags)
    # bleibt zusätzlich mindestens bis Tagesende Europe/Berlin sichtbar.
    _news_cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
    _today_berlin = datetime.now(TZ_BERLIN).date()

    def _is_high_impact(item):
        tags = {str(t).lower() for t in (item.get("tags") or [])}
        if tags & {"adhoc", "earnings", "guidance", "macro", "fed", "ezb"}:
            return True
        try:
            return abs(int(item.get("score") or 0)) >= 3
        except (TypeError, ValueError):
            return False

    def _within_news_window(item):
        dt = item.get("dt")
        if not dt:
            return True
        try:
            same_day = dt.astimezone(TZ_BERLIN).date() == _today_berlin
        except Exception:
            same_day = True
        if _is_high_impact(item) and same_day:
            return True
        return dt >= _news_cutoff

    annotated = [it for it in annotated if _within_news_window(it)]
    annotated.sort(key=lambda x: x.get("dt") or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return _dedupe_news(annotated)[:20]


@st.cache_data(ttl=90, show_spinner=False)
def fetch_rss_feed(url: str) -> list:
    try:
        raw = _news_http_get(url)
        return _parse_rss_bytes(raw)
    except Exception:
        return []


@st.cache_data(ttl=60, show_spinner=False)
def fetch_yahoo_news(yf_symbol: str) -> list:
    try:
        items = yf.Ticker(yf_symbol).news or []
    except Exception:
        return []
    out = []
    for item in items[:12]:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, dict):
            title = content.get("title") or item.get("title")
            pub = content.get("pubDate") or content.get("displayTime") or ""
            url = ""
            click = content.get("clickThroughUrl") or content.get("canonicalUrl") or {}
            if isinstance(click, dict):
                url = click.get("url") or ""
        else:
            title = item.get("title")
            pub = item.get("providerPublishTime")
            url = item.get("link") or ""
        if title:
            out.append({
                "title": title,
                "published": pub,
                "url": url,
                "source": "Yahoo",
                "source_id": "yahoo",
            })
    return out


@st.cache_data(ttl=60, show_spinner=False)
def fetch_finnhub_news(symbol: str, ticker: str, key: str) -> list:
    if not key:
        return []
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=7)
    symbols = []
    for s in (symbol, ticker, (symbol or "").split(".")[0], (ticker or "").split(".")[0]):
        if s and s.upper() not in symbols:
            symbols.append(s.upper())
    seen = set()
    out = []
    for sym in symbols:
        url = (
            "https://finnhub.io/api/v1/company-news?"
            + urllib.parse.urlencode({
                "symbol": sym,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "token": key,
            })
        )
        try:
            raw = _news_http_get(url)
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for row in data[:20]:
            title = (row.get("headline") or row.get("title") or "").strip()
            if not title:
                continue
            fp = _news_fingerprint(title)
            if fp in seen:
                continue
            seen.add(fp)
            out.append({
                "title": title,
                "published": row.get("datetime"),
                "url": row.get("url") or "",
                "source": row.get("source") or "Finnhub",
                "source_id": "finnhub",
                "summary": row.get("summary") or "",
            })
    return out


