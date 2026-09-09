# VoltDesk modular component — extracted from app.py v0.9.35
# This first-stage split uses a context binder for safe, incremental refactoring.
from __future__ import annotations

import streamlit as st


def bind_context(context: dict) -> None:
    """Bind app-level dependencies during the transition to modular architecture."""
    globals().update(context)

def render_auth_ui():
    """In die Sidebar einbinden: zeigt Login-Link bzw. Nutzerinfo + Logout."""
    _init_auth_state()
    if not HAS_AUTHLIB or not AUTH0_DOMAIN or not AUTH0_CLIENT_ID:
        st.caption("🔐 Login nicht konfiguriert (Auth0-Secrets fehlen).")
        return
    if st.session_state.pop("_auth_just_logged_in", False):
        st.toast("Erfolgreich angemeldet.", icon="✅")
    if st.session_state.get("_auth_error"):
        st.error(f"Login fehlgeschlagen: {st.session_state.pop('_auth_error')}")
    if is_authenticated():
        user = st.session_state.auth_user
        email = user.get("email", user.get("name", "Angemeldet"))
        st.markdown(
            f'<p style="text-align:center;color:#00e5ff;font-weight:600;'
            f'font-size:0.85rem;margin:0.15rem 0 0.4rem 0;">👋 {html.escape(email)}</p>',
            unsafe_allow_html=True,
        )
        if st.button("⏻ Logout", key="auth_logout_btn", use_container_width=True):
            _auth_logout()
    else:
        st.link_button("🔐 Login", _auth_login_url(), use_container_width=True, key="auth_login_btn")


def render_subscription_ui():
    """In die Sidebar einbinden (nur wenn eingeloggt): Trial-Countdown,
    Upgrade-Button oder 'Abo verwalten', je nach Status.

    Bewusst über st.link_button (neuer Tab) statt Same-Tab-Redirect: Login
    lief zuverlässig nur mit link_button (Same-Tab-Meta-Refresh wurde
    offenbar von Browser-Erweiterungen/Ad-Blockern blockiert), daher hier
    konsistent dasselbe Muster. Die URL wird kurz gecacht, damit trotzdem
    nicht bei jedem Rerun eine neue Stripe-Session entsteht."""
    user_id = current_user_id()
    if not user_id:
        return
    if not HAS_STRIPE or not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        st.caption("💳 Abo nicht konfiguriert (Stripe-Secrets fehlen).")
        return
    _sync_subscription_status(user_id)
    if st.session_state.get("_stripe_error"):
        st.error(f"Stripe-Fehler: {st.session_state.pop('_stripe_error')}")
    sub = get_subscription_row(user_id)
    email = (st.session_state.auth_user or {}).get("email")
    _centered_caption = (
        '<p style="text-align:center;margin:0.15rem 0;font-size:0.85rem;">{label}</p>'
    )
    if is_subscription_active(sub):
        st.markdown(_centered_caption.format(label="💎 Pro-Abo aktiv"), unsafe_allow_html=True)
        url = _cached_redirect_url("_portal_url_cache", lambda: _start_portal_session(user_id))
        if url:
            st.link_button("Abo verwalten", url, use_container_width=True)
        st.link_button(
            "⚖️ Widerrufsrecht",
            "https://kaisersoft.github.io/website/voltdesk-landingpage/withdrawal.html",
            use_container_width=True,
            key="btn_withdrawal_active",
        )
    elif is_trial_active(sub):
        days = trial_days_left(sub)
        st.markdown(
            _centered_caption.format(label=f"🎁 Trial: noch {days} Tag{'e' if days != 1 else ''}"),
            unsafe_allow_html=True,
        )
        url = _cached_redirect_url("_checkout_url_cache", lambda: _start_checkout_session(user_id, email))
        if url:
            st.link_button("🌟 Jetzt abonnieren", url, use_container_width=True)
        st.link_button(
            "⚖️ Widerrufsrecht",
            "https://kaisersoft.github.io/website/voltdesk-landingpage/withdrawal.html",
            use_container_width=True,
            key="btn_withdrawal_trial",
        )
    else:
        st.markdown(_centered_caption.format(label="🔒 Trial abgelaufen"), unsafe_allow_html=True)
        url = _cached_redirect_url("_checkout_url_cache", lambda: _start_checkout_session(user_id, email))
        if url:
            st.link_button("💎 Pro abonnieren", url, use_container_width=True)
        st.link_button(
            "⚖️ Widerrufsrecht",
            "https://kaisersoft.github.io/website/voltdesk-landingpage/withdrawal.html",
            use_container_width=True,
            key="btn_withdrawal_expired",
        )


@st.dialog("Trade bestätigen", width="medium")
def _trade_confirmation_dialog(trade: dict):
    """Bestätigung VOR der Ausführung. Kauf/Verkauf führt aus, Abbrechen bricht ab."""
    ticker = str(trade.get("ticker") or "—")
    side = str(trade.get("side") or "—")
    wkn = trade.get("wkn") or "—"
    product = trade.get("product") or {}
    action = str(trade.get("action") or "Trade")
    kind = trade.get("kind") or "buy"
    is_sell = kind in {"sell", "partial", "flatten"} or any(
        token in action for token in ("Verkauf", "Teilverkauf", "Bestand")
    )
    st.markdown(f"**{ticker} · {side}** — {action}")
    st.caption("Wird erst ausgeführt, wenn du Kauf/Verkauf bestätigst.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Einsatz / Betrag", f"{float(trade.get('amount',0) or 0):,.2f} €")
    c2.metric("Kurs", f"{float(trade.get('price',0) or 0):,.4f}")
    c3.metric("Gebühr (geschätzt)", f"{float(trade.get('fee',0) or FEE_PER_TRADE):.2f} €")
    st.markdown("#### Broker-Daten")
    b1, b2, b3 = st.columns(3)
    b1.write(f"**WKN:** {wkn}")
    b2.write(f"**ISIN:** {product.get('isin') or '—'}")
    b3.write(f"**Emittent:** {product.get('issuer') or '—'}")
    b1, b2, b3 = st.columns(3)
    b1.write(f"**Produkt:** {product.get('typ') or trade.get('typ') or '—'}")
    b2.write(f"**Hebel:** {product.get('leverage') or trade.get('leverage') or '—'}")
    b3.write(f"**Richtung:** {'Kauf' if side == 'LONG' else 'Verkauf' if side == 'SHORT' else side}")
    if product.get("ko") is not None:
        st.write(f"**KO-Schwelle:** {float(product['ko']):.4f}")
    if trade.get("stop") is not None:
        st.write(f"**Paper-Stop:** {float(trade['stop']):.4f}")
    if trade.get("take") is not None:
        st.write(f"**Take (PDH/OR):** {float(trade['take']):.4f}")
    if trade.get("pnl") is not None:
        st.write(f"**P&L (aktuell):** {float(trade['pnl']):+.2f} €")

    engine_label = trade.get("setup_label") or "Manuell"
    quality = trade.get("setup_quality")
    try:
        quality_n = int(quality) if quality is not None and str(quality).strip() != "" else None
    except (TypeError, ValueError):
        quality_n = None
    s1, s2 = st.columns(2)
    s1.metric("Setup (automatisch)", engine_label)
    s2.metric("Setup Quality (automatisch)", f"{quality_n}" if quality_n is not None else "—")
    st.caption(
        "Setup und Quality kommen aus der Engine (Konfidenz, Volumen, Muster, News). "
        "Nur Mood ist ein Pflichtfeld."
    )

    mood = st.selectbox("Mood *", TRADE_MOOD_CHOICES, key="pending_trade_mood")
    fields_ok = not str(mood).startswith("—")
    if not fields_ok:
        st.warning("Mood ist ein Pflichtfeld.")

    tilt = detect_tilt(st.session_state.get("fills") or [], current_mood=mood if fields_ok else None)
    apply_tilt_lock_if_needed(tilt)
    tilt_blocks_buy = (
        (not is_sell)
        and bool(st.session_state.get("tilt_enabled", True))
        and (st.session_state.get("tilt_lock") or tilt.get("suggested_action") == "lock")
    )
    if tilt.get("tilt_score", 0) > 0 or tilt.get("reasons"):
        reasons_txt = " · ".join(tilt.get("reasons") or []) or "—"
        if tilt_blocks_buy:
            st.error(
                f"🔒 Tilt-Lock · Score {tilt['tilt_score']} — neue Käufe gesperrt. {reasons_txt}"
            )
        elif tilt.get("suggested_action") == "pause":
            st.warning(
                f"⚠️ Tilt-Warnung · Score {tilt['tilt_score']} — Pause empfohlen. {reasons_txt}"
            )
        elif tilt.get("suggested_action") == "warn":
            st.info(f"🧠 Tilt-Hinweis · Score {tilt['tilt_score']} — {reasons_txt}")

    # Zusätzlicher Halt-Grund (Kill Switch/Risk Lock/Trading Pause/fehlender Tages-
    # Setup-Vertrag) - vorher nur in execute_paper_buy geprüft, tauchte also erst
    # NACH dem Klick auf "Kauf" als Fehlermeldung auf. Jetzt schon hier sichtbar,
    # bevor der Button überhaupt aktiv wäre.
    halt_reason = buy_halt_reason() if not is_sell else None
    if halt_reason and not tilt_blocks_buy:
        st.error(f"🚫 {halt_reason}")

    # Off-Plan-Check (Tages-Setup-Vertrag): weicht das automatisch erkannte Setup
    # dieses Trades vom heute gewählten Setup ab, braucht der Kauf eine explizite
    # Extra-Bestätigung und wird im Fill als off_plan markiert (s. build_day_report).
    # "Sonstiges/Discretionary" als Tagesvertrag schaltet den Check bewusst ab -
    # dafür ist genau diese Wahl selbst im Tagesbericht sichtbar.
    off_plan = False
    off_plan_confirmed = True
    if not is_sell:
        _contract = get_daily_setup_contract()
        if _contract and _contract.get("setup") not in (None, "Sonstiges/Discretionary"):
            if engine_label != _contract["setup"]:
                off_plan = True
                st.warning(
                    f"⚠️ Off-Plan: heutiges Setup ist **{_contract['setup']}**, dieser Trade "
                    f"ist **{engine_label}**. Wird im Journal als off-plan markiert."
                )
                off_plan_confirmed = st.checkbox(
                    "Ich kaufe trotzdem außerhalb des Tages-Setups",
                    key="pending_trade_off_plan_confirm",
                )
    trade["off_plan"] = off_plan

    st.markdown(
        """
        <style>
        button[kind="secondary"][data-testid="baseButton-secondary"] {}
        .st-key-trade_confirm_buy button {
            background-color: #16a34a !important;
            border-color: #16a34a !important;
            color: #ffffff !important;
        }
        .st-key-trade_confirm_sell button {
            background-color: #dc3545 !important;
            border-color: #dc3545 !important;
            color: #ffffff !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    cancel_col, ok_col = st.columns(2)
    with cancel_col:
        if st.button("Abbrechen", use_container_width=True, key="trade_confirmation_cancel"):
            if is_sell:
                msg = "Verkauf abgebrochen."
            elif "Nachkauf" in action:
                msg = "Nachkauf abgebrochen."
            else:
                msg = "Kauf abgebrochen."
            st.session_state.pending_trade = None
            st.session_state.trade_confirmation = None
            st.session_state.trade_abort_flash = msg
            st.rerun()
    with ok_col:
        confirm_label = "Verkauf" if is_sell else "Kauf"
        confirm_key = "trade_confirm_sell" if is_sell else "trade_confirm_buy"
        if st.button(
            confirm_label,
            type="primary",
            use_container_width=True,
            key=confirm_key,
            disabled=(not fields_ok) or tilt_blocks_buy or bool(halt_reason) or (off_plan and not off_plan_confirmed),
        ):
            _commit_pending_trade(trade, mood, quality_n if quality_n is not None else quality)


def request_trade_confirmation(**payload):
    """Dialog vor der Ausführung öffnen. Noch kein Fill."""
    product = payload.get("product") or _lookup_product_details(
        payload.get("ticker"), payload.get("wkn"), payload.get("side"), payload.get("leverage")
    )
    payload["product"] = product
    payload.setdefault("fee", float(st.session_state.get("fee_per_trade", FEE_PER_TRADE) if effective_pro_mode() else FEE_PER_TRADE))
    if payload.get("kind") in {None, "buy"} and payload.get("take") is None:
        try:
            tk = payload.get("ticker")
            meta = find_meta(tk) if tk else None
            lv = fetch_levels(meta["yf"]) if meta else {}
            intra = fetch_intraday(meta["yf"]) if meta else None
            payload["take"] = suggest_take_profit(payload.get("side"), payload.get("price"), lv, intra)
        except Exception:
            payload["take"] = None
    rec = payload.get("rec") or {}
    if not payload.get("setup_label"):
        payload["setup_label"] = classify_setup_label(
            source=payload.get("exec_reason") or payload.get("reason"),
            side=payload.get("side"),
            price=payload.get("price"),
        )
    if payload.get("setup_quality") is None:
        payload["setup_quality"] = rec.get("setup_quality")
        if payload.get("setup_quality") is None:
            payload["setup_quality"] = compute_setup_quality(
                confidence=rec.get("confidence"),
                patterns=None,
                volume_momentum=rec.get("volume_momentum"),
                news=None,
                side=payload.get("side"),
            )
    st.session_state.pending_trade = payload
    _trade_confirmation_dialog(payload)


def _show_trade_confirmation(ticker, side, amount, price, fee, reason, action="Trade", pnl=None, wkn=None, typ=None, leverage=None, stop=None, **kwargs):
    """Kompatibilität: leitet auf den Bestätigungsdialog vor Ausführung um."""
    request_trade_confirmation(
        kind=kwargs.get("kind") or "buy",
        ticker=ticker, side=side, amount=amount, price=price, fee=fee,
        reason=reason, action=action, pnl=pnl, wkn=wkn, typ=typ,
        leverage=leverage, stop=stop, **{k: v for k, v in kwargs.items() if k != "kind"},
    )


@st.dialog("⚠️ Kritische Portfolio-Ereignisse", width="large")
def _critical_trade_events_dialog(events: list):
    st.error(
        "Diese Aktionen wurden im VoltDesk-Paper-Depot bereits ausgeführt. "
        "**Bitte jetzt in deinem echten Broker nachziehen**, bevor du bestätigst — "
        "sonst laufen Desk und Broker-Portfolio auseinander."
    )
    closes = [ev for ev in events if ev.get("event_type") not in ("trail", "stop-set")]
    trails = [ev for ev in events if ev.get("event_type") in ("trail", "stop-set")]
    if closes:
        st.markdown("**Position schließen / nachziehen (bereits im Paper ausgeführt)**")
        for ev in closes:
            st.markdown(f"- {_format_critical_event_line(ev)}")
    if trails:
        st.markdown("**Stop im Broker anpassen — Position bleibt offen**")
        for ev in trails:
            st.markdown(f"- {_format_critical_event_line(ev)}")
    st.divider()
    if st.button("✅ Bestätigt — im Broker nachgezogen", type="primary", use_container_width=True):
        acknowledge_critical_events([ev["id"] for ev in events])
        st.rerun()


def _maybe_show_critical_trade_events_dialog():
    """Ganz am Anfang von dashboard() aufrufen (vor allem anderen Rendering) -
    blockiert die restliche Seite bewusst, solange offene Broker-relevante
    Ereignisse existieren (dein expliziter Wunsch: "hart", da eine verpasste
    Meldung zu einem auseinanderlaufenden Portfolio führen kann)."""
    events = get_pending_critical_events()
    if any(ev["requires_broker_action"] for ev in events):
        _critical_trade_events_dialog(events)


@st.dialog("⚙️ Einstellungen", width="medium")
def _settings_dialog():
    # Pro Modus ganz oben, außerhalb der Spalten - sichtbarer Schalter (die
    # Zwangsprüfung/Deaktivierung bei fehlendem Zugriff läuft weiterhin
    # zusätzlich in der Sidebar bei jedem Seitenaufruf, s. Kommentar dort).
    if has_pro_access():
        st.toggle(
            "🔓 Pro Modus",
            key="pro_mode",
            help=(
                "Pro-Funktionen: höherer Hebel (bis 8x), weitere Risiko-Limits, "
                "Backtest & Analytics, freie News-Quellen, einstellbare Gebühr "
                "und manuelle Cash-Anpassung."
            ),
        )
    else:
        st.session_state.pro_mode = False
        if not is_authenticated():
            st.caption("🔐 Zum Freischalten des Pro-Modus bitte einloggen.")
        else:
            st.caption("🔒 Trial abgelaufen - Abo nötig für Pro Modus.")
    st.divider()

    # Zweispaltiges Layout
    col1, col2 = st.columns(2)

    with col1:
        pro_mode_active = effective_pro_mode()

        st.markdown("#### News-Quellen")
        if pro_mode_active:
            flags = get_news_source_flags()
            changed_src = False
            new_flags = dict(flags)
            fh_key = get_finnhub_key()

            # News-Quellen in zwei Unterspalten
            src_col1, src_col2 = st.columns(2)
            src_items = list(NEWS_SOURCE_DEFS.items())
            mid = (len(src_items) + 1) // 2
            for i, (sid, spec) in enumerate(src_items):
                col = src_col1 if i < mid else src_col2
                label = spec["label"]
                if sid == "finnhub" and FINNHUB_NEWS_DISABLED:
                    label = f"{label} (deaktiviert – Lizenzprüfung läuft)"
                elif sid == "finnhub" and not fh_key:
                    label = f"{label} (kein API-Key)"
                val = col.toggle(
                    label,
                    value=(
                        False if (sid == "finnhub" and (FINNHUB_NEWS_DISABLED or not fh_key))
                        else bool(flags.get(sid, spec["default"]))
                    ),
                    key=f"news_src_{sid}",
                    disabled=(sid == "finnhub" and (FINNHUB_NEWS_DISABLED or not fh_key)),
                )
                if sid == "finnhub" and (FINNHUB_NEWS_DISABLED or not fh_key):
                    val = False
                if bool(val) != bool(flags.get(sid)):
                    changed_src = True
                new_flags[sid] = bool(val)
            st.session_state.news_sources_pro = new_flags
            if not any(new_flags.values()):
                st.caption("Mindestens eine Quelle sinnvoll — sonst bleibt der News-Block leer.")
            if changed_src:
                for fn in (fetch_news, fetch_rss_feed, fetch_yahoo_news, fetch_finnhub_news):
                    if hasattr(fn, "clear"):
                        fn.clear()
                st.session_state.rec_cache = {}
        else:
            st.caption(
                "🔒 Standard: Yahoo Finance + Finnhub aktiv. Weitere Quellen und die "
                "individuelle Auswahl sind nur im **Pro Modus** verfügbar."
            )

        st.divider()
        st.markdown("#### Event-Uhr · Quellen")
        _ef = dict(st.session_state.get("event_source_flags") or {
            "fred": True, "bls": True, "eurostat": True, "sec": True
        })
        _event_c1, _event_c2 = st.columns(2)
        with _event_c1:
            _fred_on = st.toggle(
                "FRED", value=bool(_ef.get("fred", True)),
                disabled=not bool(FRED_API_KEY), key="settings_event_src_fred"
            )
            _bls_on = st.toggle(
                "BLS", value=bool(_ef.get("bls", True)), key="settings_event_src_bls"
            )
        with _event_c2:
            _eu_on = st.toggle(
                "EUROSTAT", value=bool(_ef.get("eurostat", True)), key="settings_event_src_eurostat"
            )
            _sec_on = st.toggle(
                "SEC Form 4", value=bool(_ef.get("sec", True)), key="settings_event_src_sec"
            )
        st.session_state.event_source_flags = {
            "fred": bool(_fred_on), "bls": bool(_bls_on),
            "eurostat": bool(_eu_on), "sec": bool(_sec_on),
        }
        if not FRED_API_KEY:
            st.caption("FRED deaktiviert: FRED_API_KEY fehlt.")

    with col2:
        st.markdown("#### Gebühr & Cash")
        if pro_mode_active:
            st.session_state.fee_per_trade = st.number_input(
                "Gebühr je Trade (€)",
                min_value=0.0,
                max_value=50.0,
                value=float(st.session_state.get("fee_per_trade", FEE_PER_TRADE)),
                step=0.5,
                key="settings_fee_per_trade",
                help="Nur im Pro Modus einstellbar - sonst fest 1€ je Kauf/Verkauf.",
            )
            new_cash_val = st.number_input(
                "Cash-Bestand (€)",
                min_value=0.0,
                step=100.0,
                value=float(cash_available()),
                key="settings_cash_override",
                help="Nur im Pro Modus editierbar - wird bei jedem Refresh wieder aus den "
                     "tatsächlichen Daten neu gesetzt, bis du ihn manuell überschreibst und übernimmst.",
            )
            if st.button("Cash-Bestand übernehmen", key="settings_cash_apply", use_container_width=True):
                set_cash_balance(new_cash_val)
                save_app_state()
                st.rerun()
        else:
            st.caption("🔒 Gebühr je Trade und manuelle Cash-Anpassung sind nur im **Pro Modus** verfügbar.")

        st.divider()
        st.markdown("#### Sprache")
        LANGUAGE_OPTIONS = {
            "de": "🇩🇪 Deutsch",
            "en": "🇬🇧 English",
            "fr": "🇫🇷 Français",
            "es": "🇪🇸 Español",
            "it": "🇮🇹 Italiano",
        }
        st.selectbox(
            "Sprache",
            list(LANGUAGE_OPTIONS.keys()),
            format_func=lambda code: LANGUAGE_OPTIONS[code],
            index=list(LANGUAGE_OPTIONS.keys()).index(
                st.session_state.get("ui_language", "de")
            ),
            key="ui_language",
            disabled=True,
            label_visibility="collapsed",
            help="Sprachauswahl ist vorbereitet, aber noch gesperrt — aktuell nur Deutsch verfügbar.",
        )
        st.caption("🔒 Weitere Sprachen folgen — aktuell fest auf Deutsch.")

    # "Multi-User"-Platzhalter entfernt (2026-09): reiner statischer Hinweistext
    # ohne jede Funktion dahinter - Login/Logout/Abo-Verwaltung leben bereits
    # vollständig in der Sidebar (render_auth_ui(), render_subscription_ui()).

    st.divider()
    st.markdown("#### 🧠 Tilt-Erkennung (Revenge Trading)")
    st.toggle(
        "Tilt-Erkennung aktiv",
        value=bool(st.session_state.get("tilt_enabled", True)),
        key="tilt_enabled",
        help="Wertet Verlustserien und die Mood-Angabe im Trade-Dialog aus.",
    )
    t1, t2 = st.columns(2)
    with t1:
        st.slider(
            "Lookback (letzte Closes)",
            min_value=3, max_value=12,
            value=int(st.session_state.get("tilt_lookback", TILT_LOOKBACK_DEFAULT)),
            key="tilt_lookback",
        )
        st.slider(
            "Verlustrate-Schwelle (%)",
            min_value=30, max_value=90,
            value=int(st.session_state.get("tilt_loss_rate_threshold", TILT_LOSS_RATE_DEFAULT)),
            step=5,
            key="tilt_loss_rate_threshold",
        )
        st.slider(
            "Min. Verluste in Folge",
            min_value=2, max_value=6,
            value=int(st.session_state.get("tilt_consecutive_losses", TILT_CONSEC_DEFAULT)),
            key="tilt_consecutive_losses",
        )
    with t2:
        st.slider(
            "Score für Warnung",
            min_value=20, max_value=70,
            value=int(st.session_state.get("tilt_warn_threshold", TILT_WARN_DEFAULT)),
            step=5,
            key="tilt_warn_threshold",
        )
        st.slider(
            "Score für Tilt-Lock",
            min_value=50, max_value=95,
            value=int(st.session_state.get("tilt_lock_threshold", TILT_LOCK_DEFAULT)),
            step=5,
            key="tilt_lock_threshold",
        )
        if st.session_state.get("tilt_lock"):
            st.error(f"Tilt-Lock aktiv (Score {st.session_state.get('tilt_lock_score') or '—'})")
            unlock_note = st.text_input(
                "Grund fürs Aufheben (Pflicht) *",
                key="settings_tilt_unlock_note",
                help="Wird mit Zeitstempel und Score geloggt.",
            )
            if st.button(
                "Tilt-Lock aufheben", key="settings_clear_tilt_lock",
                use_container_width=True, disabled=not unlock_note.strip(),
            ):
                _log_tilt_unlock(unlock_note)
                st.rerun()
        else:
            live = detect_tilt()
            st.caption(f"Aktueller Tilt-Score: {live.get('tilt_score', 0)}")

    # Account & Daten (DSGVO) - jetzt oberhalb von Debug (2026-09).
    st.divider()
    st.markdown("#### 🔐 Konto & Daten (DSGVO)")
    _export_user_id = current_user_id()
    if _export_user_id:
        _export_data = collect_account_export_data()
        _export_json = json.dumps(_export_data, ensure_ascii=False, indent=2).encode("utf-8")
        _export_zip_buf = io.BytesIO()
        with zipfile.ZipFile(_export_zip_buf, "w", zipfile.ZIP_DEFLATED) as _zf:
            for _table, _rows in _export_data.items():
                _zf.writestr(f"{_table}.csv", pd.DataFrame(_rows).to_csv(index=False))
        ecol1, ecol2 = st.columns(2)
        with ecol1:
            st.download_button(
                "📤 Export als JSON",
                data=_export_json,
                file_name=f"voltdesk_export_{datetime.now(TZ_BERLIN).strftime('%Y%m%d_%H%M')}.json",
                mime="application/json",
                use_container_width=True,
                key="settings_data_export_json",
            )
        with ecol2:
            st.download_button(
                "📤 Export als CSV (ZIP)",
                data=_export_zip_buf.getvalue(),
                file_name=f"voltdesk_export_{datetime.now(TZ_BERLIN).strftime('%Y%m%d_%H%M')}.zip",
                mime="application/zip",
                use_container_width=True,
                key="settings_data_export_csv",
            )
    else:
        st.button(
            "📤 Datenexport anfordern",
            disabled=True,
            key="settings_data_export",
            use_container_width=True,
            help="Nur eingeloggt verfügbar.",
        )
    st.caption(
        "⚠️ „Account löschen“ entfernt unwiderruflich alle VoltDesk-Daten dieses Kontos "
        "(Trades, Berichte, Einstellungen, Fehlerprotokoll, Abo-Verknüpfung). Der Login "
        "selbst (Auth0) sowie ein laufendes Stripe-Abo werden NICHT automatisch berührt — "
        "ein Abo bitte vorher über „Abo verwalten“ in der Sidebar kündigen."
    )
    if not st.session_state.get("_confirm_delete_account"):
        if st.button(
            "🗑️ Account löschen",
            key="settings_delete_account_btn",
            use_container_width=True,
            disabled=not _export_user_id,
            help=None if _export_user_id else "Nur eingeloggt verfügbar.",
        ):
            st.session_state["_confirm_delete_account"] = True
            st.rerun()
    else:
        st.error("Das kann nicht rückgängig gemacht werden.")
        confirm_text = st.text_input(
            'Zum Bestätigen "DELETE" eingeben',
            key="settings_delete_account_confirm_text",
        )
        dcol1, dcol2 = st.columns(2)
        with dcol1:
            if st.button(
                "Endgültig löschen",
                key="settings_delete_account_final",
                use_container_width=True,
                disabled=confirm_text.strip().upper() != "DELETE",
            ):
                delete_account_data()
                st.session_state["auth_user"] = None
                st.session_state["_confirm_delete_account"] = False
                st.success("Account-Daten gelöscht. Du wirst abgemeldet.")
                st.rerun()
        with dcol2:
            if st.button("Abbrechen", key="settings_delete_account_cancel", use_container_width=True):
                st.session_state["_confirm_delete_account"] = False
                st.rerun()

    # Debug-Export außerhalb der Spalten - deaktiviert (2026-09): bisher nichts
    # zum Debuggen, Code bleibt vorerst drin (DEBUG_PANEL_ENABLED auf True
    # setzen, um es wieder einzublenden), nur einfach per Flag ausgeblendet
    # statt gelöscht.
    if DEBUG_PANEL_ENABLED:
        st.divider()
        st.markdown("#### 🐞 Debug")
        try:
            _err_df = _read_sql(
                "SELECT id, user_id, timestamp, context, message FROM error_log ORDER BY id DESC"
            )
        except Exception:
            _err_df = pd.DataFrame(columns=["id", "user_id", "timestamp", "context", "message"])
        st.caption(f"{len(_err_df)} Fehlerprotokoll-Einträge (max. 30 Tage alt).")
        st.download_button(
            "Fehlerprotokoll als CSV exportieren",
            data=_err_df.to_csv(index=False).encode("utf-8"),
            file_name=f"voltdesk_error_log_{datetime.now(TZ_BERLIN).strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            use_container_width=True,
            key="btn_export_error_log",
        )

    # Fertig-Button außerhalb der Spalten
    st.divider()
    if st.button("Fertig", type="primary", use_container_width=True, key="settings_done"):
        save_app_state()
        st.rerun()


@st.dialog("Impressum", width="large")
def _impressum_dialog():
    st.text_area(
        "Impressum",
        value=IMPRESSUM_TEXT,
        height=380,  # war 280 - bei 17 Zeilen Text zu knapp, Inhalt lief ins Scrollen/
        # zeigte den nativen Textarea-Resize-Griff, der leicht wie ein abgeschnittener
        # Text mit "..." wirken kann.
        disabled=True,
        label_visibility="collapsed",
        key="impressum_text",
    )
    if st.button("OK", type="primary", use_container_width=True, key="impressum_ok"):
        st.rerun()


def render_daily_setup_gate(sess: dict) -> None:
    """Pflichtwahl vor dem ersten Kauf des Tages: ein Setup + ein Max-Risiko in €.
    Ohne das bleiben neue Käufe gesperrt (s. buy_halt_reason()). Nur relevant,
    wenn die aktuelle Phase überhaupt Käufe erlaubt (PRE/CLOSE brauchen das nicht)."""
    if not sess["playbook"].get("buy") or get_daily_setup_contract():
        return
    st.warning(
        "📋 **Tages-Setup noch nicht gewählt.** Vor dem ersten Kauf heute: ein "
        "Setup festlegen und ein Max-Risiko in € für den Tag - bis dahin bleiben "
        "neue Käufe gesperrt."
    )
    gcol1, gcol2, gcol3 = st.columns([2, 1, 1])
    with gcol1:
        setup_choice = st.selectbox(
            "Heutiges Setup",
            DAILY_SETUP_OPTIONS,
            key="daily_setup_choice",
            help="Käufe, deren automatisch erkanntes Setup davon abweicht, "
                 "brauchen später eine Extra-Bestätigung und werden im Journal "
                 "als off-plan markiert.",
        )
    with gcol2:
        default_risk = float(st.session_state.get("daily_risk_pct", 2.0)) / 100.0 * current_capital()
        risk_choice = st.number_input(
            "Max-Risiko heute (€)",
            min_value=10.0,
            value=max(10.0, round(default_risk, 0)),
            step=10.0,
            key="daily_setup_risk_eur",
        )
    with gcol3:
        st.markdown("<div style='height:1.65rem'></div>", unsafe_allow_html=True)
        if st.button(
            "Setup festlegen", type="primary", use_container_width=True,
            key="confirm_daily_setup",
        ):
            st.session_state.daily_setup_contract = {
                "date": datetime.now(TZ_BERLIN).date().isoformat(),
                "setup": setup_choice,
                "max_risk_eur": float(risk_choice),
            }
            save_app_state()
            st.rerun()


def render_daily_setup_status() -> None:
    """Zeigt den aktiven Tages-Setup-Vertrag + erlaubt eine geloggte Änderung
    (bewusst mit Pflichtgrund - soll die Ausnahme bleiben, nicht der Normalfall)."""
    contract = get_daily_setup_contract()
    if not contract:
        return
    st.caption(
        f"📋 Heutiges Setup: **{contract['setup']}** · Max-Risiko heute: "
        f"{float(contract.get('max_risk_eur') or 0):.0f} €"
    )
    with st.expander("Setup heute ändern", expanded=False):
        st.caption("Sollte die Ausnahme bleiben - jede Änderung wird mit Zeitstempel geloggt.")
        new_setup = st.selectbox(
            "Neues Setup", DAILY_SETUP_OPTIONS, key="daily_setup_change_choice",
        )
        new_risk = st.number_input(
            "Neues Max-Risiko (€)", min_value=10.0,
            value=float(contract.get("max_risk_eur") or 10.0), step=10.0,
            key="daily_setup_change_risk",
        )
        change_note = st.text_input("Grund für die Änderung (Pflicht)", key="daily_setup_change_note")
        if st.button(
            "Ändern", key="daily_setup_change_confirm", disabled=not change_note.strip(),
        ):
            log = list(st.session_state.get("daily_setup_change_log") or [])
            log.append({
                "at": datetime.now(TZ_BERLIN).isoformat(),
                "from": contract["setup"], "to": new_setup, "note": change_note.strip(),
            })
            st.session_state.daily_setup_change_log = log[-50:]
            st.session_state.daily_setup_contract = {
                "date": contract["date"], "setup": new_setup, "max_risk_eur": float(new_risk),
            }
            save_app_state()
            st.rerun()


@st.fragment
def _render_sidebar_risk_settings():
    """Risiko- und Session-Einstellungen als eigenes Fragment (2026-09, Testlauf
    gegen den 'jede Sidebar-Änderung löst Komplett-Rerun aus'-Punkt): Slider/
    Toggle hier schreiben nur in session_state und müssen nicht sofort irgendwo
    anders auf der Seite sichtbar reagieren. Mit @st.fragment läuft bei einer
    Änderung HIER nur dieser Block neu, nicht das komplette Skript (Chart,
    Watchlist, Ranker, Journal etc. bleiben unberührt) - das ist gerade beim
    Ziehen eines Sliders spürbar, da Streamlit dabei mehrere Reruns pro Sekunde
    auslöst. Falls sich das in der Praxis nicht sauber verhält: @st.fragment
    einfach wieder entfernen, an der Logik selbst ändert das nichts.
    """
    st.divider()
    st.markdown("#### Risiko")
    st.session_state.pos_limit_enabled = st.toggle(
        "Positionslimit aktiv",
        value=st.session_state.get("pos_limit_enabled", True),
        help="Deaktiviert: Käufe/Nachkäufe sind bis 100% des verfügbaren Cash möglich.",
        key="sidebar_pos_limit_enabled",
    )
    pro_mode_active = effective_pro_mode()
    if st.session_state.pos_limit_enabled:
        slider_max = 10.0 if pro_mode_active else 5.0
        # Bestehenden Wert kappen, falls Pro Modus zwischenzeitlich deaktiviert wurde
        # und der gespeicherte Wert (z.B. 8%) jetzt über dem 5%-Limit liegt.
        current_val = float(st.session_state.get("pos_limit_pct", MAX_POSITION_PCT))
        if current_val > slider_max:
            current_val = slider_max
            st.session_state.pos_limit_pct = slider_max
        st.session_state.pos_limit_pct = st.slider(
            "Limit je Trade (% vom Cash)",
            min_value=0.0,
            max_value=slider_max,
            value=current_val,
            step=0.5,
            key="sidebar_pos_limit_pct",
            help="Ohne Pro Modus auf 5% gedeckelt - Pro Modus erlaubt bis 10%."
            if not pro_mode_active else None,
        )
    else:
        st.caption("⚠️ Limit deaktiviert — Käufe bis 100% des Cash möglich.")

    # Max. Trades/Tag: analog zum Positionslimit-Slider - ohne Pro Modus fix auf max. 5,
    # Pro Modus erlaubt unbegrenzt viele Trade-Eröffnungen pro Tag.
    if pro_mode_active:
        st.session_state.max_trades_per_day = None
        st.caption("Max. Trades/Tag: **unbegrenzt** (Pro Modus)")
    else:
        st.session_state.max_trades_per_day = st.slider(
            "Max. Trades/Tag",
            min_value=1,
            max_value=5,
            value=int(st.session_state.get("max_trades_per_day") or 5),
            step=1,
            key="sidebar_max_trades",
            help="Pro Modus erlaubt eine unbegrenzte Anzahl von Trades pro Tag.",
        )

    # Tages-Risiko-Limit (% vom Kapital): ohne Pro Modus max. 10%, Default 2%;
    # Pro Modus erlaubt bis zu 60% "In Produkten".
    risk_slider_max = 60.0 if pro_mode_active else 10.0
    current_risk_pct = float(st.session_state.get("daily_risk_pct", 2.0))
    if current_risk_pct > risk_slider_max:
        current_risk_pct = risk_slider_max
        st.session_state.daily_risk_pct = risk_slider_max
    st.session_state.daily_risk_pct = st.slider(
        "Tages-Risikoallokation (% vom Kapital)",
        min_value=0.5,
        max_value=risk_slider_max,
        value=current_risk_pct,
        step=0.5,
        key="sidebar_daily_risk_pct",
        help="Ohne Pro Modus auf 10% gedeckelt - Pro Modus erlaubt bis 60% in Produkten."
        if not pro_mode_active else None,
    )

    # Max. Verlust pro Trade in € (Guardrail 3.6, s. execute_paper_buy): bisher gab
    # es nur ein Tages- und ein Konto-Exposure-Limit, aber keine Obergrenze für den
    # geschätzten Verlust EINES einzelnen Trades bei Stop-Erreichung. Schätzung =
    # Einsatz × Hebel × Stop-Distanz in % - der tatsächlich bewegte, gehebelte Betrag.
    max_loss_cap = 5000.0 if pro_mode_active else 500.0
    current_max_loss = float(st.session_state.get("max_loss_per_trade_eur", 150.0))
    if current_max_loss > max_loss_cap:
        current_max_loss = max_loss_cap
        st.session_state.max_loss_per_trade_eur = max_loss_cap
    st.session_state.max_loss_per_trade_eur = st.slider(
        "Max. Verlust pro Trade (€)",
        min_value=10.0,
        max_value=max_loss_cap,
        value=current_max_loss,
        step=10.0,
        key="sidebar_max_loss_per_trade",
        help="Hartes Cap je Trade (Stop-Risiko). Die Engine prüft immer diesen Wert. "
             "Free: einstellbar bis 500 €. Pro: einstellbar bis 5.000 €.",
    )

    # KO-Warnschwellen (Gamma-Warnung): nur im Pro Modus konfigurierbar, sonst
    # Standardwerte (Warnung 5%, Kritisch 3%) über check_ko_proximity().
    if pro_mode_active:
        st.session_state.ko_warning_threshold = st.slider(
            "KO-Warnung (%)",
            min_value=2.0,
            max_value=15.0,
            value=float(st.session_state.get("ko_warning_threshold", 5.0)),
            step=0.5,
            key="sidebar_ko_warning_threshold",
            help="Ab diesem Abstand zur KO-Schwelle erscheint eine Warnung.",
        )
        st.session_state.ko_critical_threshold = st.slider(
            "KO-Kritisch (%)",
            min_value=1.0,
            max_value=5.0,
            value=float(st.session_state.get("ko_critical_threshold", 3.0)),
            step=0.5,
            key="sidebar_ko_critical_threshold",
            help="Ab diesem Abstand zur KO-Schwelle sollte dringend gehandelt werden.",
        )

    st.divider()

    st.markdown("#### Session")
    st.session_state.auto_flatten = st.toggle(
        "Auto-Close vor Handelsschluss",
        value=st.session_state.auto_flatten,
        help="Final Close: alle Paper-Positionen in den letzten Minuten vor Xetra 17:30 bzw. US 22:00 Europe/Berlin zwangsweise schließen.",
    )
    st.session_state.flatten_buffer_min = st.select_slider(
        "Close Preparation beginnt X Min vor Schluss",
        options=[5, 10, 15, 20, 30, 45, 60],
        value=st.session_state.flatten_buffer_min,
        help=(
            f"Ab hier: Risikoreduktion (keine neuen Käufe, Stops werden verschärft). "
            f"Der tatsächliche Zwangs-Close (Final Close) greift erst in den letzten "
            f"{CLOSE_FINAL_MINUTES} Minuten davon."
        ),
    )
    st.divider()


@st.fragment
def _render_sidebar_region_index_catalog():
    """Region-Auswahl + Index + Katalog-Picker als EIN gemeinsames Fragment
    (2026-09) - session_state.region/index werden ausschließlich hier und im
    Katalog-Picker selbst gelesen (geprüft: kein Treffer außerhalb der Sidebar),
    ein Wechsel muss also nirgendwo sonst auf der Seite sofort sichtbar werden.
    Die beiden st.rerun()-Aufrufe unten liefen bisher als volle Reruns; jetzt
    scope='fragment', da sie nur dafür sorgen, dass die Index-Selectbox/das
    Katalog-Dropdown INNERHALB dieses Fragments den neuen Default zeigen -
    kein Grund, dafür den Rest der Seite (Chart, Watchlist, Ranker, Journal)
    mit neu zu laden.
    Falls 'scope=\"fragment\"' in eurer Streamlit-Version nicht existiert: das
    Argument einfach weglassen (st.rerun()) - degradiert dann exakt auf das
    bisherige Verhalten für diese zwei Stellen, nichts bricht dadurch.
    """
    # Handelszeiten passend zu session_windows()/session_close_for() (Europe/Berlin) -
    # dieselben Werte, die auch die Session-Erkennung (PRE/OPEN/MID/CLOSE) verwendet,
    # damit die Anzeige hier nicht unabhängig von der tatsächlichen Logik abweichen kann.
    REGION_HOURS = {
        "USA": "15:30–22:00 Europe/Berlin",
        "UK": "09:00–17:30 Europe/Berlin",
        "EU": "09:00–17:30 Europe/Berlin",
        "Schweiz": "09:00–17:30 Europe/Berlin",
    }
    # ":gray[...]" ist Streamlits eingebaute Markdown-Farbsyntax - färbt nur die
    # Handelszeiten hellgrau, ohne den Rest des Labels (Flagge+Name) zu beeinflussen.
    # Ampel-Punkt statt Tür/Schloss: 🟢 aktiver Handel, 🟡 Vorbörse-/Schlussnähe,
    # 🔴 Nacht/Wochenende/Feiertag (s. region_status_color()).
    _region_dot = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
    region_labels = [
        f"{_region_dot[region_status_color(r)]} {REGION_FLAGS.get(r, r)} "
        f":gray[({REGION_HOURS.get(r, '')})]"
        for r in REGION_ORDER
    ]
    region_map = {
        f"{_region_dot[region_status_color(r)]} {REGION_FLAGS.get(r, r)} "
        f":gray[({REGION_HOURS.get(r, '')})]": r
        for r in REGION_ORDER
    }

    region_label = st.radio(
        "Region",
        region_labels,
        index=REGION_ORDER.index(st.session_state.region),
        key="sidebar_region",
    )
    region = region_map.get(region_label, region_label)
    if region != st.session_state.region:
        st.session_state.region = region
        new_index = list(UNIVERSE[region].keys())[0]
        st.session_state.index = new_index
        # Das Index-Selectbox-Widget besitzt einen eigenen Key ("sidebar_index"), der
        # nach dem ersten Rendern Vorrang vor dem index=-Parameter hat. Ohne dieses
        # Nachziehen würde die Selectbox weiter den alten (in der neuen Region evtl.
        # gar nicht mehr vorhandenen) Wert aus ihrem Key anzeigen.
        st.session_state["sidebar_index"] = new_index
        # Katalog-Auswahl beruht auf der alten Region/Index-Titelliste -> zurücksetzen,
        # sonst zeigt/verwendet das Auswahlfeld einen nicht mehr existierenden Eintrag.
        st.session_state.pop("sidebar_catalog_pick", None)
        save_app_state()
        st.rerun(scope="fragment")

    indexes = list(UNIVERSE[st.session_state.region].keys())
    try:
        idx_default = indexes.index(st.session_state.index)
    except ValueError:
        idx_default = 0
    idx = st.selectbox("Index", indexes, index=idx_default, key="sidebar_index")
    if idx != st.session_state.index:
        st.session_state.index = idx
        st.session_state.pop("sidebar_catalog_pick", None)
        save_app_state()
        st.rerun(scope="fragment")

    # High-Vol-Filter deaktiviert (2026-09, auf ausdrücklichen Wunsch): 1-Jahres-
    # Historie ist für Daytrading die falsche Zeitskala (mean-reverting, glättet
    # Regime-Wechsel weg), Hebel relativiert die Notwendigkeit hoher Basiswert-
    # Volatilität als Opportunitäts-Quelle, und hohe Vola erhöht bei Hebelprodukten
    # zusätzlich das KO-Risiko. ATR% (bereits an anderer Stelle im Einsatz) ist die
    # inhaltlich passendere, kurzfristigere Vola-Kennzahl. Bewusst nur auskommentiert,
    # nicht gelöscht - falls doch benötigt, hier + die zugehörigen Stellen in
    # _build_watchlist_core(), score_open_candidate() und den Flash-Hinweisen unten
    # wieder einkommentieren.
    # st.session_state.high_vol_only = st.toggle(
    #     "Nur High Volatile anzeigen", value=st.session_state.high_vol_only, key="sidebar_hv"
    # )
    # if st.session_state.high_vol_only:
    #     # Fixer Schwellwert (45%) griff bei einer ohnehin auf Momentum-/Daytrading-
    #     # Kandidaten kuratierten Watchlist kaum sichtbar, da die meisten Titel diese
    #     # Schwelle sowieso überschreiten. Einstellbar machen, damit der Filter
    #     # tatsächlich trennscharf genutzt werden kann.
    #     st.session_state["high_vol_threshold"] = st.slider(
    #         "Schwelle (1J-Volatilität, %)",
    #         min_value=10.0,
    #         max_value=120.0,
    #         value=float(st.session_state.get("high_vol_threshold", HIGH_VOL_THRESHOLD)),
    #         step=5.0,
    #         key="sidebar_hv_threshold",
    #         on_change=refresh_market_data,
    #     )
    # st.caption(
    #     f"Anzeigesfilter ab {st.session_state.get('high_vol_threshold', HIGH_VOL_THRESHOLD):.0f}% 1J-Vol "
    #     "· Wert steht je Titel in der Spalte „Vol 1J %“"
    # )
    st.session_state.high_vol_only = False  # erzwungen, falls aus alter Session/DB noch True persistiert

    _render_sidebar_catalog_picker()


def _render_sidebar_catalog_picker():
    """Katalog-Auswahl als eigenes Fragment (2026-09, gleicher Grund wie die
    Risiko-Sektion): das bloße Auswählen im Dropdown löste bisher schon einen
    Komplett-Rerun aus, obwohl dabei noch gar nichts zur Watchlist hinzugefügt
    wird. pick/catalog/addable hängen nur an session_state (region/index/
    watchlist), keine externen Closures nötig.
    Wichtig: das eigentliche Hinzufügen-Ergebnis MUSS auf der Hauptseite
    (Watchlist-Tabelle) sichtbar werden - deshalb hier bewusst ein explizites
    st.rerun() nach erfolgreichem add_to_watchlist(), das (anders als die
    automatische Fragment-Reaktion) einen vollen Rerun erzwingt. Ohne das würde
    der neue Titel im Fragment selbst nicht sichtbar erscheinen, sondern erst
    beim nächsten Klick, der ohnehin einen vollen Rerun auslöst.
    """
    catalog = UNIVERSE[st.session_state.region][st.session_state.index]
    owned = {w["ticker"] for w in st.session_state.watchlist}
    addable = sorted(
        (f"{s['ticker']} · {s['name']}" for s in catalog if s["ticker"] not in owned),
        key=lambda s: s.split(" · ", 1)[1].lower(),
    )
    pick = st.selectbox("Titel aus Katalog", ["—"] + addable, key="sidebar_catalog_pick")
    btn_disabled = pick == "—"
    if st.button("➕ Aus Katalog hinzufügen", disabled=btn_disabled, key="sidebar_add_catalog", use_container_width=True):
        ticker = pick.split(" · ")[0]
        item = catalog_lookup(ticker)
        if item:
            ok = add_to_watchlist(item["ticker"], item["yf"], item["name"])
            if ok:
                st.toast(f"✅ {item['ticker']} zur Watchlist hinzugefügt", icon="📌")
                st.rerun()


@st.fragment
def _render_sidebar_manual_ticker():
    """Manuelle Ticker-Eingabe als eigenes Fragment (2026-09, gleicher Grund wie
    der Katalog-Picker direkt darüber) - Tippen im Textfeld löste bisher schon
    einen Komplett-Rerun pro Tastenanschlag aus. Voller Rerun bleibt nötig, sobald
    tatsächlich zur Watchlist hinzugefügt wird (s. explizites st.rerun() unten),
    damit die Watchlist-Tabelle auf der Hauptseite den neuen Titel zeigt."""
    if effective_pro_mode():
        raw = st.text_input("Ticker manuell", placeholder="AMD, TSLA, IFX.DE", key="sidebar_manual_ticker")
        if st.button("🔭 Ticker suchen & hinzufügen", key="sidebar_add_manual", use_container_width=True):
            with st.spinner("Ticker wird gesucht..."):
                found = resolve_symbol(raw)
            if not found:
                st.error("Ticker nicht gefunden. Prüfe das Kürzel oder versuche es mit Suffix (z.B. .DE).")
            else:
                ok = add_to_watchlist(found["ticker"], found["yf"], found["name"])
                if ok:
                    st.toast(f"✅ {found['ticker']} ({found['name']}) hinzugefügt", icon="📌")
                    st.rerun()
    else:
        st.caption(
            "🔒 Manuelle Ticker-Eingabe (freies Symbol, auch außerhalb des "
            "kuratierten Katalogs) ist nur im **Pro Modus** verfügbar — "
            "Schalter in den Einstellungen (⚙️) aktivieren."
        )


@st.fragment
def _render_watchlist_section(watch: pd.DataFrame):
    """Watchlist-Tabelle + Fokus setzen/Entfernen/Leeren als eigenes Fragment
    (2026-09, Testlauf): Der Klick auf eine Zeile (Haken, erste Spalte) löste
    bisher einen KOMPLETTEN Rerun aus, obwohl dabei nur lokal eine Zeile markiert
    wird - noch kein Fokuswechsel. Mit @st.fragment läuft die Zeilenauswahl jetzt
    isoliert (nur dieser Block reagiert), spürbar schneller.
    "Fokus setzen"/"Entfernen"/"Watchlist leeren" rufen weiterhin explizit
    st.rerun() auf - das erzwingt (anders als die automatische Fragment-Reaktion)
    trotzdem einen vollen Seiten-Rerun, weil Chart/Ticket/News dem neuen Fokus
    folgen müssen bzw. sich der Watchlist-Umfang ändert. Ergebnis: aus "Zeile
    anklicken (Rerun) → Fokus setzen (noch ein Rerun)" wird "Zeile anklicken
    (kostenlos) → Fokus setzen (ein Rerun)".
    `watch` wird als Parameter übergeben statt hier neu berechnet, damit ein
    isolierter Fragment-Rerun (z.B. beim reinen Zeilen-Klick) nicht versehentlich
    einen frischen build_watchlist()-Durchlauf auslöst - die Zeilenauswahl soll
    ja gerade OHNE neuen Datenabruf reagieren.
    """
    focus = st.session_state.get("focus")
    st.markdown('<div id="nav-watchlist"></div>', unsafe_allow_html=True)
    st.subheader("Watchlist")
    st.caption("Zeile anklicken, dann „🎯 Fokus setzen” → Chart, News und Katalog folgen diesem Titel.")
    wl_c1, wl_c2, wl_c3 = st.columns(3)
    with wl_c1:
        wl_region_options = ["ALL"] + REGION_ORDER
        wl_region = st.selectbox(
            "Region",
            wl_region_options,
            format_func=lambda r: "Alle Regionen" if r == "ALL" else REGION_FLAGS.get(r, r),
            key="wl_region_filter",
        )
    with wl_c2:
        wl_sector_options = ["ALL"] + sorted(set(SECTOR_MAP.values()) | {"Sonstige"})
        wl_sector = st.selectbox(
            "Sektor",
            wl_sector_options,
            format_func=lambda s: "Alle Sektoren" if s == "ALL" else s,
            key="wl_sector_filter",
        )
    with wl_c3:
        all_index_names = sorted({idx for region_dict in UNIVERSE.values() for idx in region_dict.keys()})
        wl_index_options = ["ALL"] + all_index_names
        wl_index = st.selectbox(
            "Index",
            wl_index_options,
            format_func=lambda i: "Alle Indizes" if i == "ALL" else i,
            key="wl_index_filter",
        )
    hidden_count = st.session_state.get("_watchlist_hidden_count", 0)
    # High-Vol-Filter deaktiviert (s. Begründung an der Sidebar-Toggle-Stelle) -
    # hidden_count ist jetzt dauerhaft 0, dieser Hinweis würde ohnehin nie mehr feuern.
    # if hidden_count:
    #     st.caption(
    #         f"ℹ️ {hidden_count} Titel durch den Filter „Nur High Volatile anzeigen” ausgeblendet "
    #         "(steht in der Watchlist, ist aber unterhalb der Vola-Schwelle bzw. ohne 1J-Vola-Daten). "
    #         "Filter links deaktivieren, um alle Titel zu sehen."
    #     )
    failed_quotes = st.session_state.get("_watchlist_failed_quotes") or []
    if failed_quotes:
        st.warning(
            f"⚠️ Kein Kurs-Feed für: {', '.join(failed_quotes)} — Zeile(n) zeigen leere "
            "Kurs-/Prozent-Werte. Ggf. Refresh Now versuchen oder Yahoo/Finnhub-Status prüfen."
        )
    watch_display = watch
    if wl_region != "ALL" or wl_sector != "ALL" or wl_index != "ALL":
        keep_tickers = []
        for item in st.session_state.watchlist:
            region, idx_name = find_region_index(item["ticker"])
            sector = get_sector(item["ticker"])
            if wl_region != "ALL" and region != wl_region:
                continue
            if wl_sector != "ALL" and sector != wl_sector:
                continue
            if wl_index != "ALL" and idx_name != wl_index:
                continue
            keep_tickers.append(item["ticker"])
        watch_display = watch[watch["Ticker"].isin(keep_tickers)].reset_index(drop=True)
    if watch.empty:
        # High-Vol-Filter deaktiviert (s. Begründung an der Sidebar-Toggle-Stelle) -
        # hidden_count ist dauerhaft 0, der if-Zweig würde nie mehr greifen.
        # if hidden_count:
        #     st.info(
        #         f"Alle {hidden_count} Titel in der Watchlist sind aktuell durch den "
        #         "„Nur High Volatile”-Filter ausgeblendet. Filter links deaktivieren, um sie zu sehen."
        #     )
        # else:
        st.info("Watchlist leer. Füge links einen Titel hinzu.")
    elif watch_display.empty:
        st.info("Kein Titel in der Watchlist entspricht dem Region-/Sektor-/Index-Filter.")
    else:
        show = watch_display.drop(columns=["yf"]).reset_index(drop=True)
        selected_idx = 0
        if focus in list(show["Ticker"]):
            selected_idx = list(show["Ticker"]).index(focus)

        # Volumen/Market Cap als vorformatierte Strings (2026-09) - bewusst NICHT
        # über column_config-NumberColumn-Formatstrings gelöst, weil das mit dem
        # Styler unten (Text-Farbe für 1D %/Δ 52W-Tief %) kollidieren könnte und
        # ich das lokal nicht gegen echtes Streamlit testen kann.
        show["Volumen"] = show["Volumen"].apply(_fmt_compact_number)
        show["Market Cap"] = show["Market Cap"].apply(_fmt_compact_number)

        def _pct_text_color(v):
            if pd.isna(v):
                return ""
            if v > 0:
                return "color: #22c55e"
            if v < 0:
                return "color: #ff5d6c"
            return ""

        styled_show = show.style.map(
            _pct_text_color, subset=["1D %", "Δ 52W-Tief %"]
        ).format(
            {
                "Kurs": "{:.2f}",
                "1D %": "{:+.2f}",
                "ATR(14)": "{:.2f}",
                "High": "{:.2f}",
                "Low": "{:.2f}",
                "Δ 52W-Tief %": "{:+.2f}",
            },
            na_rep="—",
        )

        event = st.dataframe(
            styled_show,
            use_container_width=True,
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key="watch_table",
            column_config={
                "Region": st.column_config.TextColumn("Region", width="small"),
                "Sektor": st.column_config.TextColumn("Sektor", width="small"),
                "Volumen": st.column_config.TextColumn("Volumen", width="small"),
                "Market Cap": st.column_config.TextColumn("Market Cap", width="small"),
            },
        )
        rows = []
        rows = get_selected_rows(event)
        # andernfalls bleibt rows = []
        # Bounds-Check: Die Zeilenauswahl bleibt über den key "watch_table" auch nach einem
        # Rerun bestehen. Wird die Watchlist zwischenzeitlich kürzer (z.B. durch Löschen
        # eines Titels), kann der alte Row-Index außerhalb der neuen Tabelle liegen und
        # "show.iloc[rows[0]]" wirft dann einen IndexError.
        row_selected = bool(rows) and rows[0] < len(show)
        selected_ticker = show.iloc[rows[0]]["Ticker"] if row_selected else None
        # Ohne on_select="rerun" markiert ein Klick in die Tabelle die Zeile nur noch lokal
        # (kein App-Rerun mehr pro Klick). Der Fokus wird erst übernommen, wenn explizit auf
        # "Fokus setzen" geklickt wird - das spart bei häufigem Navigieren in der Watchlist
        # spürbar viele unnötige Reruns.

        # "Aktiv"-Anzeige jetzt ganz links, vor "Fokus setzen" (2026-09, auf Wunsch
        # verschoben - vorher rechts außen als kleine Caption), mit größerer Schrift.
        c_aktiv, c_a, c_b, c_c = st.columns(4)
        with c_aktiv:
            st.markdown(
                f'<p style="font-size:1.1rem;font-weight:600;margin:0.4rem 0 0 0;">'
                f'Aktiv: {focus}</p>',
                unsafe_allow_html=True,
            )
        with c_a:
            set_focus_disabled = not row_selected or selected_ticker == focus
            if st.button(
                "🎯 Fokus setzen",
                disabled=set_focus_disabled,
                key="btn_set_focus",
                use_container_width=True,
            ):
                st.session_state.focus = selected_ticker
                save_app_state()
                st.rerun()
        with c_b:
            # Bugfix (2026-09): entfernte bisher immer den FOKUS-Titel (Button-Key
            # "btn_remove_focus"), nicht den in der Tabelle ausgewählten - wich
            # voneinander ab, sobald man eine Zeile anklickt, ohne vorher explizit
            # "Fokus setzen" zu drücken. remove_from_watchlist() selbst behandelt
            # den Fokus-Fallback (falls der entfernte Titel zufällig der Fokus war)
            # bereits korrekt - hier muss nur auf die tatsächliche Auswahl umgestellt
            # werden, analog zu "Fokus setzen" oben.
            remove_disabled = not row_selected
            if st.button("🗑️ Entfernen", disabled=remove_disabled, key="btn_remove_focus", use_container_width=True):
                removed_ticker = selected_ticker
                remove_from_watchlist(selected_ticker)
                # Ausgewählte Zeile zurücksetzen, da sich die Tabellengröße/-reihenfolge
                # geändert hat und der alte Auswahl-Index sonst ins Leere/Falsche zeigt.
                st.session_state.pop("watch_table", None)
                st.toast(f"🗑️ {removed_ticker} aus Watchlist entfernt", icon="🗑️")
                save_app_state()
                st.rerun()
        with c_c:
            clear_disabled = not st.session_state.get("watchlist")
            if st.button("🧹 Watchlist leeren", disabled=clear_disabled, key="btn_clear_watchlist", use_container_width=True):
                st.session_state.watchlist = []
                st.session_state.pop("watch_table", None)
                st.toast("🧹 Watchlist geleert", icon="🧹")
                save_app_state()
                st.rerun()

    st.caption(
        "Watchlist ist deine Arbeitsliste, nicht der ganze Index. "
        "Zeile anklicken und „🎯 Fokus setzen” drücken, um Chart, News und Ticket zu wechseln."
    )


def dashboard():
    _disable_browser_back()
    _maybe_show_critical_trade_events_dialog()
    # last_refresh_ts jetzt bei JEDEM vollen dashboard()-Durchlauf aktualisiert
    # (2026-09, auf Wunsch), nicht mehr nur beim expliziten Klick auf "Refresh
    # Now" (das bleibt zusätzlich der einzige Weg, der auch die Live-Caches
    # tatsächlich leert - hier geht es nur um den angezeigten Zeitstempel).
    # Absichtlich NICHT in den Sidebar-Fragmenten (Risiko/Katalog/Ranker-Checkbox)
    # oder im Watchlist-Fragment platziert: deren isolierte Reruns sollen ja
    # gerade NICHT den vollen Seitenzustand widerspiegeln.
    st.session_state["last_refresh_ts"] = datetime.now(TZ_BERLIN)
    # Defensive CSS gegen Overflow in modalen Dialogen (@st.dialog) auf kleinen
    # Bildschirmen: erzwingt Zeilenumbruch statt horizontalem Überlauf bei langen,
    # ungetrennten Werten (z.B. ISIN/WKN-Kombinationen) und begrenzt Bilder/Tabellen
    # im Dialog hart auf die verfügbare Breite. Kein konkreter Überlauf-Bug in den
    # aktuellen Dialogen gefunden (alle nutzen bereits responsive st.columns ohne
    # feste Pixel-Breiten), aber als Sicherheitsnetz ergänzt.
    st.markdown(
        """
        <style>
        div[data-testid="stDialog"] * {
            overflow-wrap: break-word !important;
            word-break: break-word !important;
        }
        div[data-testid="stDialog"] img,
        div[data-testid="stDialog"] table,
        div[data-testid="stDialog"] [data-testid="stDataFrame"] {
            max-width: 100% !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    # --- AUTO-REFRESH ---
    # Auto-Refresh löst nur einen Rerun aus. Live-Caches behalten ihre TTL
    # (Quotes 60s, Intraday 60s, Chart-Figure 330s). Voller Cache-Reset nur
    # noch über "Refresh Now" — sonst baut jeder Tick Watchlist+Chart neu.
    refresh_sec = int(st.session_state.get("refresh_sec", 300))
    autorefresh_failed = False
    if HAS_AUTOREFRESH:
        try:
            # Die Komponente selbst kann zur Laufzeit fehlschlagen (z.B. Frontend-/
            # Backend-Versions-Mismatch der Custom-Component in manchen Hosting-Umgebungen) -
            # das war vorher ungefangen und hätte das komplette Dashboard zum Absturz
            # gebracht. Jetzt: sauber auf "kein Auto-Refresh" zurückfallen statt crashen.
            tick = st_autorefresh(interval=refresh_sec * 1000, key="volt_auto_refresh")
            st.session_state["_last_autorefresh_tick"] = tick
        except Exception as e:
            autorefresh_failed = True
            st.session_state["_autorefresh_error"] = str(e)
    if not HAS_AUTOREFRESH or autorefresh_failed:
        # Deutlich sichtbare Warnung statt einer kleinen, leicht übersehbaren Caption -
        # ein fehlendes/fehlerhaftes Auto-Refresh sollte nicht stillschweigend untergehen.
        reason = (
            f"Paket-Fehler zur Laufzeit: {st.session_state.get('_autorefresh_error')}"
            if autorefresh_failed
            else "Paket 'streamlit-autorefresh' ist in dieser Umgebung nicht installiert."
        )
        st.warning(
            f"⚠️ Auto-Refresh inaktiv – {reason}\n\n"
            "Prüfe, ob `streamlit-autorefresh` in requirements.txt eingetragen UND die "
            "App danach neu deployed/gerebootet wurde (ein reines Secrets-/Code-Update "
            "ohne Neustart installiert keine neuen Pakete nach). Bis dahin bitte manuell "
            "über den Reload-Button aktualisieren."
        )

    st.markdown(APP_CSS, unsafe_allow_html=True)

    # Fette Button-Texte (2026-09, auf Wunsch) - separat vom Farb-Block unten
    # gehalten, damit ein Rollback einzeln möglich ist, falls nur eins von
    # beidem Probleme macht. Nur font-weight, keine Farben/Hintergründe -
    # geringeres Risiko einer Kollision mit Streamlits Disabled-Styling als
    # beim früheren Türkis-Versuch (der genau daran gescheitert war).
    st.markdown(
        """
        <style>
        .stButton > button, .stLinkButton > a, .stFormSubmitButton > button,
        .stButton > button p, .stLinkButton > a p, .stFormSubmitButton > button p {
            font-weight: 700 !important;
        }
        /* Absicherung speziell für "Komplett-Scan" (2026-09): wurde trotz der
        generischen Regel oben zweimal als nicht fett gemeldet - Ursache über
        Code-Analyse nicht auffindbar (Button ist ein ganz normaler st.button()
        ohne Sonderbehandlung). Zusätzlicher, unabhängiger Signalweg über
        aria-label statt Klasse - dasselbe Muster, das beim Trading-Pause-
        Button hier im Code bereits nachweislich funktioniert. */
        button[aria-label*="Komplett-Scan"] {
            font-weight: 700 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Button-Farb-Ausnahmen (2026-09): Kauf-Familie + Login grün, Verkauf-Familie
    # + Logout rot, Kill Switch bewusst unangetastet. Das globale Türkis-Default
    # für ALLE Buttons wurde wieder entfernt (sah schlecht aus UND hat nebenbei
    # die Handelswoche/-monat/-jahr-Buttons unsichtbar gemacht: die sind meist
    # disabled, und die erzwungene Text-/Hintergrundfarbe hat mit Streamlits
    # eigenem Disabled-Styling kollidiert - ohne den globalen Block tritt das
    # nicht mehr auf). Nur noch die gezielten Ausnahmen unten, über die von
    # Streamlit automatisch vergebene "st-key-<key>"-Klasse pro Widget-Key.
    st.markdown(
        """
        <style>
        [class*="st-key-rec_buy_btn"] button,
        [class*="st-key-buy_LONG"] button,
        [class*="st-key-buy_SHORT"] button,
        [class*="st-key-add_"] button,
        [class*="st-key-trade_confirm_buy"] button,
        [class*="st-key-confirm_daily_setup"] button,
        [class*="st-key-auth_login_btn"] a {
            background-color: #22c55e !important;
            border-color: #22c55e !important;
            color: #05070c !important;
        }
        [class*="st-key-rec_buy_btn"] button:hover,
        [class*="st-key-buy_LONG"] button:hover,
        [class*="st-key-buy_SHORT"] button:hover,
        [class*="st-key-add_"] button:hover,
        [class*="st-key-trade_confirm_buy"] button:hover,
        [class*="st-key-confirm_daily_setup"] button:hover,
        [class*="st-key-auth_login_btn"] a:hover {
            background-color: #34d774 !important;
            border-color: #34d774 !important;
        }
        [class*="st-key-sell_"] button,
        [class*="st-key-sell50_"] button,
        [class*="st-key-sellall_"] button,
        [class*="st-key-trade_confirm_sell"] button,
        [class*="st-key-flatten_all_btn"] button,
        [class*="st-key-settings_delete_account_final"] button,
        [class*="st-key-auth_logout_btn"] button {
            background-color: #ef4444 !important;
            border-color: #ef4444 !important;
            color: #ffffff !important;
        }
        [class*="st-key-sell_"] button:hover,
        [class*="st-key-sell50_"] button:hover,
        [class*="st-key-sellall_"] button:hover,
        [class*="st-key-trade_confirm_sell"] button:hover,
        [class*="st-key-flatten_all_btn"] button:hover,
        [class*="st-key-settings_delete_account_final"] button:hover,
        [class*="st-key-auth_logout_btn"] button:hover {
            background-color: #f87171 !important;
            border-color: #f87171 !important;
        }
        [class*="st-key-kill_switch_btn"] button,
        [class*="st-key-kill_confirm"] button {
            background-color: rgb(255, 75, 75) !important;
            color: #ffffff !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Prominenter Hinweis ganz oben: Demo- vs. Produktiv-Modus. Guest-Modus
    # speichert bewusst nichts (s. current_user_id()/save_*-Funktionen) - das
    # sollte hier direkt sichtbar sein, nicht erst beim Reload verloren gehen.
    if is_authenticated():
        st.success("🟢 **Produktiv-Modus** — eingeloggt, Aktivitäten werden gespeichert.")
    else:
        st.warning("🟡 **Demo-Modus** — nicht eingeloggt, Aktivitäten werden **nicht** gespeichert.")

    with st.sidebar:
        _pro_mode_active = effective_pro_mode()
        _active_logo_uri = _logo_data_uri(_pro_mode_active)
        # Der separate Demo-/Pro-Modus-Hinweis oben in der Sidebar entfällt - statt-
        # dessen wechselt im Pro Modus direkt das Logo (VoltDesk -> VoltDesk Pro).

        # Logo öffnet jetzt die Landingpage in einem neuen Tab (2026-09, auf
        # Wunsch geändert - vorher ein In-App-@st.dialog, s. Historie unten).
        # Bewusst ein echter <a target="_blank">-Link statt eines st.button() mit
        # window.open()-Aufruf: ein direkter Link-Klick läuft nativ im Browser und
        # umgeht damit genau die Popup-Blocker-Problematik, die ein durch einen
        # Streamlit-Rerun ausgelöstes window.open() hätte (deswegen war hier vorher
        # extra ein In-App-Dialog gebaut worden - mit einem echten <a> braucht es
        # das nicht mehr).
        st.markdown(
            f"""
            <style>
            .voltdesk-logo-link {{
                display:flex;
                justify-content:center;
                margin-top:-14px;
                width: 100%;
            }}
            .voltdesk-logo-link a {{
                background-image: url("{_active_logo_uri}");
                background-size: contain;
                background-repeat: no-repeat;
                background-position: center;
                width: 220px;
                height: 164px;
                display: block;
                margin: 0 auto;
            }}
            .voltdesk-logo-link a:hover {{
                opacity: 0.85;
            }}
            </style>
            <div class="voltdesk-logo-link">
                <a href="https://kaisersoft.github.io/website/voltdesk-landingpage/#pricing"
                   target="_blank" rel="noopener noreferrer" aria-label="VoltDesk Preise"></a>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown(
            """
            <div style="text-align:center;padding:0 0 0.55rem 0;">
              <div style="margin-top:0.5rem;display:inline-flex;align-items:center;gap:0.4rem;
                          padding:0.28rem 0.75rem 0.28rem 0.45rem;border-radius:999px;
                          background:#0a0a0a;border:1px solid rgba(0,229,255,0.4);
                          box-shadow:0 0 10px rgba(0,229,255,0.15);
                          color:#e2e8f0;font-size:0.78rem;font-weight:500;">
                <img src="https://avatars.githubusercontent.com/u/316974313?v=4"
                     style="width:16px;height:16px;border-radius:50%;display:inline-block;
                            vertical-align:middle;" alt="Kaisersoft" />
                Made by <strong style="color:#00e5ff;"><a href="http://www.kaisersoft.info" target="_blank" style="color: #00e5ff; text-decoration: none;">Kaisersoft.ai</a></strong>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown(
            f'<p style="text-align:center;color:#8b9bb2;font-size:0.8rem;'
            f'margin:0.125rem 0 0.15rem 0;">v{VERSION} · Build: {get_build()}</p>',
            unsafe_allow_html=True,
        )

        st.markdown(
            '<hr style="margin:0.35rem 0;border:none;border-top:1px solid rgba(139,155,178,0.25);" />',
            unsafe_allow_html=True,
        )

        _maybe_show_stripe_confirmation()
        render_auth_ui()
        render_subscription_ui()

        st.markdown(
            '<hr style="margin:0.35rem 0;border:none;border-top:1px solid rgba(139,155,178,0.25);" />',
            unsafe_allow_html=True,
        )

        st.session_state.mode_override = st.radio(
            "Modus",
            ["Auto", "PRE", "OPEN", "MID", "CLOSE"],
            horizontal=True,
            key="sidebar_mode_override",
            index=["Auto", "PRE", "OPEN", "MID", "CLOSE"].index(
                st.session_state.get("mode_override", "Auto")
            ),
            help="Auto folgt der Börsenzeit des Fokus-Titels.",
        )
        # ... und alle weiteren Sidebar-Elemente (Katalog, Risiko, Session, ...)
        st.markdown(
            '<hr style="margin:0.25rem 0 0.15rem 0;border:none;border-top:1px solid rgba(139,155,178,0.2);" />',
            unsafe_allow_html=True,
        )
        ar_l, ar_r = st.columns([1.4, 1])
        with ar_l:
            st.markdown(
                "<p style='margin:0;padding-top:6px;font-size:0.8rem;"
                "color:#8b9bb2;white-space:nowrap;'>Auto-Refresh (s)</p>",
                unsafe_allow_html=True,
            )
        with ar_r:
            _refresh_opts = [60, 90, 120, 180, 300, 600, 900, 1800]
            _cur_refresh = int(st.session_state.get("refresh_sec", 300))
            if _cur_refresh not in _refresh_opts:
                _cur_refresh = min(_refresh_opts, key=lambda x: abs(x - _cur_refresh))
            refresh_sec = st.select_slider(
                "s",
                label_visibility="collapsed",
                options=_refresh_opts,
                value=_cur_refresh,
                key="refresh_sec_input",
            )
            st.session_state.refresh_sec = refresh_sec
        # Last-Refresh-Hinweis (2026-09): jetzt über statt unter dem Refresh-Now-
        # Button, Text durchgehend grau statt grün/gelb/rot - Ampelfarbe wird
        # stattdessen als vorangestelltes ⚠️ ab "gelb" (>=10 Min.) gezeigt.
        _last_refresh = st.session_state.get("last_refresh_ts")
        if _last_refresh:
            _age_min = (datetime.now(TZ_BERLIN) - _last_refresh).total_seconds() / 60.0
            _refresh_warn = "⚠️ " if _age_min >= 10 else ""
            _last_refresh_str = _last_refresh.strftime("%d.%m.%Y %H:%M:%S")
        else:
            _refresh_warn = ""
            _last_refresh_str = "—"
        st.markdown(
            f'<p style="color:#8b9bb2;font-size:0.8rem;margin:0 0 0.2rem 0;text-align:center;">'
            f'{_refresh_warn}Last Refresh: {_last_refresh_str}</p>',
            unsafe_allow_html=True,
        )
        if st.button("🔄 Refresh Now", use_container_width=True):
            # Vorher wurden hier nur 3 von 6 Live-Caches geleert (fetch_quote,
            # fetch_levels, fetch_events fehlten) - die Watchlist-Kurse und
            # Levels/Events waren also von einem "vollen" Reload gar nicht
            # betroffen. refresh_market_data() deckt alle Caches ab.
            refresh_market_data()
            st.rerun()
        fh_ok = bool(get_finnhub_key())
        if not fh_ok:
            # Caption-Status entfernt (redundant zu anderen Stellen im Dashboard) -
            # der Diagnose-Expander bleibt als reines Troubleshooting-Werkzeug erhalten
            # und erscheint nur, wenn tatsächlich ein Problem vorliegt.
            found_names = debug_secret_key_names()
            with st.expander("Finnhub-Key nicht erkannt – warum?", expanded=False):
                if not found_names:
                    st.caption(
                        "st.secrets liefert überhaupt keine Einträge. Prüfe, ob in "
                        "Streamlit Cloud unter App → Settings → Secrets etwas gespeichert "
                        "und die App danach neu gestartet wurde (Secrets werden erst nach "
                        "einem Neustart/Reboot der App geladen)."
                    )
                else:
                    st.caption("Gefundene Secret-Namen (Werte werden nie angezeigt):")
                    st.code("\n".join(found_names), language=None)
                    st.caption(
                        "Ist dein Finnhub-Key hier nicht als Name mit 'finnhub' UND "
                        "'key'/'token' gelistet, wird er nicht erkannt - benenne ihn z.B. "
                        "in FINNHUB_API_KEY um oder lege ihn unter [finnhub] key=\"...\" ab."
                    )
        # Custom <hr> statt st.divider(): Streamlits eigener Divider bringt beidseitig
        # ca. 1rem Rand mit - hier auf die Hälfte reduziert, wie gewünscht.
        st.markdown(
            '<hr style="margin:0.5rem 0;border:none;border-top:1px solid rgba(139,155,178,0.25);" />',
            unsafe_allow_html=True,
        )
        st.markdown(f"#### Katalog ({UNIVERSE_TICKER_COUNT} Titel im Universum)")
        st.caption("Hier wählen, dann zur Watchlist hinzufügen.")

        # Region + Index + Katalog-Picker jetzt als ein gemeinsames @st.fragment
        # (s. _render_sidebar_region_index_catalog(), 2026-09-Testlauf) - Region-
        # /Index-Wechsel lösen keinen Komplett-Rerun der Seite mehr aus, da
        # session_state.region/index nirgendwo außerhalb der Sidebar gelesen wird.
        _render_sidebar_region_index_catalog()

        # Komplett-Scan (2026-09): scannt das GESAMTE Universum parallel (5 Worker,
        # s. PARALLEL_FETCH_WORKERS) statt nur die Watchlist, und aktualisiert dabei
        # PRE-Picker (deckt bereits das ganze Universum ab) und OPEN-Ranker (Cache
        # geleert, wird beim nächsten Rendern der Sektion neu berechnet - dann mit
        # bereits warmen Fetch-Caches, also schnell). Zeigt die tatsächliche Laufzeit
        # an, um das "<1 Minute fürs ganze Universum"-Ziel later konkret zu testen.
        if st.button(
            f"🌌 Komplett-Scan ({UNIVERSE_TICKER_COUNT} Titel)",
            key="sidebar_full_universe_scan",
            use_container_width=True,
            help="Scannt das gesamte Universum parallel und aktualisiert PRE- und OPEN-Empfehlungen.",
        ):
            _scan_start = datetime.now(TZ_BERLIN)
            with st.spinner(f"Scanne {UNIVERSE_TICKER_COUNT} Titel parallel ({PARALLEL_FETCH_WORKERS} Worker)…"):
                for _fn in (
                    fetch_quote,
                    fetch_intraday,
                    fetch_levels,
                    fetch_news,
                    fetch_index_day_chg,
                    fetch_focus_quote,
                ):
                    if hasattr(_fn, "clear"):
                        _fn.clear()
                scan_pre_stock_picker.clear()
                _sources = enabled_news_sources()
                st.session_state["_pre_picker_results"] = scan_pre_stock_picker(_sources, "ALL")
                st.session_state["_pre_picker_scanned_at_dt"] = datetime.now(TZ_BERLIN)
                st.session_state["_open_ranker_cache"] = {}
            _elapsed = (datetime.now(TZ_BERLIN) - _scan_start).total_seconds()
            st.success(
                f"✅ Komplett-Scan fertig in {_elapsed:.1f}s "
                f"({UNIVERSE_TICKER_COUNT} Titel, {PARALLEL_FETCH_WORKERS} Worker parallel)."
            )

        st.divider()
        _render_sidebar_manual_ticker()

        # Risiko- und Session-Einstellungen jetzt in einem eigenen @st.fragment
        # (s. Funktion oben, 2026-09-Testlauf) - Slider hier lösen keinen
        # Komplett-Rerun der Seite mehr aus. pro_mode_active hier trotzdem noch
        # einmal frisch lesen, da weiter unten in dashboard() (Nachkauf-Block)
        # ebenfalls darauf zugegriffen wird und das lokal in der Fragment-Funktion
        # gesetzte pro_mode_active von dort aus nicht sichtbar ist.
        _render_sidebar_risk_settings()
        pro_mode_active = effective_pro_mode()
        # Trading Pause: keine neuen Positionen, bestehende Positionen bleiben vollständig managebar.
        pause_active = st.session_state.get("trading_pause", False)
        pause_label = "▶️ Trading Pause beenden" if pause_active else "⏸️ Trading Pause"
        st.markdown(
            """
            <style>
            button[aria-label="Trading Pause"],
            button[aria-label="Trading Pause beenden"] {
                background:#fff3b0 !important;
                border-color:#e5c84b !important;
                color:#4a3b00 !important;
                font-weight:700 !important;
            }
            button[aria-label="Trading Pause"]:hover,
            button[aria-label="Trading Pause beenden"]:hover {
                background:#ffe98a !important;
                border-color:#d8b72f !important;
                color:#3d3100 !important;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        if st.button(pause_label, key="trading_pause_btn", use_container_width=True):
            st.session_state.trading_pause = not pause_active
            save_app_state()
            # Kein st.rerun(): der Warnhinweis direkt darunter liest trading_pause bereits
            # frisch im selben Durchlauf, nichts oberhalb wurde schon mit dem alten Wert
            # gerendert - der Button-Klick hat den Rerun schon ausgelöst.
        st.caption("Pause = keine neuen Positionen. Daily Loss Limit = automatischer Risk Lock. Kill Switch = manuelle Notabschaltung plus Flatten.")
        if st.session_state.get("risk_lock", False) and not st.session_state.get("killed", False):
            st.error("DAILY LOSS LIMIT / RISK LOCK — keine neuen Trades bis zum Paper-Tag-Reset.")
        if st.session_state.get("trading_pause", False):
            st.warning("TRADING PAUSE — keine neuen Positionen. Offene Positionen bleiben managebar.")

        if not st.session_state.get("killed", False):
            if st.button("🛑 Kill Switch", type="primary", use_container_width=True, key="kill_switch_btn"):
                st.session_state.kill_confirm_pending = True
                # Kein st.rerun(): die Sicherheitsabfrage direkt darunter liest
                # kill_confirm_pending bereits frisch im selben Durchlauf.
        else:
            st.error("🔴 **KILL SWITCH AKTIV — Handel vollständig gesperrt.**")
        if st.session_state.get("kill_confirm_pending", False) and not st.session_state.get("killed", False):
            st.error("⚠️ **SICHERHEITSMELDUNG** — Der Kill Switch sperrt den Handel und schließt alle offenen Paper-Positionen. Dieser Schritt ist nicht rückgängig zu machen.")
            kc1, kc2 = st.columns(2)
            with kc1:
                if st.button("Abbrechen", key="kill_cancel", use_container_width=True):
                    st.session_state.kill_confirm_pending = False
                    st.rerun()
            with kc2:
                if st.button("Ja, Kill Switch aktivieren", type="primary", key="kill_confirm", use_container_width=True):
                    st.session_state.kill_confirm_pending = False
                    st.session_state.killed = True
                    # Glattstellung erfolgt zentral am Anfang von dashboard() (echter
                    # Emergency Close), sobald `killed` gesetzt ist - dort steht der
                    # aktuelle `watch`-Snapshot bereits zur Verfügung.
                    save_app_state()
                    st.rerun()

        st.divider()

        # Pro Modus - braucht Login UND aktiven Trial/Abo. Sichtbarer Schalter
        # lebt jetzt im Einstellungs-Dialog (ganz oben) - HIER bleibt bewusst nur
        # die stille Zwangsprüfung stehen: das muss bei JEDEM Seitenaufruf laufen
        # (nicht nur beim Öffnen des Dialogs), sonst bliebe Pro Modus nach
        # Trial-Ende/Kündigung bis zum nächsten Dialog-Öffnen fälschlich aktiv.
        if not has_pro_access():
            st.session_state.pro_mode = False
            if not is_authenticated():
                st.caption("🔐 Zum Freischalten des Pro-Modus bitte einloggen.")
            else:
                st.caption("🔒 Trial abgelaufen - Abo nötig für Pro Modus.")
        elif not st.session_state.get("_pro_mode_defaulted_once"):
            # Bugfix (2026-09): der Default True im init_state()-defaults-Dict
            # greift nur bei komplett NEUEN Sessions ohne gespeicherten State -
            # bestehende Nutzer mit bereits persistiertem pro_mode=False (aus der
            # Zeit vor dieser Änderung) erreichten diesen Default nie, die App
            # startete trotz aktivem Abo/Trial im Free-Modus. Deshalb hier:
            # einmalig (persistiert über _pro_mode_defaulted_once) automatisch
            # aktivieren, sobald Zugriff besteht - eine spätere manuelle
            # Aus-Schaltung wird danach respektiert, nicht jedes Mal überschrieben.
            st.session_state.pro_mode = True
            st.session_state["_pro_mode_defaulted_once"] = True

        if st.button("⚙️ Einstellungen", key="btn_open_settings", use_container_width=True):
            _settings_dialog()

        st.markdown(
            """
            <style>
            div[data-testid="stHorizontalBlock"]:has(.st-key-btn_open_impressum) {
                gap: 0;
            }
            .st-key-btn_open_datenschutz button,
            .st-key-btn_open_impressum button {
                background:transparent !important;
                border:none !important;
                box-shadow:none !important;
                color:#8b9bb2 !important;
                font-size:0.75rem !important;
                text-decoration:underline;
                width:100%;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        st.link_button(
            "Datenschutz",
            "https://kaisersoft.github.io/website/voltdesk-landingpage/datenschutz.html",
            use_container_width=True,
            key="btn_open_datenschutz",
        )
        st.link_button(
            "Impressum",
            "https://kaisersoft.github.io/website/voltdesk-landingpage/impressum.html",
            use_container_width=True,
            key="btn_open_impressum",
        )

    watch = build_watchlist()
    maybe_start_new_paper_day(watch)
    meta_focus = find_meta(st.session_state.focus)
    sess = session_status(meta_focus["yf"] if meta_focus else None)
    _maybe_show_momentum_fade_alarm(sess)
    closed_by_auto = auto_close_due_positions(watch)
    if closed_by_auto:
        st.session_state["flatten_flash"] = (
            "Auto-Close: " + ", ".join(closed_by_auto) + " gemäß jeweiligem Markt-Schluss glattgestellt."
        )
        st.session_state.day_report = build_day_report()
    if st.session_state.killed:
        # Kill Switch bleibt hart: alle offenen Positionen werden geschlossen.
        flatten_all(watch, reason="kill")
    mark_positions(watch)
    close_stopped()
    # Zweiter mark_positions()-Aufruf war unnötig: die Kurse aus dem ersten Aufruf sind
    # bereits aktuell (identischer watch-Snapshot), es muss hier nur noch über die nach
    # close_stopped() verbliebenen Positionen aufsummiert werden.
    open_pnl = sum(p.get("pnl", 0.0) for p in st.session_state.positions)
    actual_fees = float(st.session_state.get("fees_paid") or 0)
    fee_unit = float(st.session_state.get("fee_per_trade", FEE_PER_TRADE) if effective_pro_mode() else FEE_PER_TRADE)
    projected_fees = fee_unit * len(st.session_state.positions)
    gross_pnl = float(st.session_state.closed_pnl) + open_pnl
    fees = actual_fees
    day_pnl = gross_pnl - actual_fees
    day_pct = _safe_pct(day_pnl, current_capital())
    if day_pct <= -MAX_DAILY_LOSS_PCT and not st.session_state.get("risk_lock") and not st.session_state.killed:
        st.session_state.risk_lock = True
        flatten_all(watch, reason="daily-loss-limit")
        save_app_state()
        open_pnl = 0.0
        actual_fees = float(st.session_state.get("fees_paid") or 0)
        projected_fees = 0.0
        gross_pnl = float(st.session_state.closed_pnl)
        fees = actual_fees
        day_pnl = gross_pnl - actual_fees
        day_pct = _safe_pct(day_pnl, current_capital())
        st.session_state["flatten_flash"] = (
            f"Daily Loss Limit: Netto-Tagesverlust -{MAX_DAILY_LOSS_PCT:.2f}% — "
            f"Risk Lock aktiv, alle Positionen glattgestellt."
        )
    check_pnl_alerts(day_pnl, current_equity(), day_pct)
    invested = invested_amount()
    cash = cash_available()
    if cash < 0:
        st.warning(
            f"⚠️ Cash-Bestand ist negativ ({cash:,.2f} €) — vermutlich durch Gebühren "
            "über dem verbleibenden Kapital. Positionen prüfen bzw. Kill Switch erwägen."
        )
    record_equity_snapshot()
    open_risk = sum(position_stop_risk_eur(p) for p in st.session_state.positions)
    rest = max(0.0, MAX_DAILY_LOSS_PCT + day_pct) if day_pct < 0 else MAX_DAILY_LOSS_PCT

    focus = st.session_state.focus


    # Sprungnavigation + echte Live-Uhr.
    # Die Uhr läuft ausschließlich im Browser (JavaScript setInterval) und löst
    # KEINEN Streamlit-Rerun aus. Damit aktualisiert sich nur die Uhr innerhalb
    # dieses kleinen HTML-Components, während der Rest der App unverändert bleibt.
    _nav_left = [
        ("#nav-watchlist", "Watchlist"),
        ("#nav-chart", "Chart"),
        ("#nav-news", "News"),
    ]
    _nav_right = [
        ("#nav-empfehlungen", "Empfehlungen"),
        ("#nav-katalog", "Katalog"),
        ("#nav-bestand", "Bestand"),
        ("#nav-bericht", "Bericht"),
    ]

    def _nav_anchor(href, label, with_right_border=False):
        # target="_parent" + href="#..." verlässt sich auf die relative URL-Auflösung
        # des Browsers gegen die Parent-Seite - das schlägt in vielen Setups fehl
        # (Streamlit-Rerun-Query-Params, Cross-Origin-Edge-Fälle, Browser-Eigenheiten).
        # Stattdessen: direkter JS-Scroll im Parent-Dokument (gleiches Origin,
        # da components.html standardmäßig same-origin eingebettet wird).
        anchor_id = href.lstrip("#")
        border = "border-right:1px solid rgba(255,255,255,0.08);" if with_right_border else ""
        return (
            f'<a href="{href}" onclick="vdJump(event, \'{anchor_id}\')" '
            f'style="flex:1;text-align:center;text-decoration:none;display:flex;'
            f'align-items:center;justify-content:center;padding:0.5rem 0.35rem;'
            f'color:#e2e8f0;font-size:0.8rem;white-space:nowrap;{border}">'
            f'{label}</a>'
        )

    _left_links = "".join(_nav_anchor(h, l, i < len(_nav_left) - 1) for i, (h, l) in enumerate(_nav_left))
    _right_links = "".join(_nav_anchor(h, l, i < len(_nav_right) - 1) for i, (h, l) in enumerate(_nav_right))

    components.html(
        f"""
        <style>
          html, body {{ margin:0; padding:0; background:transparent; overflow:hidden; }}
          .vd-nav {{
            box-sizing:border-box; width:100%; height:48px;
            display:grid; grid-template-columns:1fr 150px 1.33fr;
            background:#111827;
            border:1px solid rgba(0,229,255,0.25);
            border-radius:0.5rem;
            overflow:hidden;
            font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
          }}
          .vd-links {{ display:flex; min-width:0; }}
          .vd-clock {{
            display:flex; align-items:center; justify-content:center;
            border-left:1px solid rgba(255,255,255,0.08);
            border-right:1px solid rgba(255,255,255,0.08);
            color:#00e5ff;
            text-shadow:0 0 8px rgba(0,229,255,0.35);
            font-family:"SFMono-Regular",Consolas,"Liberation Mono",monospace;
            font-size:1rem; font-weight:600; letter-spacing:.08em;
            font-variant-numeric:tabular-nums;
            white-space:nowrap;
          }}
          .vd-clock span {{ min-width:92px; text-align:center; }}
          @media (max-width:900px) {{
            .vd-nav {{ grid-template-columns:1fr 120px 1fr; }}
            .vd-links a {{ font-size:.7rem !important; padding-left:.15rem !important; padding-right:.15rem !important; }}
            .vd-clock {{ font-size:.85rem; }}
          }}
        </style>
        <div class="vd-nav">
          <div class="vd-links">{_left_links}</div>
          <div class="vd-clock" aria-label="Live-Uhr Berlin"><span id="vd-clock">--:--:--</span></div>
          <div class="vd-links">{_right_links}</div>
        </div>
        <script>
          (function() {{
            const el = document.getElementById('vd-clock');
            if (!el) return;
            const fmt = new Intl.DateTimeFormat('de-DE', {{
              timeZone: 'Europe/Berlin',
              hour: '2-digit', minute: '2-digit', second: '2-digit',
              hour12: false
            }});
            function updateClock() {{ el.textContent = fmt.format(new Date()); }}
            updateClock();
            setInterval(updateClock, 1000);
          }})();

          // Sprungnavigation: da diese Navbar in einer eigenen iframe (components.html)
          // liegt, funktioniert ein normaler Anker-Klick (target="_parent" + "#id") nicht
          // zuverlässig. Stattdessen greifen wir direkt auf das Parent-Dokument zu
          // (funktioniert, solange die iframe same-origin eingebettet ist, was bei
          // Streamlit-Components standardmäßig der Fall ist) und scrollen manuell.
          window.vdJump = function(evt, id) {{
            evt.preventDefault();
            try {{
              const doc = window.parent.document;
              const target = doc.getElementById(id);
              if (target) {{
                target.scrollIntoView({{behavior: 'smooth', block: 'start'}});
              }} else {{
                // Fallback: Standard-Hash-Navigation, falls Element (noch) nicht existiert
                window.parent.location.hash = id;
              }}
            }} catch (e) {{
              // Cross-Origin-Fall o.ä. - letzter Fallback auf Standardverhalten
              window.location.hash = id;
            }}
          }};
        </script>
        """,
        height=48,
        scrolling=False,
    )

    st.caption(
        f"Fokus: {focus or '—'} · {sess['now'].strftime('%H:%M')} CET · {sess['venue']}"
    )
    if st.session_state.get("flatten_flash"):
        st.warning(st.session_state.pop("flatten_flash"))
    if st.session_state.get("watchlist_add_flash"):
        _wl_flash = st.session_state.pop("watchlist_add_flash")
        if "Limit erreicht" in str(_wl_flash):
            st.warning(_wl_flash)
        else:
            st.info(_wl_flash)
    if st.session_state.get("trade_abort_flash"):
        st.warning(st.session_state.pop("trade_abort_flash"))
    if st.session_state.get("trade_ok_flash"):
        st.success(st.session_state.pop("trade_ok_flash"))
    if st.session_state.get("pending_trade"):
        _trade_confirmation_dialog(st.session_state.pending_trade)
    book = sess["playbook"]
    PHASE_EMOJI = {"PRE": "🌅", "OPEN": "🚀", "MID": "📊", "CLOSE": "🔔"}
    # Buttons zum manuellen Phasenwechsel funktionierten nicht zuverlässig (Klick löste
    # keinen zuverlässigen Rerun/State-Wechsel aus) - zurück auf reine Anzeige der vier
    # Phasen, aktive Phase optisch hervorgehoben, ohne Interaktion.
    m1, m2, m3, m4 = st.columns(4)
    for col, key in zip((m1, m2, m3, m4), ("PRE", "OPEN", "MID", "CLOSE")):
        label = f"{PHASE_EMOJI[key]} {MODE_PLAYBOOK[key]['title']}"
        is_active = sess["mode"] == key
        with col:
            if is_active:
                st.markdown(
                    f"<div style='text-align:center;padding:0.5rem;border-radius:6px;"
                    f"background-color:#1f6feb;color:white;font-weight:bold;'>{label}</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"<div style='text-align:center;padding:0.5rem;border-radius:6px;"
                    f"background-color:rgba(255,255,255,0.06);color:#8b9bb2;'>{label}</div>",
                    unsafe_allow_html=True,
                )
    st.info(f"**{PHASE_EMOJI[sess['mode']]} {book['title']} · {sess['venue']}** — {book['text']}")
    render_daily_setup_gate(sess)
    render_daily_setup_status()
    if sess["mode"] == "CLOSE" and sess.get("close_subphase"):
        sub = sess["close_subphase"]
        if sub == "prep":
            st.warning(
                "🟡 **Close Preparation** — keine neuen Trades, Trailing verschärft "
                "(Gewinne werden sofort auf Breakeven gesichert). "
                f"Final Close in {int((sess['final_at'] - sess['now']).total_seconds() // 60)} Min."
            )
        else:
            st.error("🔴 **Final Close** — offene Hebel werden glattgestellt, Tag wird abgeschlossen.")
    if sess["mode"] == "OPEN" and sess.get("mid_structure") is not None and not sess["mid_structure"]["ready"]:
        ms = sess["mid_structure"]
        missing = []
        if not ms["vwap_ok"]:
            missing.append("VWAP noch nicht etabliert")
        if ms["momentum"] is None:
            missing.append("kein klares Momentum")
        if not ms["volume_ok"]:
            missing.append("Volumen noch erhöht")
        st.caption(
            f"⏳ Opening Range zeitlich vorbei, aber Struktur noch nicht bestätigt "
            f"({', '.join(missing)}) — Wechsel nach MID verzögert sich."
        )
    if sess.get("market_closed_reason"):
        st.warning(
            f"🚫 Markt zu: {sess['market_closed_reason']}. "
            f"Auto-Modus = CLOSE · Override möglich zum Testen."
        )
    if st.session_state.mode_override != "Auto":
        st.caption(f"Override aktiv (Auto wäre {sess['auto_mode']}).")
    elif sess["mode"] == "PRE":
        open_from = session_windows(sess["venue"])["open"][0]
        st.caption(
            f"Vorbörse bis {open_from.strftime('%H:%M')} CET · "
            f"Watchlist zeigt den letzten gültigen Schlusskurs (kein Live-Premarket)."
        )
    elif sess["mode"] != "CLOSE":
        cash_basis = max(float(cash + invested), 0.0)
        in_production_pct = (float(invested) / cash_basis * 100.0) if cash_basis > 0 else 0.0
        fees_pct = (float(fees) / cash_basis * 100.0) if cash_basis > 0 else 0.0
        st.caption(
            f"Auto-Close {sess['flatten_at'].strftime('%H:%M')} CET · "
            f"Schluss {sess['close_at'].strftime('%H:%M')} · noch {max(sess['minutes_left'], 0):.0f} Min · "
            f"In Produkten {in_production_pct:.2f}% · Gebühren {fees_pct:.2f}%"
        )

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Paper-Cash", f"{cash:,.0f} €")
    k2.metric("In Produkten", f"{invested:,.0f} €")
    k3.metric("Netto P&L", f"{day_pnl:+.2f} €", f"{day_pct:+.2f}%")
    k4.metric("Gebühren (ist)", f"{fees:.2f} €")
    k5.metric("Restbudget", f"{rest:.2f}%")
    k6.metric("Daily-Loss-Lock", f"−{MAX_DAILY_LOSS_PCT:.2f}%")
    # Transparenz für die neuen Overtrading-/Risiko-Guardrails - sonst wirkt eine
    # Ticket-Ablehnung überraschend, weil der aktuelle Stand nirgends sichtbar ist.
    if not effective_pro_mode():
        trades_today = int(st.session_state.get("trades_opened_today", 0))
        max_trades_disp = st.session_state.get("max_trades_per_day", 5)
        risk_used = float(st.session_state.get("risk_used_today_eur", 0.0))
        risk_pct_disp = float(st.session_state.get("daily_risk_pct", 2.0))
        risk_cap = current_capital() * risk_pct_disp / 100.0
        cooldown_txt = ""
        last_loss_iso = st.session_state.get("last_loss_close_at")
        if last_loss_iso:
            remaining = timedelta(minutes=10) - (datetime.now(TZ_BERLIN) - datetime.fromisoformat(last_loss_iso))
            if remaining.total_seconds() > 0:
                cooldown_txt = f" · Cooldown noch {int(remaining.total_seconds() // 60) + 1} Min."
        st.caption(
            f"Guardrails: {trades_today}/{max_trades_disp} Trades heute · "
            f"Risk allocated today {risk_used:.0f}/{risk_cap:.0f} € · "
            f"Offenes Risiko {open_risk:.0f} €"
            f"{cooldown_txt} · (Pro Modus hebt alle drei auf)"
        )

    tilt_now = detect_tilt(st.session_state.get("fills") or [])
    apply_tilt_lock_if_needed(tilt_now)
    if st.session_state.get("tilt_enabled", True) and (
        tilt_now.get("tilt_detected") or st.session_state.get("tilt_lock")
    ):
        reasons_txt = " · ".join(tilt_now.get("reasons") or []) or st.session_state.get("tilt_lock_reason") or ""
        score = tilt_now.get("tilt_score") or st.session_state.get("tilt_lock_score") or 0
        # Volle Breite für die Meldung, außer wenn wirklich ein Lock-Button nötig
        # ist (2026-09) - der separate "Pause"-Button hier wurde entfernt, das ist
        # exakt derselbe trading_pause-Toggle wie der Button in der Sidebar
        # (trading_pause_btn) und damit redundant.
        is_locked = bool(st.session_state.get("tilt_lock") or tilt_now.get("suggested_action") == "lock")
        if is_locked:
            tcol1, tcol2 = st.columns([4, 1])
        else:
            tcol1, tcol2 = st.container(), None
        with tcol1:
            if is_locked:
                st.error(f"🧠 Tilt-Lock · Score {score} — keine neuen Käufe. {reasons_txt}")
            elif tilt_now.get("suggested_action") == "pause":
                st.warning(f"🧠 Tilt-Score {score} — Trading-Pause empfohlen. {reasons_txt}")
            else:
                st.info(f"🧠 Tilt-Score {score} — {reasons_txt}")
        if tcol2 is not None:
            with tcol2:
                if st.session_state.get("tilt_lock"):
                    if st.button("Lock lösen", key="dash_clear_tilt_lock", use_container_width=True):
                        st.session_state._show_tilt_unlock_form = True
        if st.session_state.get("tilt_lock") and st.session_state.get("_show_tilt_unlock_form"):
            unlock_note = st.text_input(
                "Grund fürs Aufheben (Pflicht) *", key="dash_tilt_unlock_note",
            )
            uc1, uc2 = st.columns(2)
            with uc1:
                if st.button(
                    "Bestätigen & aufheben", key="dash_confirm_tilt_unlock",
                    disabled=not unlock_note.strip(), use_container_width=True,
                ):
                    _log_tilt_unlock(unlock_note)
                    st.session_state._show_tilt_unlock_form = False
                    st.rerun()
            with uc2:
                if st.button("Abbrechen", key="dash_cancel_tilt_unlock", use_container_width=True):
                    st.session_state._show_tilt_unlock_form = False
                    st.rerun()

    cash_pct = cash / current_capital() * 100 if current_capital() else 0
    cash_block = cash_pct < 20
    if cash_pct < 20:
        st.error(
            f"Cash nur {cash_pct:.0f} % — zweite Warnung. "
            "Mit so wenig Cash ist kein wirksames Risikomanagement mehr möglich. Neue Käufe gesperrt."
        )
    elif cash_pct < 40:
        st.warning(
            f"Cash nur {cash_pct:.0f} % — erste Warnung. "
            "Weitere Käufe lassen kaum Puffer für Stops und Nachsteuerungen."
        )

    # --- PRE Stock Picker (News → mögliche Bewegung) ---
    render_pre_stock_picker(sess)
    # --- OPEN Watchlist Ranker (Tradbarkeit · Range · Setup · Kontext) ---
    render_open_watchlist_ranker(sess)
    # --- MID Positions-Cockpit (Ampel) ---
    mid_advice_rows = []
    if sess.get("mode") == "MID" and st.session_state.get("positions"):
        with st.spinner("Positionen für MID bewerten…"):
            mid_advice_rows = evaluate_positions_advice(sess)
    render_mid_position_cockpit(sess, mid_advice_rows)

    # --- WATCHLIST (full width) ---
    _render_watchlist_section(watch)


    # --- INTRADAY CHART (full width) ---
    meta = find_meta(st.session_state.focus)
    st.markdown('<div id="nav-chart"></div>', unsafe_allow_html=True)
    st.subheader(f"Intraday · {st.session_state.focus or '—'}")
    levels = fetch_levels(meta["yf"]) if meta else {}
    # Gap zum Vortag: Differenz zwischen heutigem Open und Vortages-Schlusskurs - der
    # eigentliche "Gap", nicht die reine Tagesveränderung (die vom letzten Kurs ausgeht).
    gap_pct = None
    gap_abs = None
    if levels.get("open") and levels.get("prev_close"):
        gap_abs = levels["open"] - levels["prev_close"]
        gap_pct = gap_abs / levels["prev_close"] * 100
    if meta and levels:
        st.markdown(
            """
            <style>
            div[data-testid="stVerticalBlockBorderWrapper"]:has(> div > div[data-testid="stVerticalBlock"] > div.st-key-chart_metrics_row)
                [data-testid="stMetricValue"] { font-size: 0.8em; }
            .st-key-chart_metrics_row [data-testid="stMetricValue"] { font-size: 0.8em; }
            </style>
            """,
            unsafe_allow_html=True,
        )
        _chart_metrics_box = st.container(key="chart_metrics_row")
        with _chart_metrics_box:
            l1, l2, l3, l4, l5, l6 = st.columns(6)
            l1.metric("Vortag H", f"{levels['prev_high']:.2f}" if levels.get("prev_high") else "—")
            l2.metric("Vortag L", f"{levels['prev_low']:.2f}" if levels.get("prev_low") else "—")
            l3.metric("Open", f"{levels['open']:.2f}" if levels.get("open") else "—")
            l4.metric("VWAP", f"{levels['vwap']:.2f}" if levels.get("vwap") else "—")
            l5.metric("ATR(14)", f"{levels['atr']:.2f}" if levels.get("atr") else "—")
            l6.metric(
                "Gap z. Vortag",
                f"{gap_pct:+.2f}%" if gap_pct is not None else "—",
                f"{gap_abs:+.2f}" if gap_abs is not None else None,
            )
            sx = compute_sector_relative(st.session_state.focus, levels.get("close") or levels.get("open"), levels)
            if sx.get("rel_pct") is not None:
                st.caption(
                    f"vs. Sektor ({sx.get('sector')}) {sx['rel_pct']:+.2f}% · {sx.get('status')} "
                    f"· Peers {sx.get('peers')} · Sektor {sx.get('sector_chg'):+.2f}%"
                )
            else:
                st.caption("vs. Sektor — · zu wenige Peers für Relativstärke")

            # --- Previous-Day-Context + Volumen-/Momentumanalyse (kompakt, kein zweiter
            # Hauptchart) ---
            focus_q = fetch_focus_quote(meta["yf"])
            focus_price = focus_q.get("price")
            check_price_alerts(meta["ticker"], focus_price, levels)
            focus_intra = fetch_intraday(meta["yf"])
            vol_mom_focus = compute_volume_momentum(meta["ticker"], focus_intra, levels, focus_price)

            pd1, pd2, pd3, pd4, pd5, pd6 = st.columns(6)
            pd1.metric("Vortag Close", f"{levels['prev_close']:.2f}" if levels.get("prev_close") else "—")
            mcap = levels.get("market_cap")
            pd2.metric(
                "Marktkapitalisierung",
                f"{mcap / 1e9:.1f} Mrd." if mcap else "—",
            )
            vdelta = levels.get("volume_delta_pct")
            pd3.metric(
                "Volumen heute vs. Vortag",
                f"{levels['today_volume']:,.0f}" if levels.get("today_volume") else "—",
                f"{vdelta:+.1f}%" if vdelta is not None else None,
            )
            sv_pct = vol_mom_focus["stock_volume_pct"]
            sv_ind = vol_mom_focus["stock_volume_indicator"]
            pd4.metric(
                "Titel-Volumen vs. 20D",
                f"{sv_ind['emoji']} {sv_pct:.0f}%" if sv_pct is not None else "—",
                sv_ind["label"] if sv_pct is not None else None,
            )
            mv_pct = vol_mom_focus["market_volume_pct"]
            mv_ind = vol_mom_focus["market_volume_indicator"]
            pd5.metric(
                "Benchmark-Volumen vs. 20D",
                f"{mv_ind['emoji']} {mv_pct:.0f}%" if mv_pct is not None else "—",
                mv_ind["label"] if mv_pct is not None else None,
            )
            direction = vol_mom_focus["price_direction"]
            pd6.metric(
                "Price Direction",
                f"{direction['emoji']} {direction['label']}",
            )
            mom_emoji = {"strong": "💪", "neutral": "➖", "weak": "😴"}.get(vol_mom_focus["momentum"], "➖")
            conf_emoji = {"high": "✅", "neutral": "➖", "low": "❔"}.get(vol_mom_focus["confirmation"], "➖")
            st.caption(
                f"Momentum: {mom_emoji} {vol_mom_focus['momentum']} · "
                f"Confirmation: {conf_emoji} {vol_mom_focus['confirmation']} · "
                f"Relative Volume: {vol_mom_focus['relative_volume']:.2f}x"
                if vol_mom_focus["relative_volume"] is not None
                else f"Momentum: {mom_emoji} {vol_mom_focus['momentum']} · Confirmation: {conf_emoji} {vol_mom_focus['confirmation']}"
            )
    if not meta:
        st.warning("Kein Fokus gewählt.")
    else:
        intra = fetch_intraday(meta["yf"])
        if intra.empty:
            st.warning("Kein Intraday-Chart verfügbar (Markt zu oder Feed leer).")
        else:
            _maybe_show_three_red_candles_alarm(meta["ticker"], intra, sess)
            # Wenn der letzte verfügbare Balken nicht von heute ist, zeigt der Chart den
            # letzten Handelstag (Markt aktuell zu / Wochenende / Feiertag / PRE ohne Live-
            # Daten) - das war vorher nicht erkennbar und konnte wie ein "aktueller" Chart
            # missverstanden werden.
            last_bar_date = intra.index[-1].date()
            today_date = datetime.now(TZ_BERLIN).date()
            is_prev_day = last_bar_date != today_date
            chart_title = (
                f"Vortageschart ({last_bar_date.strftime('%d.%m.%Y')}) – Markt aktuell geschlossen"
                if is_prev_day else None
            )
            if HAS_PLOTLY:
                # Fills für diesen Ticker vorfiltern - leichtgewichtig (kein Cache-
                # Hashing nötig, s. Underscore-Parameter in build_focus_chart_figure),
                # wird für Fingerprint UND zum Zeichnen der Kauf-/Verkaufs-Linien
                # gebraucht.
                ticker_fills = tuple(
                    (f.get("time"), f.get("side"))
                    for f in st.session_state.get("fills", [])
                    if f.get("ticker") == meta["ticker"] and f.get("time")
                )
                show_volume = bool(st.session_state.get("chart_show_volume", True))
                sector_name = get_sector(meta["ticker"])
                sector_symbol = SECTOR_BENCH.get(sector_name)
                # Sektor-Linie ist bewusst standardmäßig AUS (s. Diskussion: Info-
                # Overload vermeiden) - nur wenn der Nutzer sie einschaltet, wird
                # überhaupt die zusätzliche Sektor-ETF-Historie geladen.
                show_sector_line = bool(st.session_state.get("chart_show_sector", False)) and bool(sector_symbol)
                sector_series = sector_overlay_series(intra, sector_symbol) if show_sector_line else None
                focus_pos = next(
                    (p for p in (st.session_state.get("positions") or [])
                     if p.get("ticker") == meta["ticker"]),
                    None,
                )
                pos_fp = None
                if focus_pos:
                    pos_fp = (
                        round(float(focus_pos["entry"]), 4) if focus_pos.get("entry") is not None else None,
                        round(float(focus_pos["stop"]), 4) if focus_pos.get("stop") is not None else None,
                        round(float(focus_pos["take"]), 4) if focus_pos.get("take") is not None else None,
                    )
                venue_name = sess.get("venue") or "US Regular"
                fingerprint = (
                    len(intra),
                    str(intra.index[-1]) if not intra.empty else None,
                    round(float(intra["Close"].iloc[-1]), 2) if not intra.empty else None,
                    round(float(levels["vwap"]), 2) if levels.get("vwap") else None,
                    ticker_fills,
                    bool(show_volume),
                    venue_name,
                    pos_fp,
                    bool(show_sector_line),
                    round(float(sector_series.iloc[-1]), 2) if sector_series is not None and not sector_series.dropna().empty else None,
                )
                fig = build_focus_chart_figure(
                    meta["ticker"], fingerprint, intra, levels, chart_title, ticker_fills,
                    _show_volume=bool(show_volume),
                    _venue=venue_name,
                    _position=focus_pos,
                    _sector_series=sector_series,
                    _sector_label=sector_symbol,
                )
                if chart_title:
                    st.warning(chart_title)
                st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CHART_CONFIG)

                # Sektor-Bestätigungs-Badge ("Option C", 2026-09): immer sichtbar bei
                # erkanntem Ausbruch, unabhängig vom Sektor-Linie-Toggle - Hinweis,
                # kein Signal (s. sector_confirmation), und ein Trigger, die Linie
                # unten für den Verlauf einzuschalten.
                breakout_side = _current_breakout_side(intra, levels)
                if breakout_side:
                    sec_conf = sector_confirmation(meta["ticker"], breakout_side)
                    if sec_conf["available"]:
                        chg = sec_conf["sector_chg"]
                        if sec_conf["confirmed"]:
                            st.success(
                                f"✅ Sektor ({sec_conf['sector']}) bestätigt den {breakout_side}-Ausbruch ({chg:+.2f}%)"
                            )
                        else:
                            st.warning(
                                f"⚠️ Sektor ({sec_conf['sector']}) bestätigt den {breakout_side}-Ausbruch NICHT "
                                f"({chg:+.2f}%) — möglicher Fake Breakout, muss aber keiner sein. "
                                "Sektor-Linie unten einschalten für den Verlauf."
                            )
                    else:
                        st.caption(
                            f"ℹ️ {breakout_side}-Ausbruch erkannt, aber kein Sektor-Benchmark für "
                            f"„{sec_conf['sector']}“ verfügbar."
                        )

                tcol_a, tcol_b = st.columns(2)
                with tcol_a:
                    st.toggle(
                        "Volumen-Histogramm",
                        value=show_volume,
                        key="chart_show_volume",
                        help="5-Min-Volumen unter dem Kerzenchart. Grün = Kerze auf, rot = Kerze ab.",
                    )
                with tcol_b:
                    st.toggle(
                        "Sektor-Linie",
                        value=show_sector_line,
                        key="chart_show_sector",
                        disabled=not bool(sector_symbol),
                        help=(
                            f"% Veränderung von {sector_symbol} ({sector_name}) seit Chart-Start, "
                            "eigene Achse rechts."
                        ) if sector_symbol else f"Kein Sektor-Benchmark für „{sector_name}“ verfügbar.",
                    )
                last_close = float(intra["Close"].iloc[-1]) if not intra.empty else None
                bands_now = _intraday_vwap_bands(intra)
                if not bands_now.empty and last_close is not None and pd.notna(bands_now["sigma"].iloc[-1]):
                    sig = float(bands_now["sigma"].iloc[-1])
                    vw = float(bands_now["vwap"].iloc[-1]) if pd.notna(bands_now["vwap"].iloc[-1]) else None
                    if vw and sig and sig > 0:
                        z = (last_close - vw) / sig
                        if abs(z) >= 2:
                            zone = "Überdehnung (≥2σ)"
                        elif abs(z) >= 1:
                            zone = "Momentum-Band (1–2σ)"
                        else:
                            zone = "Fair-Value-Zone (±1σ)"
                        st.caption(f"VWAP-Lage: {z:+.2f}σ · {zone}")
                if focus_pos:
                    crv = _position_crv(focus_pos)
                    try:
                        risk = abs(float(focus_pos.get("entry")) - float(focus_pos.get("stop")))
                    except (TypeError, ValueError):
                        risk = None
                    crv_txt = f"{crv:.2f}R" if crv is not None else "—"
                    risk_txt = f"{risk:.2f}" if risk else "—"
                    st.caption(
                        f"Position {focus_pos.get('side') or ''} · "
                        f"Entry {focus_pos.get('entry')} · Stop {focus_pos.get('stop')} · "
                        f"Take {focus_pos.get('take') or '—'} · CRV {crv_txt} · Distanz Stop {risk_txt}"
                    )
            else:
                if chart_title:
                    st.caption(f"⚠️ {chart_title}")
                st.line_chart(intra["Close"], height=320)

        # --- PATTERNS under chart ---
        patterns = detect_intraday_patterns(intra, levels, sess["mode"], sess["venue"])
        st.markdown("**Muster am Fokus-Chart**")
        if not patterns:
            st.caption("Kein klares 5-Min-Muster an der letzten Kerze.")
        else:
            for p in patterns:
                if p["bias"] == "long":
                    st.success(f"{p['name']} · {p['note']}")
                elif p["bias"] == "short":
                    st.error(f"{p['name']} · {p['note']}")
                else:
                    st.info(f"{p['name']} · {p['note']}")
            st.caption("Regelbasierte Erkennung, kein Handelssignal. Immer mit Level, Volumen und News lesen.")

    # --- RECOMMENDATION ---
    rec = None
    rec_levels = {}
    if meta_focus:
        rec_levels = fetch_levels(meta_focus["yf"])
        rec_intra = fetch_intraday(meta_focus["yf"])
        rec_news = load_focus_news(meta_focus)
        rec_events = fetch_events(meta_focus["yf"], meta_focus["ticker"])
        rec_q = fetch_focus_quote(meta_focus["yf"])
        rec_px = rec_q.get("price")
        # --- CACHED RECOMMENDATION LOGIC ---
        now_ts = datetime.now(TZ_BERLIN).timestamp()
        cache = st.session_state.rec_cache.get(focus)
        last_rec = st.session_state.last_rec_time.get(focus, 0)

        # Check if we need a fresh recommendation
        need_fresh = True
        if cache:
            age = now_ts - cache.get("ts", 0)
            old_px = cache.get("px", 0)
            px_change = abs((rec_px - old_px) / old_px * 100) if old_px and rec_px else 0

            if age < REC_CACHE_SEC and px_change < 0.5:
                # Cache still valid: same price (< 0.5% change) and within TTL
                need_fresh = False
            elif age < REC_CACHE_SEC and px_change >= 0.5:
                # Price moved significantly, force refresh despite TTL
                need_fresh = True
            elif now_ts - last_rec < REC_MIN_GAP_MIN * 60:
                # Within min gap: use cached rec if available, else skip
                need_fresh = False

        if need_fresh:
            rec = recommend_product(
                rec_intra, rec_levels, rec_news, rec_events, sess["mode"], rec_px,
                venue=sess["venue"], ticker=focus,
            )
            st.session_state.rec_cache[focus] = {"rec": rec, "ts": now_ts, "px": rec_px}
            if rec and rec.get("action") == "BUY":
                st.session_state.last_rec_time[focus] = now_ts
        else:
            rec = cache.get("rec") if cache else None

        check_earnings_alerts(focus, rec_events or {})
        _maybe_show_earnings_alert_dialog(focus, rec_events or {})
        check_setup_alerts(focus, rec or {}, venue=sess.get("venue") or "US Regular")

        # Track recommendation history — WAIT bewusst auslassen (Grundrauschen)
        if rec and rec.get("action") and str(rec.get("action")).upper() != "WAIT":
            hist_entry = {
                "time": datetime.now(TZ_BERLIN).strftime("%H:%M:%S"),
                "ticker": focus,
                "action": rec["action"],
                "side": rec.get("side", "—"),
                "confidence": rec.get("confidence", 0),
                "reasons": " · ".join(rec.get("reasons", [])[-3:]) if rec.get("reasons") else "—",
            }
            # Avoid duplicates within same minute
            existing = [
                h
                for h in st.session_state.rec_history
                if h["ticker"] == focus and h["time"][:5] == hist_entry["time"][:5]
            ]
            if not existing:
                st.session_state.rec_history.insert(0, hist_entry)
                if len(st.session_state.rec_history) > 50:
                    st.session_state.rec_history = st.session_state.rec_history[:50]
        # Alte WAIT-Einträge aus der Historie entfernen
        if st.session_state.get("rec_history"):
            st.session_state.rec_history = [
                h
                for h in st.session_state.rec_history
                if str(h.get("action") or "").upper() != "WAIT"
            ]
        b1, b2 = st.columns([1, 3])
        b1.markdown(f"**{rec_q.get('feed', 'DELAYED')}**")
        b2.caption(f"Fokus-Kurs {rec_px if rec_px else '—'} · {rec_q.get('stamp') or '—'} · Yahoo-Delay typisch ~15 Min.")

        st.markdown('<div id="nav-empfehlungen"></div>', unsafe_allow_html=True)
        st.subheader(f"Empfehlung · {focus}")
        if rec["action"] != "BUY":
            st.warning(" · ".join(rec["reasons"][-3:]) if rec["reasons"] else "Keine Empfehlung.")
            with st.expander("🔍 Diagnose: Warum keine Empfehlung?"):
                scores = rec.get("scores") or {}
                st.write(f"**Score Long:** {scores.get('long', 0)}  ·  **Score Short:** {scores.get('short', 0)}")
                st.write(f"**Aktueller Modus:** {sess['mode']} ({sess['playbook']['title']})")
                st.write(f"**Fokus-Kurs:** {rec_px or '—'}")
                if rec_levels:
                    st.write(f"**Levels:** VWAP {rec_levels.get('vwap', '—')} · ATR {rec_levels.get('atr', '—')}")
                if rec_news:
                    pos_news = sum(1 for n in rec_news if (n.get("bias") == "long" or (n.get("score") or 0) > 0))
                    neg_news = sum(1 for n in rec_news if (n.get("bias") == "short" or (n.get("score") or 0) < 0))
                    tagged = sum(1 for n in rec_news if n.get("tags"))
                    st.write(
                        f"**News:** {pos_news} positiv · {neg_news} negativ · "
                        f"{tagged} mit Tag · {len(rec_news)} total"
                    )
                st.caption("Regelbasierte Scoring-Logik. Keine Garantie für Richtigkeit.")
        else:
            prod = rec["product"]
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("Richtung", rec["side"])
            r2.metric("Produkt", str(prod.get("Typ")))
            r3.metric("Hebel", f"{float(prod.get('Hebel', 0)):.1f}x")
            r4.metric("Setup", rec.get("setup_label") or "Empfehlung")
            sec = rec.get("sector") or {}
            sec_txt = (
                f" · vs. Sektor {sec.get('rel_pct'):+.2f}% ({sec.get('status')})"
                if sec.get("rel_pct") is not None else ""
            )
            st.caption(
                f"Konfidenz {rec['confidence']:.0f}% · Setup {rec.get('setup_label') or 'Empfehlung'} · "
                f"Quality {rec.get('setup_quality', '—')}{sec_txt}"
            )
            ko_val = prod.get("KO")
            ko_txt = "—" if ko_val is None or (isinstance(ko_val, float) and pd.isna(ko_val)) else f"{float(ko_val):.2f}"
            st.caption(
                f"{prod.get('Profil')} · KO {ko_txt} · "
                f"Eignung {prod.get('Eignung')} · {prod.get('Hinweis')}"
            )
            with st.expander("Begründung"):
                scores = rec.get("scores") or {}
                st.write(f"Punkte Long {scores.get('long', 0)} / Short {scores.get('short', 0)}")
                for line in rec["reasons"]:
                    st.write(f"- {line}")
            taken = rec_key(focus, rec) in st.session_state.taken_recs
            if taken:
                st.markdown('<div class="rec-done">', unsafe_allow_html=True)
                st.info("Diesen Trade hast du heute schon ausgeführt. Erneuter Kauf ist möglich.")
            atr_val = rec_levels.get("atr") if rec_levels else None
            limit_pct = get_position_limit_pct()
            rec_stop_preview = calc_atr_stop(rec_px, rec["side"], atr_val) if rec_px else None
            rec_lev_preview = float(min((prod.get("Hebel") or 5.0), effective_max_leverage()))
            max_size = (
                calc_position_size(cash, atr_val, rec_px, rec_stop_preview, rec_lev_preview)
                if rec_px else min(cash * limit_pct / 100, cash)
            )
            # Dynamic sizing: higher confidence = higher allocation
            conf = rec.get("confidence", 50) if rec else 50
            conf_factor = 0.5 + (conf / 100) * 0.5  # 50% at 0 conf → 100% at 100 conf
            suggested_rec = min(max_size * conf_factor, cash)
            suggested_rec = float(round(suggested_rec / 50) * 50)
            st.caption(
                f"Max. Einsatz: {max_size:.0f} € ({limit_pct:.1f}% Cash"
                f"{' · vol.-adj.' if atr_val else ''} · R-Sizing über Stop×Hebel) · "
                f"Konfidenz-Faktor: {conf_factor:.0%}"
            )
            rec_col, rec_btn_col = st.columns([1, 2])
            with rec_col:
                rec_amount = st.number_input(
                    "Einsatz (€)",
                    min_value=50.0,
                    max_value=max(float(max_size), 50.0),
                    value=max(float(max_size), 50.0),
                    step=50.0,
                    key="rec_amount",
                )
            can_buy = (
                rec_px
                and sess["playbook"]["buy"]
                and not buy_halt_reason()
                and rec_amount <= cash
                and not cash_block
            )
            with rec_btn_col:
                st.markdown("<div style='height:1.65rem'></div>", unsafe_allow_html=True)
                do_rec_buy = st.button(
                    "Empfehlung kaufen (Paper)", type="primary", disabled=not can_buy, key="rec_buy_btn",
                )
            if do_rec_buy:
                lev = float(min(prod.get("Hebel") or 5.0, effective_max_leverage()))
                stop = calc_atr_stop(rec_px, rec["side"], rec_levels.get("atr"))
                request_trade_confirmation(
                    kind="buy",
                    ticker=focus,
                    side=rec["side"],
                    amount=rec_amount,
                    price=rec_px,
                    stop=stop,
                    leverage=lev,
                    typ=str(prod.get("Typ") or "Open-End Turbo"),
                    wkn=prod.get("WKN"),
                    action="Kauf",
                    reason="Empfehlung",
                    exec_reason="empfehlung",
                    setup_label=rec.get("setup_label") or classify_setup_label(None, rec_levels, rec_px, rec["side"], source="empfehlung"),
                    setup_quality=rec.get("setup_quality"),
                    rec=rec,
                    product=prod,
                    ko=prod.get("KO") or prod.get("ko"),
                    spread_pct=prod.get("Spread %") or prod.get("spread_pct"),
                    update_ticket=True,
                )
            if taken:
                st.markdown("</div>", unsafe_allow_html=True)
        st.caption("Regelbasierte Paper-Empfehlung, kein Rat und keine Order.")

    # --- EMPFEHLUNGSHISTORIE ---
    if st.session_state.rec_history:
        with st.expander(f"📋 Empfehlungshistorie ({len(st.session_state.rec_history)})"):
            hf1, hf2 = st.columns(2)
            with hf1:
                hist_filter = st.selectbox(
                    "Filter",
                    ["Alle", "Nur BUY", "Nur Fokus-Ticker"],
                    index=["Alle", "Nur BUY", "Nur Fokus-Ticker"].index(
                        st.session_state.get("rec_hist_filter", "Alle")
                    )
                    if st.session_state.get("rec_hist_filter", "Alle")
                    in ("Alle", "Nur BUY", "Nur Fokus-Ticker")
                    else 0,
                    key="rec_hist_filter_box",
                )
                st.session_state.rec_hist_filter = hist_filter
            hist_rows = list(st.session_state.rec_history)
            if hist_filter == "Nur BUY":
                hist_rows = [h for h in hist_rows if str(h.get("action") or "").upper() == "BUY"]
            elif hist_filter == "Nur Fokus-Ticker":
                foc = st.session_state.get("focus")
                hist_rows = [h for h in hist_rows if h.get("ticker") == foc]
            hist_df = pd.DataFrame(hist_rows) if hist_rows else pd.DataFrame()
            if hist_df.empty:
                st.caption("Keine Einträge für diesen Filter.")
            else:
                st.dataframe(hist_df, use_container_width=True, hide_index=True)

    # --- NEWS + EVENTS side by side ---
    news_ev_col1, news_ev_col2 = st.columns(2)
    with news_ev_col1:
        meta = find_meta(st.session_state.focus)
        st.subheader(f"Event-Uhr · {st.session_state.focus or '—'}")
        if not meta:
            st.caption("Titel wählen.")
        else:
            # Die Auswahl der Event-Uhr-Quellen liegt zentral im Einstellungsdialog.
            # Hier nur noch die konsolidierten Ergebnisse anzeigen.
            ev = fetch_events(meta["yf"], meta["ticker"])
            st.write(f"**Session:** {ev.get('session') or '—'}")
            st.write(f"**Nächste Earnings:** {ev.get('earnings') or 'nicht gefunden'}")
            if ev.get("earnings_note"):
                st.caption(ev["earnings_note"])
            # Clean Event-Uhr: never render raw Python dicts.
            # Generic timing hints remain separate from actual source data.
            macro_hints = [line for line in (ev.get("macro") or []) if isinstance(line, str)]
            if macro_hints:
                st.markdown("**Makro-Hinweise**")
                for line in macro_hints:
                    st.caption(line)

            macro_events = [m for m in (ev.get("macro") or []) if isinstance(m, dict)]

            # De-duplicate overlapping FRED/BLS observations. Prefer BLS for US
            # labour/inflation figures, while retaining FRED Fed Funds and any
            # source-specific values that have no equivalent.
            preferred = {
                "US CPI": "BLS",
                "US unemployment": "BLS",
                "US payrolls": "BLS",
            }
            selected = []
            seen_keys = set()
            preferred_titles = {
                title for title, source in preferred.items()
                if any(str(x.get("title") or "").strip() == title and str(x.get("source") or "").strip() == source for x in macro_events)
            }
            for item in macro_events:
                title = str(item.get("title") or "").strip()
                source = str(item.get("source") or "").strip()
                if title in preferred_titles and source != preferred[title]:
                    continue
                key = (source, title)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                selected.append(item)

            def _macro_display(item):
                source = str(item.get("source") or "—")
                title = str(item.get("title") or "Makrodaten")
                date = str(item.get("date") or "—")
                value = item.get("value")
                details = str(item.get("details") or "")
                # Keep values compact and meaningful instead of exposing raw API
                # payloads or unhelpful floating-point artefacts.
                if title in {"US unemployment"}:
                    try: value_text = f"{float(value):.1f} %"
                    except Exception: value_text = details
                elif title in {"US Fed funds"}:
                    try: value_text = f"{float(value):.2f} %"
                    except Exception: value_text = details
                elif title in {"US CPI", "Euro area HICP", "Germany HICP"}:
                    try: value_text = f"{float(value):.3f}" if title == "US CPI" else f"{float(value):.1f} %"
                    except Exception: value_text = details
                elif title == "US payrolls":
                    try: value_text = f"{float(value)/1000:.1f} Mio."
                    except Exception: value_text = details
                else:
                    value_text = details or str(value or "—")
                return f"{source} · {title} · {date} · {value_text}"

            if selected:
                st.markdown("**Aktuelle Makrodaten**")
                for item in selected[:8]:
                    st.caption(f"🟡 {_macro_display(item)}")
            else:
                st.caption("Keine aktuellen offiziellen Makrodaten verfügbar.")
            for ins in (ev.get("insider") or [])[:6]:
                if ins.get("type") == "insider_cluster":
                    st.caption(f"🟢🟢 {ins.get('title')} · {ins.get('details')} · +{ins.get('score', 0)}")
                elif ins.get("type") == "insider_buy":
                    st.caption(f"🟢 INSIDER BUY · {ins.get('role')} · {ins.get('details')} · +{ins.get('score', 0)}")
                elif ins.get("type") == "insider_sell":
                    st.caption(f"🔴 INSIDER SELL · {ins.get('role')} · {ins.get('details')} · {ins.get('score', 0)}")
            if ev.get("insider_score") is not None:
                st.caption(f"Insider Score: {ev.get('insider_score'):+d}")
            if FRED_API_KEY:
                st.caption("This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.")


    with news_ev_col2:
        meta = find_meta(st.session_state.focus)
        st.markdown('<div id="nav-news"></div>', unsafe_allow_html=True)
        st.subheader(f"News · {st.session_state.focus or '—'}")
        if not meta:
            st.caption("Titel wählen.")
        else:
            news = load_focus_news(meta)
            active = enabled_news_sources()
            src_counts = Counter(
                (n.get("source") or n.get("source_id") or "?") for n in news
            )
            count_txt = " · ".join(f"{k} {v}" for k, v in src_counts.most_common()) or "—"
            st.caption(
                f"{len(news)} Meldungen · aktiv: "
                + (", ".join(NEWS_SOURCE_DEFS[s]["label"] for s in active if s in NEWS_SOURCE_DEFS) or "keine")
                + f" · geliefert: {count_txt}"
            )
            if not news:
                st.caption("Keine Headlines geladen. Quellen in der Sidebar prüfen.")
            for item in news[:14]:
                tags = item.get("tags") or []
                tag_txt = " ".join(f"`{t}`" for t in tags)
                score = int(item.get("score") or 0)
                src = item.get("source") or item.get("source_id") or ""
                title = item.get("title") or ""
                url = item.get("url") or ""
                title_md = f"[{title}]({url})" if url else title
                if item.get("bias") == "long":
                    tone = "🟢"
                elif item.get("bias") == "short":
                    tone = "🔴"
                else:
                    tone = "⚪"
                extra = ""
                if score:
                    extra = f" · {score:+d}"
                st.markdown(
                    f"{tone} **{item.get('when', '—')}** · {src}{extra}  \n{title_md}"
                    + (f"  \n{tag_txt}" if tag_txt else "")
                )

    # --- PRODUCT CATALOG ---
    st.markdown('<div id="nav-katalog"></div>', unsafe_allow_html=True)
    st.subheader(f"Produktkatalog · {st.session_state.focus or '—'}")
    st.caption(
        "WKN / KO / Spread. Onvista-Stammdaten wo vorhanden, sonst Desk-Katalog. "
        "Hebel und KO-Abstand werden am aktuellen Basiswert neu gerechnet — keine Live-Derivatquotes."
    )
    meta_der = find_meta(st.session_state.focus)
    last_for_der = fetch_quote(meta_der["yf"]).get("price") if meta_der else None
    if not last_for_der:
        st.info("Kein Kurs — Katalog nicht verfügbar.")
    else:
        cat = live_catalog(st.session_state.focus, last_for_der)
        tab_long, tab_short = st.tabs(["📈 Long", "📉 Short"])
        for tab, der_side in ((tab_long, "LONG"), (tab_short, "SHORT")):
            with tab:
                show = cat[cat["Seite"] == der_side].reset_index(drop=True) if not cat.empty else pd.DataFrame()
                if show.empty:
                    st.caption("Keine Produkte in dieser Richtung.")
                    continue
                def _eignung_row_color(row):
                    # Ganze Zeile einfärben (nicht nur die "Eignung"-Zelle), damit
                    # geeignete/ungeeignete Produkte auf einen Blick erkennbar sind.
                    color = "background-color: rgba(34,197,94,0.15)" if row["Eignung"] == "geeignet" else "background-color: rgba(234,179,8,0.15)"
                    return [color] * len(row)

                event = st.dataframe(
                    show.style.apply(_eignung_row_color, axis=1).format(
                        {
                            "Hebel": "{:.2f}",
                            "KO": "{:.2f}",
                            "KO-Abstand %": "{:.1f}",
                            "Spread %": "{:.2f}",
                        },
                        na_rep="—",
                    ),
                    use_container_width=True,
                    hide_index=True,
                    on_select="rerun",
                    selection_mode="single-row",
                    key=f"cat_{der_side}",
                )
                suitable = show[show["Eignung"] == "geeignet"]
                st.caption(f"{len(suitable)} geeignet · Zeile wählen, Betrag angeben, kaufen.")
                rows = get_selected_rows(event)
                # andernfalls bleibt rows = []
                limit_pct = get_position_limit_pct()
                max_cat = min(cash * limit_pct / 100, cash)
                st.caption(f"Max. Einsatz: {max_cat:.0f} € ({limit_pct:.1f}% Cash)")
                amt_col, btn_col = st.columns([1, 2])
                with amt_col:
                    cat_amount = st.number_input(
                        f"Einsatz (€) · {der_side}",
                        min_value=50.0,
                        max_value=max(float(max_cat), 50.0),
                        value=min(500.0, max(float(max_cat), 50.0)),
                        step=50.0,
                        key=f"amt_{der_side}",
                    )
                buy_ok = (
                    bool(rows)
                    and sess["playbook"]["buy"]
                    and not buy_halt_reason()
                    and not cash_block
                    and cat_amount <= cash
                    # Bugfix: hier stand vorher zusätzlich "and not (rec is None or
                    # action == WAIT)" - das hat den Katalog-Kauf bei WAIT HART
                    # gesperrt, für jeden (auch Pro-Modus), ohne Override-Möglichkeit.
                    # Das widersprach dem eigentlichen Entwurf: der Boredom Guard in
                    # execute_paper_buy() soll einen manuellen Kauf ohne aktive
                    # Empfehlung nur an eine eingetragene Tagesnotiz-Begründung
                    # koppeln, ihn nicht verbieten. Die inhaltliche Prüfung passiert
                    # weiterhin dort (reason="katalog") - hier nur noch ein Hinweis,
                    # kein Blocker mehr.
                )
                if rec is None or str(rec.get("action", "WAIT")).upper() == "WAIT":
                    st.caption("ℹ️ Kein aktives Kauf-Signal (WAIT) — Kauf erfordert eine Begründung in der Tagesnotiz.")
                st.caption(
                    "🥱 Boredom Guard: Käufe ohne aktive Empfehlung erfordern einen "
                    "Grund in der Tagesnotiz (weiter unten)."
                )
                with btn_col:
                    st.markdown("<div style='height:1.65rem'></div>", unsafe_allow_html=True)
                    do_buy = st.button(
                        f"{der_side.capitalize()}-Produkt kaufen (Paper)",
                        disabled=not buy_ok,
                        key=f"buy_{der_side}",
                    )
                if do_buy:
                    if not rows or rows[0] >= len(show):
                        st.warning("Auswahl ist nicht mehr gültig, bitte Zeile erneut wählen.")
                        st.stop()
                    row = show.iloc[rows[0]]
                    lev = float(min(row["Hebel"], effective_max_leverage()))
                    # ATR-basierter Stop statt starrer 3% (s. calc_atr_stop) - fällt
                    # automatisch auf 3% zurück, falls kein ATR verfügbar ist.
                    stop = calc_atr_stop(last_for_der, der_side, levels.get("atr"))
                    request_trade_confirmation(
                        kind="buy",
                        ticker=st.session_state.focus,
                        side=der_side,
                        amount=cat_amount,
                        price=last_for_der,
                        stop=stop,
                        leverage=lev,
                        typ=str(row["Typ"]),
                        wkn=str(row["WKN"]),
                        action="Kauf",
                        reason="Produktkatalog",
                        exec_reason="katalog",
                        setup_label=((rec or {}).get("setup_label") if rec else classify_setup_label(None, levels, last_for_der, der_side, source="katalog")),
                        setup_quality=(rec or {}).get("setup_quality") if rec else None,
                        product=row.to_dict(),
                        ko=row.get("KO"),
                        spread_pct=row.get("Spread %"),
                    )

    # --- TAGESNOTIZ ---
    st.subheader("Tagesnotiz")
    today_key = datetime.now(TZ_BERLIN).strftime("%Y-%m-%d")
    notes = dict(st.session_state.get("day_notes") or {})
    note_val = st.text_area(
        f"Notiz · {today_key}",
        value=notes.get(today_key, ""),
        height=80,
        key=f"day_note_{today_key}",
        placeholder="Freier Text zum Handelstag…",
    )
    if st.button("Notiz speichern", key="save_day_note"):
        notes[today_key] = note_val
        st.session_state.day_notes = notes
        save_app_state()
        st.toast("Notiz gespeichert", icon="📝")

    # --- POSITIONS / PORTFOLIO ---
    st.markdown('<div id="bestand-mgmt"></div>', unsafe_allow_html=True)
    st.markdown('<div id="nav-bestand"></div>', unsafe_allow_html=True)
    st.subheader("Bestand / Empfehlungsmanagement")
    st.caption("Open: aufbauen · Mid: halten / nachziehen / reduzieren · Close: verkaufen oder Auto-Close.")
    for _p in st.session_state.get("positions") or []:
        render_r_milestones(_p)
        _warn = first_hour_warning(_p)
        if _warn:
            st.warning(_warn)

    if not st.session_state.positions:
        st.caption("Kein Bestand — Empfehlung oder Katalogzeile kaufen.")
    else:
        # In MID: bereits bewertete Advice nutzen; sonst pro Position berechnen
        advice_by_id = {}
        if sess.get("mode") == "MID" and mid_advice_rows:
            for r in mid_advice_rows:
                advice_by_id[r["pos"].get("id")] = r
        for pos in list(st.session_state.positions):
            pid = pos["id"]
            last_txt = f"{pos.get('last'):.2f}" if pos.get("last") else "—"
            held = float(pos.get("amount") or 0)
            cached = advice_by_id.get(pid)
            if cached:
                advice = cached["advice"]
            else:
                advice = manage_position(
                    pos,
                    rec if pos.get("ticker") == focus else {},
                    rec_levels if pos.get("ticker") == focus else {},
                    sess["mode"],
                    sess.get("close_subphase"),
                )
            sev = _advice_severity(advice.get("action"))
            sev_icon = {"red": "🔴", "yellow": "🟡", "green": "🟢", "gray": "⚪"}.get(sev, "⚪")
            pnl_val = float(pos.get("pnl", 0.0) or 0.0)
            # Deutliche Farbmarkierung nach P&L-Vorzeichen: verhindert, dass ein
            # Trader versehentlich bei einer bereits im Verlust liegenden Position
            # nachkauft ("hier darf ich nicht nachkaufen") - Gewinn-Positionen analog grün.
            if pnl_val < 0:
                pnl_bg, pnl_border = "rgba(239,68,68,0.14)", "rgba(239,68,68,0.55)"
            elif pnl_val > 0:
                pnl_bg, pnl_border = "rgba(34,197,94,0.14)", "rgba(34,197,94,0.55)"
            else:
                pnl_bg, pnl_border = "rgba(255,255,255,0.04)", "rgba(255,255,255,0.15)"
            entry_txt = f"{float(pos.get('entry')):.4f}" if pos.get('entry') is not None else "—"
            take_status = f"{float(pos.get('take')):.4f}" if pos.get('take') is not None else "—"
            st.markdown(
                f"""
                <div style="background:{pnl_bg};border:1px solid {pnl_border};
                            border-radius:8px;padding:0.5rem 0.75rem;margin-bottom:0.3rem;">
                    {sev_icon} <b>{pos['ticker']}</b> {pos.get('wkn') or '—'} · {pos['side']} · 
                    {held:.0f} € · Entry {entry_txt} · Last {last_txt} · Take {take_status} · P&amp;L {pnl_val:+.2f} €
                </div>
                """,
                unsafe_allow_html=True,
            )
            focus_col, _ = st.columns([1.2, 5])
            with focus_col:
                if st.button("🎯 Fokus", key=f"position_focus_{pid}", use_container_width=True, disabled=pos.get("ticker") == focus):
                    st.session_state.focus = pos.get("ticker")
                    save_app_state()
                    st.rerun()
            ko_px = pos.get("ko")
            if pos.get("take"):
                st.caption(f"Take (PDH/OR) {float(pos['take']):.4f}")
            if ko_px:
                ko_status = check_ko_proximity(pos, pos.get("last") or pos.get("entry"))
                dist = ko_status.get("distance_pct")
                dist_txt = f" · Abstand {dist:.2f}%" if dist is not None else ""
                st.caption(f"KO {float(ko_px):.4f}{dist_txt}")
                if ko_status["level"] == "knocked":
                    st.error("❌ AUSGEKNOCKT! KO-Schwelle wurde erreicht.")
                elif ko_status["level"] == "critical":
                    st.error(f"🔴 KO-Nähe: Nur noch {dist:.1f}% zur KO-Schwelle!")
                elif ko_status["level"] == "warning":
                    st.warning(f"🟡 KO-Nähe: {dist:.1f}% zur KO-Schwelle")
                elif ko_status["level"] == "notice":
                    st.info(f"ℹ️ KO-Abstand: {dist:.1f}%")
            if advice["action"] == "EXIT":
                st.error(advice["label"])
            elif advice["action"] in {"CLOSE", "REDUCE"}:
                st.warning(advice["label"])
            else:
                st.success(advice["label"])

            a, b, c, d = st.columns([1.2, 1.1, 0.9, 1.1])
            with a:
                sell_amt = st.number_input(
                    "Verkauf (€)",
                    min_value=0.0,
                    max_value=max(held, 0.0),
                    value=held,
                    step=50.0,
                    key=f"sellamt_{pid}",
                )
            with b:
                # Bestands-Take: vorhandenen Wert beibehalten, sonst sinnvolles Ziel
                # aus OR/PDH bzw. OR/PDL/PDL-Kontext vorbelegen. 0 bedeutet bewusst: kein Take.
                default_take = _as_float(pos.get("take"))
                if default_take is None:
                    try:
                        _pmeta = find_meta(pos.get("ticker"))
                        _plv = fetch_levels(_pmeta["yf"]) if _pmeta else {}
                        default_take = _as_float(suggest_take_profit(pos.get("side"), pos.get("last") or pos.get("entry"), _plv))
                    except Exception:
                        default_take = None
                new_take = st.number_input(
                    "Take-Profit",
                    min_value=0.0,
                    value=float(default_take or 0.0),
                    key=f"take_{pid}",
                    format="%.4f",
                    help="0 = kein Take festlegen. Vorbelegung nutzt das nächste logische Ziel (OR/PDH bzw. OR/PDL).",
                )
                new_stop = st.number_input(
                    "Stop",
                    value=float(pos.get("stop") or 0),
                    key=f"stop_{pid}",
                    format="%.4f",
                )
            with c:
                if st.button("Take setzen", key=f"set_take_{pid}", use_container_width=True):
                    if update_take(pid, new_take):
                        st.success("Take-Profit gesetzt." if new_take > 0 else "Take-Profit entfernt.")
                        st.rerun()
                if st.button("Stop setzen", key=f"setstop_{pid}", use_container_width=True):
                    ticker = next((p.get("ticker", "") for p in st.session_state.positions if p.get("id") == pid), "")
                    update_stop(pid, new_stop, event_type="stop-set")
                    st.toast(f"🛑 Stop gesetzt: {ticker} @ {new_stop:.4f}", icon="📍")
                    st.rerun()
                if advice.get("trail") and st.button("Stop nachziehen", key=f"trail_{pid}", use_container_width=True):
                    ticker = next((p.get("ticker", "") for p in st.session_state.positions if p.get("id") == pid), "")
                    update_stop(pid, float(advice["trail"]))
                    st.toast(f"🛑 Stop nachgezogen: {ticker} @ {float(advice['trail']):.4f}", icon="📍")
                    st.rerun()
            # Schnellverkauf bewusst außerhalb der Spalten: drei Aktionen
            # untereinander und über die volle verfügbare Breite.
            sell_clicked = st.button(
                "Verkaufen", key=f"sell_{pid}", disabled=sell_amt <= 0, use_container_width=True,
            )
            if sell_clicked:
                pos_before = next((p for p in st.session_state.positions if p.get("id") == pid), {})
                if sell_amt >= held - 0.01:
                    request_trade_confirmation(
                        kind="sell", pid=pid, watch=watch,
                        ticker=pos_before.get("ticker"), side=pos_before.get("side"),
                        amount=pos_before.get("amount", sell_amt),
                        price=pos_before.get("last") or pos_before.get("entry"),
                        stop=pos_before.get("stop"), leverage=pos_before.get("leverage"),
                        typ=pos_before.get("typ"), wkn=pos_before.get("wkn"),
                        pnl=pos_before.get("pnl", 0.0),
                        action="Verkauf", reason="Manueller Verkauf",
                        exec_reason="manual-sell",
                        setup_label=pos_before.get("setup_label") or "OTHER",
                    )
                else:
                    frac = sell_amt / max(held, 1e-9)
                    request_trade_confirmation(
                        kind="partial", pid=pid, fraction=frac, watch=watch,
                        ticker=pos_before.get("ticker"), side=pos_before.get("side"),
                        amount=sell_amt,
                        price=pos_before.get("last") or pos_before.get("entry"),
                        stop=pos_before.get("stop"), leverage=pos_before.get("leverage"),
                        typ=pos_before.get("typ"), wkn=pos_before.get("wkn"),
                        pnl=float(pos_before.get("pnl", 0.0) or 0.0) * frac,
                        action=f"Teilverkauf {frac:.0%}", reason="Teilverkauf",
                        exec_reason="partial-sell",
                        setup_label=pos_before.get("setup_label") or "OTHER",
                    )
            st.markdown("<div style='height:0.25rem'></div>", unsafe_allow_html=True)
            if st.button("50%", key=f"sell50_{pid}", disabled=held <= 0, use_container_width=True):
                    pos_before = next((p for p in st.session_state.positions if p.get("id") == pid), {})
                    request_trade_confirmation(
                        kind="partial", pid=pid, fraction=0.5, watch=watch,
                        ticker=pos_before.get("ticker"), side=pos_before.get("side"),
                        amount=float(pos_before.get("amount", 0) or 0) * 0.5,
                        price=pos_before.get("last") or pos_before.get("entry"),
                        stop=pos_before.get("stop"), leverage=pos_before.get("leverage"),
                        typ=pos_before.get("typ"), wkn=pos_before.get("wkn"),
                        pnl=float(pos_before.get("pnl", 0.0) or 0) * 0.5,
                        action="Teilverkauf 50%", reason="Schnellverkauf 50%",
                        exec_reason="quick-50",
                        setup_label=pos_before.get("setup_label") or "OTHER",
                    )
            st.markdown("<div style='height:0.25rem'></div>", unsafe_allow_html=True)
            if st.button("Alles", key=f"sellall_{pid}", disabled=held <= 0, use_container_width=True):
                    pos_before = next((p for p in st.session_state.positions if p.get("id") == pid), {})
                    request_trade_confirmation(
                        kind="sell", pid=pid, watch=watch,
                        ticker=pos_before.get("ticker"), side=pos_before.get("side"),
                        amount=pos_before.get("amount", 0),
                        price=pos_before.get("last") or pos_before.get("entry"),
                        stop=pos_before.get("stop"), leverage=pos_before.get("leverage"),
                        typ=pos_before.get("typ"), wkn=pos_before.get("wkn"),
                        pnl=pos_before.get("pnl", 0.0),
                        action="Verkauf", reason="Schnellverkauf komplett",
                        exec_reason="quick-all",
                        setup_label=pos_before.get("setup_label") or "OTHER",
                    )
            pos_amt = float(pos.get("amount") or 0)
            # Konsistent mit den anderen Kauf-Pfaden (Empfehlung/Katalog) darf auch beim
            # Nachkauf die GESAMTE Positionsgröße (bestehender Einsatz + Nachkauf) das
            # MAX_POSITION_PCT-Limit nicht überschreiten. Vorher fehlte diese Grenze hier
            # komplett - es ließ sich bis zu 100% des Cash nachschießen.
            limit_pct = get_position_limit_pct()
            position_basis = cash + pos_amt
            max_total_pos = position_basis * limit_pct / 100.0
            max_add = max(0.0, min(max_total_pos - pos_amt, cash))
            can_add = max_add >= 50.0
            r_add = calc_position_size(
                cash,
                None,
                pos.get("last") or pos.get("entry"),
                pos.get("stop"),
                float(pos.get("leverage") or 5),
            )
            suggested = min(max(200.0, cash * 0.1, pos_amt * 0.2), max_add, r_add) if can_add else 0.0
            suggested = float(round(suggested / 50) * 50)
            adds_used = scale_in_count(pos.get("ticker"), pos.get("side"))
            st.caption(
                f"Vorschlag: {suggested:.0f} € · max. Nachkauf {max_add:.0f} € "
                f"({limit_pct:.1f}% + R-Sizing) · Scale-ins {adds_used}/{MAX_SCALE_INS}"
            )
            if advice.get("action") != "ADD":
                st.caption("Nachkauf nur bei ADD-Advice (bestätigte Struktur in MID, Position im Plus).")
            # Streamlit ignoriert `value=` bei bereits vorhandenem session_state-Key
            # (persistiert über Reruns hinweg) - deshalb wurde das Feld nach dem
            # ersten Rendern nicht mehr auf einen neu berechneten Vorschlagswert
            # aktualisiert. Fix: vor jedem Rendern explizit auf den (gedeckelten)
            # Vorschlagswert zurücksetzen, damit das Feld immer den aktuellen
            # Vorschlag zeigt.
            st.session_state[f"addamt_{pid}"] = min(float(suggested), float(max_add)) if can_add else 0.0
            add_amt = st.number_input(
                "Nachkauf (€)",
                min_value=50.0 if can_add else 0.0,
                max_value=float(max_add) if can_add else 0.0,
                step=50.0,
                key=f"addamt_{pid}",
                disabled=not can_add,
            )
            add_ok = (
                can_add
                and advice["action"] == "ADD"
                and sess["playbook"]["buy"]
                and not buy_halt_reason()
                and not cash_block
                and add_amt <= max_add
                and scale_in_count(pos.get("ticker"), pos.get("side")) < MAX_SCALE_INS
            )
            # Verlustposition + Nachkauf: ohne Pro Modus hart blockiert (Fehlermeldung) -
            # "hier darf ich nicht nachkaufen" soll technisch erzwungen werden, nicht nur
            # als Empfehlung erscheinen. Im Pro Modus als bewusste Ausnahme mit expliziter
            # Warnung + Bestätigungs-Checkbox erlaubt.
            pos_in_loss = float(pos.get("pnl", 0.0) or 0.0) < 0
            loss_nachkauf_confirmed = True
            if pos_in_loss:
                if pro_mode_active:
                    st.warning(
                        "⚠️ Diese Position ist aktuell im Verlust. Nachkauf in eine "
                        "Verlustposition erhöht das Risiko zusätzlich (Durchschnittspreis-"
                        "Verwässerung statt Fehlerkorrektur)."
                    )
                    loss_nachkauf_confirmed = st.checkbox(
                        "Ich bestätige den Nachkauf in die Verlustposition trotzdem",
                        key=f"loss_addcheck_{pid}",
                    )
                else:
                    st.error(
                        "🔴 Nachkauf blockiert: Diese Position ist im Verlust. "
                        "Nachkäufe in Verlustpositionen sind ohne Pro Modus nicht erlaubt."
                    )
                    loss_nachkauf_confirmed = False
            add_ok = add_ok and loss_nachkauf_confirmed
            if st.button("Nachkauf ausführen", key=f"add_{pid}", disabled=not add_ok):
                last_px = pos.get("last") or pos.get("entry")
                request_trade_confirmation(
                    kind="buy",
                    ticker=pos["ticker"], side=pos["side"], amount=add_amt, price=last_px,
                    stop=pos.get("stop"), leverage=float(pos.get("leverage") or 5),
                    typ=pos.get("typ") or "Open-End Turbo", wkn=pos.get("wkn"),
                    action="Nachkauf", reason="Nachkauf", exec_reason="nachkauf",
                    setup_label=pos.get("setup_label") or (rec.get("setup_label") if rec else "OTHER"),
                )
            st.divider()
        if st.button("Gesamten Bestand schließen", key="flatten_all_btn"):
            positions_before = list(st.session_state.positions)
            tickers = [p.get("ticker", "") for p in positions_before]
            total_pnl = sum(p.get("pnl", 0.0) for p in positions_before)
            close_amount = sum(float(p.get("amount", 0) or 0) for p in positions_before)
            request_trade_confirmation(
                kind="flatten", watch=watch,
                ticker=", ".join(tickers), side="—",
                amount=close_amount, price=0, pnl=total_pnl,
                action="Bestand schließen", reason="Gesamten Bestand schließen",
                exec_reason="manual",
                setup_label="OTHER",
            )

    # --- EQUITY CURVE (Live-Reporting) ---
    eq_hist = get_equity_history(limit=500, user_id=current_user_id())
    with st.expander("Equity Curve", expanded=False):
        if eq_hist.empty or len(eq_hist) < 2:
            st.caption("Noch zu wenig Datenpunkte — sammelt sich automatisch während der laufenden Session (max. alle 60s ein Snapshot).")
        else:
            eq1, eq2, eq3 = st.columns(3)
            eq1.metric("Equity", f"{eq_hist['equity'].iloc[-1]:,.2f} €")
            eq2.metric("Max Drawdown", f"{eq_hist['drawdown_pct'].min():.2f}%")
            recovery = compute_recovery_time(eq_hist)
            eq3.metric("Recovery Time", recovery or "—")
            fig_eq = go.Figure()
            fig_eq.add_trace(go.Scatter(x=eq_hist["timestamp"], y=eq_hist["equity"], mode="lines", name="Equity", line=dict(color="#00e5ff")))
            fig_eq.update_layout(height=260, margin=dict(l=10, r=10, t=20, b=10), showlegend=False)
            st.plotly_chart(fig_eq, use_container_width=True, config=PLOTLY_CHART_CONFIG)
            fig_dd = go.Figure()
            fig_dd.add_trace(go.Scatter(x=eq_hist["timestamp"], y=eq_hist["drawdown_pct"], mode="lines", fill="tozeroy", name="Drawdown %", line=dict(color="#ef4444")))
            fig_dd.update_layout(height=180, margin=dict(l=10, r=10, t=20, b=10), showlegend=False)
            st.plotly_chart(fig_dd, use_container_width=True, config=PLOTLY_CHART_CONFIG)

    # --- JOURNAL ---
    st.markdown('<div id="nav-journal"></div>', unsafe_allow_html=True)
    with st.expander(f"Ausführungsjournal ({len(st.session_state.get('fills') or [])})", expanded=False):
        if not st.session_state.fills:
            st.caption("Noch keine Fills.")
        else:
            journal = pd.DataFrame(st.session_state.fills)
            if "mood" in journal.columns:
                journal["mood"] = journal["mood"].map(lambda m: MOOD_OPTIONS.get(m, m) if m else "—")
            if "setup_quality" in journal.columns:
                journal["setup_quality"] = journal["setup_quality"].map(
                    lambda q: f"{float(q):.1f}/10" if q is not None and q != "" else "—"
                )
            journal = journal.rename(columns={
                "fee": "Fee",
                "setup_label": "Setup",
                "mood": "Mood",
                "setup_quality": "Setup Quality",
            })
            st.dataframe(journal, use_container_width=True, hide_index=True)

    # --- DAY REPORT ---
    st.markdown('<div id="nav-bericht"></div>', unsafe_allow_html=True)
    # Close-Notizen jetzt direkt hier statt ganz oben auf der Seite (2026-09, auf
    # Wunsch verschoben) - inhaltlich gehören sie zum Tagesabschluss, nicht an den
    # Seitenanfang.
    render_pending_close_notes()
    st.subheader("Tagesbericht")
    st.caption("Nach Close oder manuell. Enthält Einzelorders und Tages-P&L.")
    _notes_open = bool(pending_close_notes())
    day_btn1, day_btn2 = st.columns(2)
    with day_btn1:
        _close_day = st.button(
            "🔒 Handelstag abschließen",
            disabled=_notes_open,
            help="Erst offene Close-Notizen speichern." if _notes_open else None,
            use_container_width=True,
        )
    with day_btn2:
        # Noch ohne Funktion - Platzhalter für den geplanten PDF-Export, analog zu
        # Woche/Monat/Jahr (2026-09 ergänzt, war hier bisher als einziges der vier
        # Abschluss-Buttons ohne PDF-Pendant).
        st.button(
            "📄 Als PDF exportieren", disabled=True, key="day_pdf_export",
            use_container_width=True, help="Kommt noch - PDF-Export ist in Vorbereitung.",
        )
    if _close_day:
        if st.session_state.positions:
            flatten_all(watch, reason="day-end")
        if pending_close_notes():
            save_app_state()
            st.rerun()
        finished_report = build_day_report()
        save_day_report(finished_report)
        # Bugfix (2026-09): Tag jetzt auch sauber versiegeln, genau wie es der
        # automatische Tageswechsel tut. Vorher blieben fills/closed_pnl/etc. nach
        # einem manuellen Abschluss unverändert stehen - maybe_start_new_paper_day()
        # erkannte den Vortag dadurch am Folgetag fälschlich als "nie manuell
        # beendet" (had_activity=True wegen der stehen gebliebenen fills) und
        # speicherte einen ZWEITEN Tagesbericht für denselben Kalendertag, mit
        # ggf. inzwischen abweichenden Werten (z.B. Gebühren) - Ursache für
        # doppelte Tageseinträge in der Wochenübersicht.
        _reset_paper_day_state()
        st.session_state.day_report = finished_report  # Anzeige direkt nach dem Klick erhalten
        save_app_state()
        st.rerun()
    report = st.session_state.get("day_report")
    with st.expander("Tagesbericht-Details", expanded=False):
        if not report:
            st.caption("Noch kein Abschluss — erst handeln, dann Tag schließen.")
        else:
            d1, d2, d3, d4, d5, d6 = st.columns(6)
            d1.metric("Netto P&L", f"{report['day_pct']:+.2f}%", f"{report['day_pnl']:+.2f} €")
            d2.metric("Fee", f"{report.get('fees', 0):.2f} €")
            d3.metric("Closes", report["trades"])
            d4.metric("Trefferquote", f"{report['win_rate']:.0f}%")
            d5.metric("Best", f"{report['best']:+.2f} €")
            d6.metric("Worst", f"{report['worst']:+.2f} €")
            st.write(f"Stand {report['time']} · Opens {report['opens']} · noch offen {report['open_positions']}")
            if report.get("daily_setup"):
                on_plan_rate = report.get("on_plan_rate")
                st.caption(
                    f"📋 Tages-Setup war **{report['daily_setup']}** · "
                    + (f"On-Plan-Quote {on_plan_rate:.0f}% ({report['opens'] - report.get('off_plan_trades', 0)}/{report['opens']} Käufe)"
                       if on_plan_rate is not None else "keine Käufe heute")
                )
            if report["fills"]:
                st.dataframe(pd.DataFrame(report["fills"]), use_container_width=True, hide_index=True)
    # --- WEEKLY REPORT ---
    st.subheader("Wochenbericht")
    st.caption("Tagesübersicht läuft live mit; der endgültige Abschluss erfolgt freitags ab 22:00 CET.")
    week_mon, week_fri = get_week_bounds()
    week_report = load_week_report(week_mon, week_fri)
    # Solange die Woche noch nicht offiziell abgeschlossen ist (kein gespeicherter
    # week_report), live aus den bereits abgeschlossenen Tagesberichten neu aufbauen.
    # So taucht die Ergebniszeile eines Tages sofort nach "Handelstag abschließen" in
    # der Wochenübersicht auf, statt erst freitags sichtbar zu werden.
    week_report_is_live = False
    if not week_report:
        week_report = build_week_report()
        week_report_is_live = week_report is not None
    can_close_week = is_week_end()
    wk_btn1, wk_btn2 = st.columns(2)
    with wk_btn1:
        close_week_clicked = st.button(
            "🔒 Handelswoche abschließen", disabled=not can_close_week, type="secondary",
            use_container_width=True,
        )
    with wk_btn2:
        # Noch ohne Funktion - Platzhalter für den geplanten PDF-Export.
        st.button(
            "📄 Als PDF exportieren", disabled=True, key="week_pdf_export",
            use_container_width=True, help="Kommt noch - PDF-Export ist in Vorbereitung.",
        )
    if close_week_clicked:
        if st.session_state.positions:
            flatten_all(watch, reason="week-end")
        wr = build_week_report()
        if wr:
            save_week_report(week_mon, week_fri, wr)
            st.session_state.week_report = wr
            st.rerun()
        else:
            st.warning("Keine Trades in dieser Woche.")
    if week_report:
        if week_report_is_live:
            st.info(f"Woche {week_report['week_start']} – {week_report['week_end']} (läuft, noch nicht abgeschlossen)")
        else:
            st.success(f"Woche {week_report['week_start']} – {week_report['week_end']}")
        w1, w2, w3, w4, w5, w6 = st.columns(6)
        w1.metric("Trades", week_report["total_trades"])
        w2.metric("Netto P&L (Gross − Fees)", f"{week_report['net_pnl']:+.2f} €")
        w3.metric("Gebühren", f"{week_report['total_fees']:.2f} €")
        w4.metric("Trefferquote", f"{week_report['win_rate']:.0f}%")
        w5.metric("Profit Factor (Brutto)", f"{week_report['profit_factor']:.2f}" if not math.isinf(week_report['profit_factor']) else "∞")
        w6.metric("Ø Win / Loss (Brutto)", f"{week_report['avg_win']:+.2f} / {week_report['avg_loss']:+.2f}")
        st.caption(f"Max (Brutto): {week_report['max_win']:+.2f} € · Min (Brutto): {week_report['max_loss']:+.2f} € · Stand: {week_report['time']}")
        # Day summary table
        day_rows = []
        for day in week_report["days"]:
            day_rows.append({
                "Day": day["date"],
                "Trades": day["trades"],
                "P&L": f"{day['pnl']:+.2f} €",
                "P&L %": f"{day['pct']:+.2f}%",
                "Fees": f"{day['fees']:.2f} €",
                "Win Rate": f"{day['win_rate']:.0f}%",
                "Best": f"{day['best']:+.2f} €",
                "Worst": f"{day['worst']:+.2f} €",
            })
        if day_rows:
            st.caption("Tagesübersicht")
            st.dataframe(pd.DataFrame(day_rows), use_container_width=True, hide_index=True)
        for day in week_report["days"]:
            with st.expander(f"📅 {day['date']} · {day['trades']} Trades · P&L {day['pnl']:+.2f} €"):
                st.write(f"Win Rate: {day['win_rate']:.0f}% · Gebühren: {day['fees']:.2f} €")
                if day["fills"]:
                    st.dataframe(pd.DataFrame(day["fills"]), use_container_width=True, hide_index=True)
    elif not can_close_week:
        st.caption(f"Aktuelle Woche: {week_mon} – {week_fri}. Noch keine abgeschlossenen Handelstage.")

    # --- MONTHLY REPORT ---
    st.subheader("Monatsbericht")
    st.caption("Nur am 1. Tag des Monats für den Vormonat verfügbar.")
    prev_y, prev_m = get_prev_month_bounds()
    prev_ym = f"{prev_y:04d}-{prev_m:02d}"
    month_report = load_month_report(prev_ym)
    can_close_month = is_month_start()
    mo_btn1, mo_btn2 = st.columns(2)
    with mo_btn1:
        close_month_clicked = st.button(
            "🔒 Handelsmonat abschließen", disabled=not can_close_month, type="secondary",
            use_container_width=True,
        )
    with mo_btn2:
        # Noch ohne Funktion - Platzhalter für den geplanten PDF-Export.
        st.button(
            "📄 Als PDF exportieren", disabled=True, key="month_pdf_export",
            use_container_width=True, help="Kommt noch - PDF-Export ist in Vorbereitung.",
        )
    if close_month_clicked:
        mr = build_month_report()
        if mr:
            save_month_report(prev_ym, mr)
            st.session_state.month_report = mr
            st.rerun()
        else:
            st.warning("Keine Trades im Vormonat.")
    if month_report:
        st.success(f"{month_report['month_name']}")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trades", month_report["total_trades"])
        m2.metric("Netto P&L (Gross − Fees)", f"{month_report['net_pnl']:+.2f} €")
        m3.metric("Gebühren", f"{month_report['total_fees']:.2f} €")
        m4.metric("Trefferquote", f"{month_report['win_rate']:.0f}%")
        m5.metric("Profit Factor (Brutto)", f"{month_report['profit_factor']:.2f}" if not math.isinf(month_report['profit_factor']) else "∞")
        st.caption(f"Stand: {month_report['time']}")
        st.info("💡 Steuern (Abgeltungssteuer, Soli, ggf. Kirchensteuer) sind noch nicht eingerechnet.")
        for week in month_report["weeks"]:
            with st.expander(f"KW {week['week']} · {week['trades']} Trades · P&L {week['pnl']:+.2f} €"):
                st.write(f"Win Rate: {week['win_rate']:.0f}% · Gebühren: {week['fees']:.2f} €")
    elif not can_close_month:
        st.caption(f"Bericht für {prev_ym} erst am 1. des Monats verfügbar.")

    # --- YEARLY REPORT ---
    st.subheader("Jahresbericht")
    st.caption("Nur am 1. Januar für das Vorjahr verfügbar.")
    prev_year = get_prev_year()
    year_report = load_year_report(str(prev_year))
    can_close_year = is_year_start()
    yr_btn1, yr_btn2 = st.columns(2)
    with yr_btn1:
        close_year_clicked = st.button(
            "🔒 Handelsjahr abschließen", disabled=not can_close_year, type="secondary",
            use_container_width=True,
        )
    with yr_btn2:
        # Noch ohne Funktion - Platzhalter für den geplanten PDF-Export.
        st.button(
            "📄 Als PDF exportieren", disabled=True, key="year_pdf_export",
            use_container_width=True, help="Kommt noch - PDF-Export ist in Vorbereitung.",
        )
    if close_year_clicked:
        yr = build_year_report()
        if yr:
            save_year_report(str(prev_year), yr)
            st.session_state.year_report = yr
            st.rerun()
        else:
            st.warning("Keine Trades im Vorjahr.")
    if year_report:
        st.success(f"Jahr {year_report['year']}")
        y1, y2, y3, y4, y5 = st.columns(5)
        y1.metric("Trades", year_report["total_trades"])
        y2.metric("Netto P&L (Gross − Fees)", f"{year_report['net_pnl']:+.2f} €")
        y3.metric("Gebühren", f"{year_report['total_fees']:.2f} €")
        y4.metric("Trefferquote", f"{year_report['win_rate']:.0f}%")
        y5.metric("Profit Factor (Brutto)", f"{year_report['profit_factor']:.2f}" if not math.isinf(year_report['profit_factor']) else "∞")
        st.caption(f"Max (Brutto): {year_report['max_win']:+.2f} € · Min (Brutto): {year_report['max_loss']:+.2f} € · Stand: {year_report['time']}")
        st.info("💡 Steuern (Abgeltungssteuer, Soli, ggf. Kirchensteuer) sind noch nicht eingerechnet.")
        for month in year_report["months"]:
            with st.expander(f"{month['month_name']} · {month['trades']} Trades · P&L {month['pnl']:+.2f} €"):
                st.write(f"Win Rate: {month['win_rate']:.0f}% · Gebühren: {month['fees']:.2f} €")
    elif not can_close_year:
        st.caption(f"Bericht für {prev_year} erst am 1. Januar verfügbar.")

    if effective_pro_mode():
        render_prio3_analytics(meta_focus)
    else:
        st.subheader("Backtest & Analytics")
        st.caption(
            "🔒 Nur im **Pro Modus** verfügbar – Schalter in den Einstellungen (⚙️) aktivieren."
        )

    # --- STATE PERSISTENCE ---
    save_app_state()


def splash():
    """Splash im Stil der AstroWeather-App — nur Branding auf VoltDesk geändert."""
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"], [data-testid="stHeader"], [data-testid="stToolbar"] {
            display: none !important;
        }
        .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
            background: #050508 !important;
        }
        .alsplash {
            position: fixed; inset: 0; z-index: 999999;
            background:
                repeating-linear-gradient(0deg, transparent, transparent 39px, rgba(0,229,255,0.06) 40px),
                repeating-linear-gradient(90deg, transparent, transparent 39px, rgba(0,229,255,0.06) 40px),
                #050508;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            font-family: Inter, system-ui, sans-serif;
        }
        .alsplash-k { width: 220px; height: 220px; margin-bottom: 0.4rem;
            filter: drop-shadow(0 0 18px rgba(0,229,255,0.55)); }
        .alsplash-title {
            font-size: 3.1rem; font-weight: 800; color: #ffffff; letter-spacing: -0.03em;
            margin: 0.15rem 0 0 0; line-height: 1.05;
        }
        .alsplash-sub {
            font-size: 1.05rem; font-weight: 700; color: #00e5ff;
            letter-spacing: 0.42em; margin: 0.35rem 0 1.35rem 0;
        }
        .alsplash-badge {
            display: inline-flex; align-items: center; gap: 0.55rem;
            padding: 0.4rem 0.95rem 0.4rem 0.45rem; border-radius: 999px;
            background: #0a0a0a; border: 1px solid rgba(0,229,255,0.4);
            box-shadow: 0 0 14px rgba(0,229,255,0.18);
            color: #e2e8f0; font-size: 0.95rem; font-weight: 500;
        }
        .alsplash-badge strong { color: #00e5ff; }
        .alsplash-bar {
            width: min(420px, 70vw); height: 8px; margin-top: 2.2rem;
            background: #111827; border-radius: 999px; overflow: hidden;
            box-shadow: 0 0 16px rgba(0,229,255,0.18);
        }
        .alsplash-bar > span {
            display: block; height: 100%; width: 0;
            background: linear-gradient(90deg, #00b8d4, #00e5ff);
            border-radius: 999px;
            animation: alsplash-load 2.5s ease-in-out forwards;
        }
        .alsplash-note { margin-top: 1.35rem; color: #64748b; font-size: 0.85rem; }
        @keyframes alsplash-load { from { width: 0; } to { width: 100%; } }
        </style>
        <div class="alsplash">
          <svg class="alsplash-k" viewBox="0 0 200 200" fill="none" xmlns="http://www.w3.org/2000/svg">
            <circle cx="48" cy="30" r="5" fill="#00E5FF"/>
            <circle cx="48" cy="70" r="4.5" fill="#00E5FF"/>
            <circle cx="48" cy="100" r="6" fill="#00E5FF"/>
            <circle cx="48" cy="130" r="4.5" fill="#00E5FF"/>
            <circle cx="48" cy="170" r="5" fill="#00E5FF"/>
            <circle cx="78" cy="70" r="4" fill="#00E5FF"/>
            <circle cx="95" cy="55" r="3.5" fill="#00E5FF"/>
            <circle cx="112" cy="40" r="4.5" fill="#00E5FF"/>
            <circle cx="135" cy="28" r="5" fill="#00E5FF"/>
            <circle cx="78" cy="130" r="4" fill="#00E5FF"/>
            <circle cx="95" cy="145" r="3.5" fill="#00E5FF"/>
            <circle cx="112" cy="160" r="4.5" fill="#00E5FF"/>
            <circle cx="135" cy="172" r="5" fill="#00E5FF"/>
            <circle cx="70" cy="100" r="3.5" fill="#00E5FF"/>
            <circle cx="100" cy="100" r="5" fill="#00E5FF"/>
            <g stroke="#00E5FF" stroke-width="1.4" stroke-linecap="round" opacity="0.9">
              <line x1="48" y1="30" x2="48" y2="70"/>
              <line x1="48" y1="70" x2="48" y2="100"/>
              <line x1="48" y1="100" x2="48" y2="130"/>
              <line x1="48" y1="130" x2="48" y2="170"/>
              <line x1="48" y1="100" x2="70" y2="100"/>
              <line x1="70" y1="100" x2="100" y2="100"/>
              <line x1="48" y1="70" x2="78" y2="70"/>
              <line x1="78" y1="70" x2="95" y2="55"/>
              <line x1="95" y1="55" x2="112" y2="40"/>
              <line x1="112" y1="40" x2="135" y2="28"/>
              <line x1="48" y1="100" x2="95" y2="55"/>
              <line x1="100" y1="100" x2="112" y2="40"/>
              <line x1="48" y1="130" x2="78" y2="130"/>
              <line x1="78" y1="130" x2="95" y2="145"/>
              <line x1="95" y1="145" x2="112" y2="160"/>
              <line x1="112" y1="160" x2="135" y2="172"/>
              <line x1="48" y1="100" x2="95" y2="145"/>
              <line x1="100" y1="100" x2="112" y2="160"/>
            </g>
          </svg>
          <div class="alsplash-title">VoltDesk</div>
          <div class="alsplash-sub">DAYTRADING</div>
          <div class="alsplash-badge">
            <img src="https://avatars.githubusercontent.com/u/316974313?v=4"
                 style="width:20px;height:20px;border-radius:50%;display:inline-block;
                        vertical-align:middle;" alt="Kaisersoft" />
            Made by <strong>Kaisersoft.ai</strong>
          </div>
          <div class="alsplash-bar"><span></span></div>
          <div class="alsplash-note">Paper-Modus · Keine Anlageberatung</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    _sleep(2.7)
    st.session_state.entered = True
    st.session_state.splash_done = True
    st.rerun()


