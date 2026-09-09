# VOLTDESK_EVENT_SOURCE_SWITCHES_V1
# VOLTDESK_PROFIT_MACRO_INSIDER_PATCH_V1
# ==============================================================================
# app.py – VoltDesk Paper-Trading Desk
# Version 0.9.35 – Bestands-UX, Handelszeit-Alerts, Event-Quellen im Einstellungsdialog
# ==============================================================================

import math
import os
import io
import zipfile
import random
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
import sqlite3
import json
import re
from collections import Counter
import hashlib
import base64
import html
import urllib.parse
import urllib.request
try:
    import defusedxml.ElementTree as ET  # Haertung: blockiert Entity-Expansion/XXE bei RSS-Feeds
    ET_PARSE_HARDENED = True
except ImportError:  # Fallback: stdlib (bisheriges Verhalten)
    import xml.etree.ElementTree as ET
    ET_PARSE_HARDENED = False
from contextlib import contextmanager
from pathlib import Path
from time import sleep as _sleep
from itertools import product
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf
import event_sources

# Official Event-Uhr sources. Missing keys simply disable the corresponding feed.
FRED_API_KEY = st.secrets.get("FRED_API_KEY", "")
if not FRED_API_KEY:
    _fred_cfg = st.secrets.get("fred", {})
    if hasattr(_fred_cfg, "get"):
        FRED_API_KEY = _fred_cfg.get("api_key", "")
SEC_USER_AGENT = st.secrets.get("SEC_USER_AGENT", "VoltDesk/1.0 contact@kaisersoft.ai")

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

PLOTLY_CHART_CONFIG = {"displayModeBar": False, "responsive": True}

try:
    from fpdf import FPDF
    HAS_FPDF = True
except ImportError:
    HAS_FPDF = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

try:
    import libsql
    HAS_LIBSQL = True
except ImportError:
    HAS_LIBSQL = False

try:
    from authlib.integrations.requests_client import OAuth2Session
    HAS_AUTHLIB = True
except ImportError:
    HAS_AUTHLIB = False

try:
    import stripe
    HAS_STRIPE = True
except ImportError:
    HAS_STRIPE = False

# ==============================================================================
# ------------------------------------------------------------------------------
# Modular architecture (v0.9.35): feature modules are bound to app globals at
# runtime. This keeps the first split behavior-compatible while we progressively
# remove the legacy monolith dependencies.
import core as _core
import data as _data
import trading as _trading
import analytics as _analytics
import ui as _ui

# Re-export extracted names into the legacy app namespace during the transition.
# Includes private helpers because the existing monolith calls many underscore-prefixed
# functions directly.
for _module in (_core, _data, _trading, _analytics, _ui):
    for _name in dir(_module):
        if _name.startswith("__"):
            continue
        globals()[_name] = getattr(_module, _name)

# --- Auth0 Integration ---
# ==============================================================================
_auth0_secrets = st.secrets.get("auth0", {})

_server_metadata_url = _auth0_secrets.get("server_metadata_url", "")
if _server_metadata_url:
    AUTH0_DOMAIN = _server_metadata_url.replace("https://", "").replace("http://", "").rstrip("/")
else:
    AUTH0_DOMAIN = _auth0_secrets.get("domain", "").rstrip("/")

AUTH0_CLIENT_ID = _auth0_secrets.get("client_id", "")
AUTH0_CLIENT_SECRET = _auth0_secrets.get("client_secret", "")
AUTH0_REDIRECT_URI = _auth0_secrets.get("redirect_uri", "http://localhost:8501/oauth2/callback")

AUTH0_AUTHORIZE_URL = f"https://{AUTH0_DOMAIN}/authorize"
AUTH0_TOKEN_URL = f"https://{AUTH0_DOMAIN}/oauth/token"
AUTH0_USERINFO_URL = f"https://{AUTH0_DOMAIN}/userinfo"
AUTH0_LOGOUT_URL = f"https://{AUTH0_DOMAIN}/v2/logout"


def _init_auth_state():
    st.session_state.setdefault("auth_tokens", None)
    st.session_state.setdefault("auth_user", None)


def _get_oauth_session():
    return OAuth2Session(
        client_id=AUTH0_CLIENT_ID,
        client_secret=AUTH0_CLIENT_SECRET,
        redirect_uri=AUTH0_REDIRECT_URI,
        scope="openid profile email",
    )


def is_authenticated() -> bool:
    """True, wenn ein Nutzer erfolgreich über Auth0 eingeloggt ist."""
    return bool(st.session_state.get("auth_user"))


def current_user_id() -> Optional[str]:
    """Auth0 'sub' (z.B. 'google-oauth2|123...') des eingeloggten Nutzers, oder
    None im Gast-Modus. Alle save_*/load_*-Funktionen scopen ihre Daten darauf;
    im Gast-Modus (None) findet keinerlei DB-Persistenz statt."""
    user = st.session_state.get("auth_user")
    if not user:
        return None
    return user.get("sub")


def _create_oauth_nonce() -> str:
    """Erzeugt einen zufälligen, einmal gültigen State-Wert für den Auth0-
    Login und speichert ihn serverseitig (DB, nicht Session - s. Kommentar
    an der Tabellendefinition). Räumt bei jedem Aufruf nebenbei abgelaufene
    Nonces auf (>10 Min. alt), damit die Tabelle nicht unbegrenzt wächst."""
    nonce = secrets.token_urlsafe(24)
    now = datetime.now(TZ_BERLIN)
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            cutoff = (now - timedelta(minutes=10)).isoformat()
            c.execute("DELETE FROM oauth_nonces WHERE created_at < ?", (cutoff,))
            c.execute("INSERT INTO oauth_nonces (nonce, created_at) VALUES (?, ?)", (nonce, now.isoformat()))
    except Exception:
        pass  # Im Zweifel trotzdem einen (dann nur clientseitig geprüften) Wert liefern
    return nonce


def _consume_oauth_nonce(nonce: Optional[str]) -> bool:
    """Prüft einen State-Wert gegen die serverseitig gespeicherten Nonces und
    verbraucht ihn dabei (Einmal-Gültigkeit, verhindert Replay). True nur bei
    einem noch nicht verbrauchten, nicht abgelaufenen (<10 Min.) Treffer."""
    if not nonce:
        return False
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            cutoff = (datetime.now(TZ_BERLIN) - timedelta(minutes=10)).isoformat()
            row = c.execute(
                "SELECT nonce FROM oauth_nonces WHERE nonce = ? AND created_at >= ?", (nonce, cutoff)
            ).fetchone()
            if not row:
                return False
            c.execute("DELETE FROM oauth_nonces WHERE nonce = ?", (nonce,))
            return True
    except Exception:
        return False


def log_error(context: str, message: str, user_id: Optional[str] = None) -> None:
    """Selektives Fehlerprotokoll für sonst stille Hintergrundpfade (Auth0-
    Callback, Stripe-Sync/Reconcile, DB-Migrationen). Gäste-Fehler werden
    bewusst NICHT geloggt (Gast-Modus hat konsequent keine Persistenz).
    30 Tage Aufbewahrung, ältere Einträge werden bei jedem Aufruf aufgeräumt.
    Darf selbst nie eine Exception werfen - Logging soll nie die App stören.
    user_id optional explizit übergebbar (z.B. _claim_legacy_data), sonst
    current_user_id() aus der Session - vermeidet Race Conditions, falls der
    Session-Status zum Log-Zeitpunkt noch nicht sicher aktuell ist."""
    uid = user_id or current_user_id()
    if not uid:
        return
    now = datetime.now(TZ_BERLIN)
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            cutoff = (now - timedelta(days=30)).isoformat()
            c.execute("DELETE FROM error_log WHERE timestamp < ?", (cutoff,))
            c.execute(
                "INSERT INTO error_log (user_id, timestamp, context, message) VALUES (?, ?, ?, ?)",
                (uid, now.isoformat(), context, str(message)[:2000]),
            )
    except Exception:
        pass


def log_critical_trade_event(
    event_type: str, ticker: str, side: Optional[str], details: dict,
    requires_broker_action: bool = True,
) -> None:
    """CRITICAL TRADE EVENT (2026-09): für Ereignisse, die der Nutzer im ECHTEN
    Broker nachziehen muss (Stop/Take/KO/Auto-Close/Daily-Loss-Lock/Kill-
    Switch), damit Desk und Broker-Portfolio nicht auseinanderlaufen.
    DB-persistiert (überlebt Cold-Starts, s. pending_critical_events) - nur für
    eingeloggte Nutzer, Gast-Modus hat konsequent keine Persistenz. Guest-
    Fallback: session-lokale, nicht-blockierende Flash-Meldung, da ohne
    user_id kein DB-Eintrag möglich ist. Darf selbst nie eine Exception werfen."""
    user_id = current_user_id()
    if not user_id:
        st.session_state["flatten_flash"] = (
            f"⚠️ {event_type}: {ticker} - bitte im Broker nachziehen (Gast-Modus, nicht gespeichert)."
        )
        return
    try:
        created_at = datetime.now(TZ_BERLIN).isoformat()
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO pending_critical_events "
                "(user_id, event_type, ticker, side, details_json, requires_broker_action, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, event_type, ticker, side, json.dumps(details),
                 1 if requires_broker_action else 0, created_at),
            )
        return True
    except Exception as e:
        fallback = st.session_state.setdefault("critical_event_fallback", [])
        fallback.append({"id": f"session-{uuid.uuid4().hex}", "event_type": event_type,
                         "ticker": ticker, "side": side, "details": details,
                         "requires_broker_action": bool(requires_broker_action),
                         "created_at": datetime.now(TZ_BERLIN).isoformat()})
        log_error("critical_trade_event", f"{event_type} {ticker}: {e}", user_id=user_id)
        return False


def get_pending_critical_events() -> list:
    user_id = current_user_id()
    if not user_id:
        return []
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            rows = c.execute(
                "SELECT id, event_type, ticker, side, details_json, requires_broker_action, created_at "
                "FROM pending_critical_events WHERE user_id = ? AND acknowledged_at IS NULL "
                "ORDER BY created_at",
                (user_id,),
            ).fetchall()
    except Exception as e:
        # Bei DB-Lesefehler niemals vorhandene Session-Fallback-Events verstecken.
        log_error("get_pending_critical_events", str(e))
        return list(st.session_state.get("critical_event_fallback") or [])
    events = [
        {
            "id": r[0], "event_type": r[1], "ticker": r[2], "side": r[3],
            "details": json.loads(r[4]), "requires_broker_action": bool(r[5]),
            "created_at": r[6],
        }
        for r in rows
    ]
    events.extend(list(st.session_state.get("critical_event_fallback") or []))
    return events


def acknowledge_critical_events(ids: list) -> None:
    ids = set(ids or [])
    if not ids:
        return
    fallback = st.session_state.get("critical_event_fallback") or []
    st.session_state["critical_event_fallback"] = [ev for ev in fallback if ev.get("id") not in ids]
    user_id = current_user_id()
    if not user_id:
        return
    now = datetime.now(TZ_BERLIN).isoformat()
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            placeholders = ",".join("?" * len(ids))
            c.execute(
                f"UPDATE pending_critical_events SET acknowledged_at = ? "
                f"WHERE id IN ({placeholders}) AND user_id = ?",
                (now, *ids, user_id),
            )
    except Exception as e:
        log_error("critical_trade_event_ack", str(e), user_id=user_id)


def _auth_login_url() -> str:
    nonce = _create_oauth_nonce()
    return (
        f"{AUTH0_AUTHORIZE_URL}?"
        f"response_type=code&"
        f"client_id={AUTH0_CLIENT_ID}&"
        f"redirect_uri={urllib.parse.quote(AUTH0_REDIRECT_URI, safe='')}&"
        f"scope=openid%20profile%20email&"
        f"state={nonce}"
    )


def _handle_auth_callback():
    """Verarbeitet den Auth0-Redirect (?code=...). Früh in main() aufrufen,
    bevor der Rest der Seite gerendert wird."""
    if "code" not in st.query_params:
        return
    code = st.query_params["code"]
    state = st.query_params.get("state")
    # Query-Params sofort leeren, bevor irgendein Rerun (z.B. Autorefresh) den
    # Code ein zweites Mal verarbeitet - Auth0-Codes sind nur einmal einlösbar.
    st.query_params.clear()
    # Nach einem Login-Versuch immer direkt zum Dashboard, nie zurück zum
    # Splash-Screen - sonst ist die Sidebar (und damit jede Rückmeldung zum
    # Login) durch das Splash-CSS unsichtbar.
    st.session_state.entered = True
    if not _consume_oauth_nonce(state):
        st.session_state["_auth_error"] = "Ungültiger oder abgelaufener Login-Versuch - bitte erneut einloggen."
        st.rerun()
        return
    try:
        oauth = _get_oauth_session()
        token = oauth.fetch_token(AUTH0_TOKEN_URL, code=code)
        st.session_state.auth_tokens = token
        st.session_state.auth_user = oauth.get(AUTH0_USERINFO_URL).json()
        st.session_state["_auth_just_logged_in"] = True
        _uid = current_user_id()
        if _uid:
            _claim_legacy_data(_uid)
            _ensure_trial_started(_uid)
            _sync_subscription_status(_uid, force=True)
            st.session_state["_legacy_claim_done"] = True
        # State-Neuladen erzwingen: init_state() lädt beim nächsten Durchlauf
        # die persistierten Daten dieses (jetzt bekannten) Nutzers statt der
        # Gast-Defaults. _skip_splash_reset verhindert, dass dieser erzwungene
        # Reload den Nutzer dabei zurück auf den Splash-Screen wirft.
        st.session_state["_skip_splash_reset"] = True
        st.session_state.pop("_state_loaded", None)
    except Exception as e:
        st.session_state.auth_tokens = None
        st.session_state.auth_user = None
        st.session_state["_auth_error"] = str(e)
    st.rerun()


def _same_tab_redirect(url: str):
    """Navigiert den AKTUELLEN Tab per Meta-Refresh weiter, statt (wie
    st.link_button es laut Streamlit-Doku immer tut) einen neuen Tab mit einer
    komplett neuen Session zu öffnen. Wichtig für jeden Redirect-Flow, der
    zur selben Session zurückkehren muss (Login, Stripe Checkout/Portal)."""
    st.markdown(f'<meta http-equiv="refresh" content="0; url={url}">', unsafe_allow_html=True)


def _auth_logout():
    st.session_state.auth_tokens = None
    st.session_state.auth_user = None
    st.session_state.pro_mode = False
    st.session_state.pop("_legacy_claim_done", None)
    # State-Neuladen erzwingen: ohne eingeloggten Nutzer gibt es keine
    # Persistenz mehr - init_state() setzt beim nächsten Durchlauf die
    # Gast-Defaults, damit keine Daten des vorherigen Accounts sichtbar bleiben.
    st.session_state.pop("_state_loaded", None)
    return_to = AUTH0_REDIRECT_URI.split("/oauth2")[0]
    logout_url = (
        f"{AUTH0_LOGOUT_URL}?"
        f"returnTo={urllib.parse.quote(return_to, safe='')}&"
        f"client_id={AUTH0_CLIENT_ID}"
    )
    st.markdown(f'<meta http-equiv="refresh" content="0; url={logout_url}">', unsafe_allow_html=True)




# ==============================================================================
# --- Stripe Trial & Subscription ---
# ==============================================================================
# Streamlit kann keine Webhooks empfangen (kein öffentlicher HTTP-Endpoint für
# beliebige Routen) - Abo-Status wird deshalb aktiv über die Stripe-API
# geprüft/synchronisiert (beim Login, gedrosselt), nicht passiv per Webhook.
# Checkout und Kundenportal laufen als Redirect-Flow, analog zu Auth0.
_stripe_secrets = st.secrets.get("stripe", {})
STRIPE_SECRET_KEY = _stripe_secrets.get("secret_key", "")
STRIPE_PRICE_ID = _stripe_secrets.get("price_id", "")
STRIPE_RETURN_URL = _stripe_secrets.get("return_url") or AUTH0_REDIRECT_URI.split("/oauth2")[0] or "http://localhost:8501/"
TRIAL_DAYS = 7
WATCHLIST_LIMIT_FREE = 5
WATCHLIST_LIMIT_PRO = 10
FEATURE_COMPARISON_URL = "https://raw.githubusercontent.com/kaisersoft/assets/main/VoltDesk_Featurevergleich.jpg"
_SUBSCRIPTION_SYNC_INTERVAL_SECONDS = 3600  # max. 1x/Stunde live bei Stripe nachfragen

if HAS_STRIPE and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def _ensure_trial_started(user_id: str):
    """Legt beim allerersten Kontakt eines Nutzers eine subscriptions-Zeile mit
    trial_start=jetzt an. INSERT OR IGNORE: bei jedem weiteren Login ein No-Op,
    der Trial startet also garantiert nur genau einmal pro Nutzer."""
    if not user_id:
        return
    now = datetime.now(TZ_BERLIN).isoformat()
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR IGNORE INTO subscriptions (user_id, trial_start, updated_at) VALUES (?, ?, ?)",
                (user_id, now, now),
            )
    except Exception as e:
        # Nicht mehr lautlos: ein fehlgeschlagener Trial-Start wuerde sonst nie auffallen.
        log_error("ensure_trial_started", str(e), user_id=user_id)


def get_subscription_row(user_id: str) -> Optional[dict]:
    if not user_id:
        return None
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT stripe_customer_id, stripe_subscription_id, status, current_period_end, "
            "trial_start FROM subscriptions WHERE user_id = ?", (user_id,)
        )
        row = c.fetchone()
    if not row:
        return None
    return {
        "stripe_customer_id": row[0],
        "stripe_subscription_id": row[1],
        "status": row[2],
        "current_period_end": row[3],
        "trial_start": row[4],
    }


def _trial_end(sub: Optional[dict]) -> Optional[datetime]:
    if not sub or not sub.get("trial_start"):
        return None
    try:
        start = datetime.fromisoformat(sub["trial_start"])
    except Exception:
        return None
    return start + timedelta(days=TRIAL_DAYS)


def is_trial_active(sub: Optional[dict]) -> bool:
    end = _trial_end(sub)
    return bool(end and datetime.now(TZ_BERLIN) < end)


def trial_days_left(sub: Optional[dict]) -> int:
    end = _trial_end(sub)
    if not end:
        return 0
    remaining = end - datetime.now(TZ_BERLIN)
    return max(0, math.ceil(remaining.total_seconds() / 86400))


def is_subscription_active(sub: Optional[dict]) -> bool:
    if not sub or not sub.get("status"):
        return False
    if sub["status"] not in ("active", "trialing"):
        return False
    cpe = sub.get("current_period_end")
    if cpe:
        try:
            if datetime.fromisoformat(cpe) < datetime.now(TZ_BERLIN):
                return False
        except Exception:
            pass
    return True


def has_pro_access() -> bool:
    """Zugriffsregel für Pro Modus: Login + (aktiver Trial ODER aktives Abo)."""
    user_id = current_user_id()
    if not user_id:
        return False
    sub = get_subscription_row(user_id)
    return is_trial_active(sub) or is_subscription_active(sub)


def effective_pro_mode() -> bool:
    """Tatsächlich wirksamer Pro-Modus (2026-09-Fix): Toggle-Wert UND echte
    Berechtigung (Login + aktiver Trial/Abo) gleichzeitig. Vorher lasen die
    meisten Stellen im Code direkt session_state.pro_mode - das konnte
    kurzzeitig veraltet sein (v.a. in isolierten @st.fragment-Reruns, die
    NICHT durch die Zwangs-Reset-Prüfung in dashboard() laufen, z.B. wenn
    Trial/Abo zwischen zwei vollen Reruns abläuft), und an mindestens einer
    Stelle (Watchlist-Limit) wurde stattdessen fälschlich has_pro_access()
    allein geprüft - das entkoppelte das Limit komplett vom manuellen
    Toggle. Für ALLES, was Pro-Funktionen tatsächlich freischaltet oder
    anzeigt (Hebel-Cap, Logo, Limits, Gebühren), ab jetzt einheitlich diese
    Funktion nutzen statt direkt session_state.pro_mode oder has_pro_access()
    einzeln zu lesen."""
    return bool(st.session_state.get("pro_mode", False)) and has_pro_access()


def _save_subscription_from_stripe(user_id: str, customer_id: str, sub_obj) -> None:
    """Schreibt Status/Laufzeitende eines Stripe-Subscription-Objekts in die DB.
    Attribut-Zugriff (sub_obj.x) statt .get()/Bracket - robuster über
    stripe-python-Versionen hinweg (s. ListObject-Vorfall bei .get())."""
    try:
        current_period_end = datetime.fromtimestamp(
            sub_obj.current_period_end, tz=timezone.utc
        ).isoformat()
    except Exception:
        current_period_end = None
    now = datetime.now(TZ_BERLIN).isoformat()
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE subscriptions SET stripe_customer_id=?, stripe_subscription_id=?, "
                "status=?, current_period_end=?, updated_at=? WHERE user_id=?",
                (customer_id, sub_obj.id, sub_obj.status, current_period_end, now, user_id),
            )
    except Exception as e:
        log_error("save_subscription", f"user={user_id}: {e}")


def _reconcile_subscription_by_email(user_id: str, email: str) -> bool:
    """Fallback-Sicherheitsnetz: falls die Zuordnung beim Checkout-Rücksprung
    verpasst wurde (z.B. weil der neue Tab, den st.link_button öffnet, beim
    Rücksprung von Stripe noch nie bei uns eingeloggt war), hier aktiv bei
    Stripe per E-Mail nach einem Customer mit aktivem Abo suchen und
    nachträglich zuordnen. _start_checkout_session() setzt beim allerersten
    Checkout immer customer_email, Stripe legt dafür zuverlässig einen
    Customer mit dieser E-Mail an - genau darüber lässt sich das hier wieder
    finden, unabhängig vom Redirect."""
    if not HAS_STRIPE or not STRIPE_SECRET_KEY or not email:
        return False
    try:
        customers = stripe.Customer.list(email=email, limit=5)
        for cust in customers.data:
            subs = stripe.Subscription.list(customer=cust.id, status="all", limit=5)
            for sub in subs.data:
                if sub.status in ("active", "trialing"):
                    _save_subscription_from_stripe(user_id, cust.id, sub)
                    return True
    except Exception as e:
        # Bewusst NICHT mehr still verschlucken - sonst ist ein API-/Rechte-
        # Fehler von "kein Abo gefunden" nicht mehr zu unterscheiden.
        st.session_state["_stripe_error"] = f"Abgleich fehlgeschlagen: {e}"
        log_error("reconcile_subscription", f"user={user_id} email={email}: {e}")
    return False


def _sync_subscription_status(user_id: str, force: bool = False):
    """Fragt den aktuellen Abo-Status live bei Stripe ab und schreibt ihn in
    die DB - gedrosselt auf max. 1x/Stunde pro Session, da wir keine Webhooks
    empfangen können und uns sonst nur auf den Stand vom letzten Checkout
    verlassen würden (verpasste Kündigungen/Zahlungsausfälle)."""
    if not HAS_STRIPE or not STRIPE_SECRET_KEY or not user_id:
        return
    last = st.session_state.get("_last_sub_sync_at")
    if not force and last and (datetime.now(TZ_BERLIN) - last).total_seconds() < _SUBSCRIPTION_SYNC_INTERVAL_SECONDS:
        return
    st.session_state["_last_sub_sync_at"] = datetime.now(TZ_BERLIN)
    sub = get_subscription_row(user_id)
    if not sub or not sub.get("stripe_subscription_id"):
        # Noch keine lokal bekannte Zuordnung - per E-Mail nachschauen, statt
        # dauerhaft "kein Abo" anzunehmen, obwohl bei Stripe längst eins existiert.
        email = (st.session_state.auth_user or {}).get("email")
        if email:
            _reconcile_subscription_by_email(user_id, email)
        return
    try:
        live = stripe.Subscription.retrieve(sub["stripe_subscription_id"])
        _save_subscription_from_stripe(user_id, sub.get("stripe_customer_id") or live.customer, live)
    except Exception as e:
        # Sync darf die App nie zum Absturz bringen - alter DB-Stand bleibt gültig
        log_error("sync_subscription", f"user={user_id}: {e}")


def _start_checkout_session(user_id: str, email: Optional[str]) -> Optional[str]:
    if not HAS_STRIPE or not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        return None
    sub = get_subscription_row(user_id)
    customer_id = sub.get("stripe_customer_id") if sub else None
    try:
        kwargs = dict(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"{STRIPE_RETURN_URL}?stripe_session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=STRIPE_RETURN_URL,
            client_reference_id=user_id,
        )
        if customer_id:
            kwargs["customer"] = customer_id
        elif email:
            kwargs["customer_email"] = email
        session = stripe.checkout.Session.create(**kwargs)
        return session.url
    except Exception as e:
        st.session_state["_stripe_error"] = str(e)
        return None


def _start_portal_session(user_id: str) -> Optional[str]:
    if not HAS_STRIPE or not STRIPE_SECRET_KEY:
        return None
    sub = get_subscription_row(user_id)
    customer_id = sub.get("stripe_customer_id") if sub else None
    if not customer_id:
        return None
    try:
        session = stripe.billing_portal.Session.create(customer=customer_id, return_url=STRIPE_RETURN_URL)
        return session.url
    except Exception as e:
        st.session_state["_stripe_error"] = str(e)
        return None


def _handle_stripe_return():
    """Verarbeitet den Redirect von Stripe Checkout (?stripe_session_id=...).
    Früh in main() aufrufen, analog zu _handle_auth_callback().

    WICHTIG: Checkout läuft über st.link_button in einem NEUEN Tab, der beim
    Rücksprung von Stripe noch NIE bei uns eingeloggt war - current_user_id()
    ist hier also praktisch immer None, selbst bei erfolgreicher Zahlung. Die
    Zuordnung darf sich deshalb NICHT auf die lokale Session verlassen,
    sondern MUSS aus Stripes eigenem client_reference_id kommen, das beim
    Erstellen der Checkout-Session gesetzt wurde (s. _start_checkout_session)."""
    if "stripe_session_id" not in st.query_params:
        return
    session_id = st.query_params["stripe_session_id"]
    st.query_params.clear()
    st.session_state.entered = True
    if not HAS_STRIPE or not STRIPE_SECRET_KEY:
        st.rerun()
        return
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id, expand=["subscription"])
        ref_user_id = getattr(checkout_session, "client_reference_id", None)
        sub_obj = getattr(checkout_session, "subscription", None)
        if ref_user_id and sub_obj:
            _save_subscription_from_stripe(
                ref_user_id, checkout_session.customer, sub_obj
            )
            # Diese Session kennt evtl. gar keinen eingeloggten Nutzer (frischer
            # Tab) - trotzdem eine Bestätigung zeigen, damit die Zahlung nicht
            # kommentarlos verpufft. Zeigt "Abo aktiv", falls dieser Tab zufällig
            # doch für denselben Nutzer eingeloggt ist, sonst einen Hinweis,
            # sich einzuloggen, um den Pro-Status zu sehen.
            st.session_state["_stripe_checkout_confirmed_for"] = ref_user_id
        elif getattr(checkout_session, "payment_status", None) not in ("paid", "no_payment_required"):
            st.session_state["_stripe_error"] = "Checkout wurde nicht abgeschlossen (keine Zahlung erfasst)."
    except Exception as e:
        st.session_state["_stripe_error"] = str(e)
    st.rerun()


def _maybe_show_stripe_confirmation():
    """Zeigt die Buchungsbestätigung unabhängig davon, ob DIESER Tab gerade
    eingeloggt ist - s. Kommentar in _handle_stripe_return(). Am Anfang der
    Sidebar aufrufen, immer (nicht nur wenn is_authenticated())."""
    confirmed_uid = st.session_state.pop("_stripe_checkout_confirmed_for", None)
    if not confirmed_uid:
        return
    if current_user_id() == confirmed_uid:
        st.toast("Abo aktiv - danke!", icon="✅")
    else:
        st.success("✅ Zahlung erfolgreich! Bitte einloggen, um den Pro-Status zu sehen.")


def _cached_redirect_url(cache_key: str, create_fn, ttl_seconds: int = 300) -> Optional[str]:
    """Cacht eine Checkout-/Portal-URL kurzzeitig in der Session, damit
    st.link_button (das die URL beim Rendern kennen muss, nicht erst beim
    Klick) nicht bei jedem Rerun eine neue Stripe-Session anlegt."""
    cached = st.session_state.get(cache_key)
    now = datetime.now(TZ_BERLIN)
    if cached and (now - cached["at"]).total_seconds() < ttl_seconds:
        return cached["url"]
    url = create_fn()
    st.session_state[cache_key] = {"url": url, "at": now}
    return url




ASSETS_BASE_URL = "https://raw.githubusercontent.com/kaisersoft/assets/main"

st.set_page_config(
    page_title="VoltDesk",
    page_icon=f"{ASSETS_BASE_URL}/favicon-32x32.png",
    layout="wide",
    initial_sidebar_state="expanded",
)

# App-Icons (2026-09): immer als raw-Datei aus dem öffentlichen GitHub-Repo geladen,
# bewusst NICHT als Base64 im Code eingebettet (kein Aufblähen der app.py, Icons
# lassen sich im Repo austauschen, ohne die App neu zu deployen).
# page_icon oben deckt den Browser-Tab zuverlässig ab (nativ von Streamlit
# unterstützt). Die zusätzlichen <link>-Tags hier (Apple-Touch-Icon, Android-
# Chrome, klassisches favicon.ico/-.svg) sind Best-Effort: Streamlit fügt
# st.markdown-Inhalte in den <body> ein, nicht in <head> - für "Zum Homescreen
# hinzufügen" auf iOS/Android ist eigentlich ein <head>-Eintrag vorgesehen.
# Manche Browser werten <link>-Tags aber auch außerhalb von <head> aus; wo
# nicht, bleibt zumindest der Browser-Tab-Favicon oben zuverlässig gesetzt.
st.markdown(
    f"""
    <link rel="icon" type="image/png" sizes="16x16" href="{ASSETS_BASE_URL}/favicon-16x16.png">
    <link rel="icon" type="image/png" sizes="32x32" href="{ASSETS_BASE_URL}/favicon-32x32.png">
    <link rel="icon" type="image/svg+xml" href="{ASSETS_BASE_URL}/favicon.svg">
    <link rel="shortcut icon" href="{ASSETS_BASE_URL}/favicon.ico">
    <link rel="apple-touch-icon" sizes="180x180" href="{ASSETS_BASE_URL}/apple-touch-icon.png">
    <link rel="icon" type="image/png" sizes="192x192" href="{ASSETS_BASE_URL}/android-chrome-192x192.png">
    <link rel="icon" type="image/png" sizes="512x512" href="{ASSETS_BASE_URL}/android-chrome-512x512.png">
    """,
    unsafe_allow_html=True,
)

UNIVERSE = {
    "Indizes": {
        "USA": [
            {"ticker": "SPY", "yf": "SPY", "name": "S&P 500 (SPY)"},
            {"ticker": "QQQ", "yf": "QQQ", "name": "Nasdaq-100 (QQQ)"},
            {"ticker": "DIA", "yf": "DIA", "name": "Dow Jones (DIA)"},
            {"ticker": "IWM", "yf": "IWM", "name": "Russell 2000 (IWM)"},
        ],
        "Europa": [
            {"ticker": "DAX", "yf": "^GDAXI", "name": "DAX"},
            {"ticker": "ESTX50", "yf": "^STOXX50E", "name": "Euro Stoxx 50"},
            {"ticker": "FTSE", "yf": "^FTSE", "name": "FTSE 100"},
            {"ticker": "SMI", "yf": "^SSMI", "name": "SMI"},
        ],
    },
    "USA": {
        "S&P 500": [
            {"ticker": "NVDA", "yf": "NVDA", "name": "NVIDIA"},
            {"ticker": "TSLA", "yf": "TSLA", "name": "Tesla"},
            {"ticker": "INTC", "yf": "INTC", "name": "Intel"},
            {"ticker": "KO", "yf": "KO", "name": "Coca-Cola"},
            {"ticker": "JPM", "yf": "JPM", "name": "JPMorgan Chase"},
            {"ticker": "UNH", "yf": "UNH", "name": "UnitedHealth"},
            {"ticker": "V", "yf": "V", "name": "Visa"},
            {"ticker": "XOM", "yf": "XOM", "name": "Exxon Mobil"},
            {"ticker": "WMT", "yf": "WMT", "name": "Walmart"},
            {"ticker": "PG", "yf": "PG", "name": "Procter & Gamble"},
            {"ticker": "MA", "yf": "MA", "name": "Mastercard"},
            {"ticker": "HD", "yf": "HD", "name": "Home Depot"},
            {"ticker": "CVX", "yf": "CVX", "name": "Chevron"},
            {"ticker": "MRK", "yf": "MRK", "name": "Merck"},
            {"ticker": "LLY", "yf": "LLY", "name": "Eli Lilly"},
        ],
        "Dow Jones": [
            {"ticker": "NVDA", "yf": "NVDA", "name": "NVIDIA"},
            {"ticker": "AAPL", "yf": "AAPL", "name": "Apple"},
            {"ticker": "AMZN", "yf": "AMZN", "name": "Amazon"},
            {"ticker": "AXP", "yf": "AXP", "name": "American Express"},
            {"ticker": "BA", "yf": "BA", "name": "Boeing"},
            {"ticker": "CAT", "yf": "CAT", "name": "Caterpillar"},
            {"ticker": "CRM", "yf": "CRM", "name": "Salesforce"},
            {"ticker": "CSCO", "yf": "CSCO", "name": "Cisco"},
            {"ticker": "CVX", "yf": "CVX", "name": "Chevron"},
            {"ticker": "DIS", "yf": "DIS", "name": "Walt Disney"},
            {"ticker": "GOOGL", "yf": "GOOGL", "name": "Alphabet"},
            {"ticker": "GS", "yf": "GS", "name": "Goldman Sachs"},
            {"ticker": "HD", "yf": "HD", "name": "Home Depot"},
            {"ticker": "HON", "yf": "HON", "name": "Honeywell"},
            {"ticker": "IBM", "yf": "IBM", "name": "IBM"},
            {"ticker": "JNJ", "yf": "JNJ", "name": "Johnson & Johnson"},
            {"ticker": "JPM", "yf": "JPM", "name": "JPMorgan Chase"},
            {"ticker": "KO", "yf": "KO", "name": "Coca-Cola"},
            {"ticker": "MCD", "yf": "MCD", "name": "McDonald's"},
            {"ticker": "MMM", "yf": "MMM", "name": "3M"},
            {"ticker": "MRK", "yf": "MRK", "name": "Merck"},
            {"ticker": "MSFT", "yf": "MSFT", "name": "Microsoft"},
            {"ticker": "NKE", "yf": "NKE", "name": "Nike"},
            {"ticker": "PG", "yf": "PG", "name": "Procter & Gamble"},
            {"ticker": "SHW", "yf": "SHW", "name": "Sherwin-Williams"},
            {"ticker": "TRV", "yf": "TRV", "name": "Travelers"},
            {"ticker": "UNH", "yf": "UNH", "name": "UnitedHealth"},
            {"ticker": "V", "yf": "V", "name": "Visa"},
            {"ticker": "WMT", "yf": "WMT", "name": "Walmart"},
            {"ticker": "AMGN", "yf": "AMGN", "name": "Amgen"},
        ],
        "Nasdaq-100": [
            {"ticker": "AMD", "yf": "AMD", "name": "AMD"},
            {"ticker": "APP", "yf": "APP", "name": "AppLovin"},
            {"ticker": "ARM", "yf": "ARM", "name": "Arm Holdings"},
            {"ticker": "MU", "yf": "MU", "name": "Micron"},
            {"ticker": "AMAT", "yf": "AMAT", "name": "Applied Materials"},
            {"ticker": "AAPL", "yf": "AAPL", "name": "Apple"},
            {"ticker": "MSFT", "yf": "MSFT", "name": "Microsoft"},
            {"ticker": "GOOGL", "yf": "GOOGL", "name": "Alphabet"},
            {"ticker": "META", "yf": "META", "name": "Meta"},
            {"ticker": "AVGO", "yf": "AVGO", "name": "Broadcom"},
            {"ticker": "PEP", "yf": "PEP", "name": "PepsiCo"},
            {"ticker": "COST", "yf": "COST", "name": "Costco"},
            {"ticker": "ADBE", "yf": "ADBE", "name": "Adobe"},
            {"ticker": "NFLX", "yf": "NFLX", "name": "Netflix"},
            {"ticker": "TMUS", "yf": "TMUS", "name": "T-Mobile"},
        ],
    },
    "UK": {
        "FTSE 100": [
            {"ticker": "AZN", "yf": "AZN.L", "name": "AstraZeneca"},
            {"ticker": "SHEL", "yf": "SHEL.L", "name": "Shell"},
            {"ticker": "HSBA", "yf": "HSBA.L", "name": "HSBC"},
            {"ticker": "BP", "yf": "BP.L", "name": "BP"},
            {"ticker": "ULVR", "yf": "ULVR.L", "name": "Unilever"},
            {"ticker": "RIO", "yf": "RIO.L", "name": "Rio Tinto"},
            {"ticker": "BATS", "yf": "BATS.L", "name": "British American Tobacco"},
            {"ticker": "DGE", "yf": "DGE.L", "name": "Diageo"},
            {"ticker": "GSK", "yf": "GSK.L", "name": "GSK"},
            {"ticker": "VOD", "yf": "VOD.L", "name": "Vodafone"},
            {"ticker": "LLOY", "yf": "LLOY.L", "name": "Lloyds"},
            {"ticker": "BARC", "yf": "BARC.L", "name": "Barclays"},
            {"ticker": "NG", "yf": "NG.L", "name": "National Grid"},
            {"ticker": "REL", "yf": "REL.L", "name": "RELX"},
            {"ticker": "AAL", "yf": "AAL.L", "name": "Anglo American"},
        ],
    },
    "EU": {
        "DAX": [
            {"ticker": "IFX", "yf": "IFX.DE", "name": "Infineon"},
            {"ticker": "ENR", "yf": "ENR.DE", "name": "Siemens Energy"},
            {"ticker": "SAP", "yf": "SAP.DE", "name": "SAP"},
            {"ticker": "RHM", "yf": "RHM.DE", "name": "Rheinmetall"},
            {"ticker": "SIE", "yf": "SIE.DE", "name": "Siemens"},
            {"ticker": "ALV", "yf": "ALV.DE", "name": "Allianz"},
            {"ticker": "DTE", "yf": "DTE.DE", "name": "Deutsche Telekom"},
            {"ticker": "BAS", "yf": "BAS.DE", "name": "BASF"},
            {"ticker": "MRK", "yf": "MRK.DE", "name": "Merck KGaA"},
            {"ticker": "BMW", "yf": "BMW.DE", "name": "BMW"},
            {"ticker": "MBG", "yf": "MBG.DE", "name": "Mercedes-Benz"},
            {"ticker": "ADS", "yf": "ADS.DE", "name": "adidas"},
            {"ticker": "HEI", "yf": "HEI.DE", "name": "Heidelberg Materials"},
            {"ticker": "SHL", "yf": "SHL.DE", "name": "Siemens Healthineers"},
            {"ticker": "ZAL", "yf": "ZAL.DE", "name": "Zalando"},
        ],
        "MDAX": [
            {"ticker": "AIXA", "yf": "AIXA.DE", "name": "AIXTRON"},
            {"ticker": "AT1", "yf": "AT1.DE", "name": "Aroundtown"},
            {"ticker": "AUMOV", "yf": "AUMOV.DE", "name": "AUMOVIO"},
            {"ticker": "NDA", "yf": "NDA.DE", "name": "Aurubis"},
            {"ticker": "AUTO1", "yf": "AUTO1.DE", "name": "AUTO1 Group"},
            {"ticker": "BC8", "yf": "BC8.DE", "name": "Bechtle"},
            {"ticker": "GBF", "yf": "GBF.DE", "name": "Bilfinger"},
            {"ticker": "EVT", "yf": "EVT.DE", "name": "Evotec"},
            {"ticker": "DHER", "yf": "DHER.DE", "name": "Delivery Hero"},
            {"ticker": "DEZ", "yf": "DEZ.DE", "name": "DEUTZ"},
            {"ticker": "DWS", "yf": "DWS.DE", "name": "DWS Group"},
            {"ticker": "ELG", "yf": "ELG.DE", "name": "Elmos Semiconductor"},
            {"ticker": "EVK", "yf": "EVK.DE", "name": "Evonik"},
            {"ticker": "FTK", "yf": "FTK.DE", "name": "flatexDEGIRO"},
            {"ticker": "FRA", "yf": "FRA.DE", "name": "Fraport"},
            {"ticker": "FNTN", "yf": "FNTN.DE", "name": "freenet"},
            {"ticker": "FPE3", "yf": "FPE3.DE", "name": "FUCHS"},
            {"ticker": "HLE", "yf": "HLE.DE", "name": "HELLA"},
            {"ticker": "HAG", "yf": "HAG.DE", "name": "HENSOLDT"},
            {"ticker": "BOSS", "yf": "BOSS.DE", "name": "HUGO BOSS"},
            {"ticker": "IOS", "yf": "IOS.DE", "name": "IONOS"},
            {"ticker": "JEN", "yf": "JEN.DE", "name": "JENOPTIK"},
            {"ticker": "SDF", "yf": "SDF.DE", "name": "K+S"},
            {"ticker": "KGX", "yf": "KGX.DE", "name": "KION GROUP"},
            {"ticker": "KBX", "yf": "KBX.DE", "name": "Knorr-Bremse"},
            {"ticker": "KRN", "yf": "KRN.DE", "name": "KRONES"},
            {"ticker": "LXS", "yf": "LXS.DE", "name": "LANXESS"},
            {"ticker": "LEG", "yf": "LEG.DE", "name": "LEG Immobilien"},
            {"ticker": "LHA", "yf": "LHA.DE", "name": "Lufthansa"},
            {"ticker": "NEM", "yf": "NEM.DE", "name": "Nemetschek"},
            {"ticker": "NDA1", "yf": "NDA1.DE", "name": "Nordex"},
            {"ticker": "PAH3", "yf": "PAH3.DE", "name": "Porsche Automobil Holding"},
            {"ticker": "P911", "yf": "P911.DE", "name": "Porsche AG"},
            {"ticker": "PUM", "yf": "PUM.DE", "name": "PUMA"},
            {"ticker": "RAA", "yf": "RAA.DE", "name": "Rational"},
            {"ticker": "RENK", "yf": "RENK.DE", "name": "RENK"},
            {"ticker": "RRTL", "yf": "RRTL.DE", "name": "RTL Group"},
            {"ticker": "SZG", "yf": "SZG.DE", "name": "Salzgitter"},
            {"ticker": "SRT3", "yf": "SRT3.DE", "name": "Sartorius Vz."},
            {"ticker": "SHA", "yf": "SHA.DE", "name": "Schaeffler"},
            {"ticker": "WAF", "yf": "WAF.DE", "name": "Siltronic"},
            {"ticker": "SMHN", "yf": "SMHN.DE", "name": "SUSS MicroTec"},
            {"ticker": "TAG", "yf": "TAG.DE", "name": "TAG Immobilien"},
            {"ticker": "TLX", "yf": "TLX.DE", "name": "Talanx"},
            {"ticker": "TKAM", "yf": "TKAM.DE", "name": "thyssenkrupp"},
            {"ticker": "TKMS", "yf": "TKMS.DE", "name": "TKMS"},
            {"ticker": "8TRA", "yf": "8TRA.DE", "name": "TRATON"},
            {"ticker": "TUI1", "yf": "TUI1.DE", "name": "TUI"},
            {"ticker": "UTDI", "yf": "UTDI.DE", "name": "United Internet"},
            {"ticker": "WCH", "yf": "WCH.DE", "name": "Wacker Chemie"},
        ],
        "TecDAX": [
            {"ticker": "OHB", "yf": "OHB.DE", "name": "OHB"},
            {"ticker": "AIXA", "yf": "AIXA.DE", "name": "AIXTRON"},
            {"ticker": "WAF", "yf": "WAF.DE", "name": "Siltronic"},
            {"ticker": "S92", "yf": "S92.DE", "name": "SMA Solar"},
            {"ticker": "FNTN", "yf": "FNTN.DE", "name": "freenet"},
            {"ticker": "NEM", "yf": "NEM.DE", "name": "Nemetschek"},
            {"ticker": "SRT", "yf": "SRT3.DE", "name": "Sartorius"},
            {"ticker": "EVT", "yf": "EVT.DE", "name": "Evotec"},
            {"ticker": "DRW3", "yf": "DRW3.DE", "name": "Drägerwerk"},
            {"ticker": "TUI1", "yf": "TUI1.DE", "name": "TUI"},
            {"ticker": "UTDI", "yf": "UTDI.DE", "name": "United Internet"},
            {"ticker": "QIA", "yf": "QIA.DE", "name": "Qiagen"},
            {"ticker": "DLG", "yf": "DLG.DE", "name": "Dialog Semiconductor"},
            {"ticker": "BC8", "yf": "BC8.DE", "name": "Bechtle"},
            {"ticker": "SOW", "yf": "SOW.DE", "name": "Software AG"},
        ],
        "EURO STOXX 50": [
            {"ticker": "ASML", "yf": "ASML.AS", "name": "ASML Holding"},
            {"ticker": "SAP", "yf": "SAP.DE", "name": "SAP"},
            {"ticker": "MC", "yf": "MC.PA", "name": "LVMH"},
            {"ticker": "OR", "yf": "OR.PA", "name": "L'Oréal"},
            {"ticker": "TTE", "yf": "TTE.PA", "name": "TotalEnergies"},
            {"ticker": "SU", "yf": "SU.PA", "name": "Schneider Electric"},
            {"ticker": "SAN", "yf": "SAN.MC", "name": "Banco Santander"},
            {"ticker": "SIE", "yf": "SIE.DE", "name": "Siemens"},
            {"ticker": "AIR", "yf": "AIR.PA", "name": "Airbus"},
            {"ticker": "ALV", "yf": "ALV.DE", "name": "Allianz"},
            {"ticker": "IBE", "yf": "IBE.MC", "name": "Iberdrola"},
            {"ticker": "BNP", "yf": "BNP.PA", "name": "BNP Paribas"},
            {"ticker": "ADYEN", "yf": "ADYEN.AS", "name": "Adyen"},
            {"ticker": "DTE", "yf": "DTE.DE", "name": "Deutsche Telekom"},
            {"ticker": "ENEL", "yf": "ENEL.MI", "name": "Enel"},
        ],
        "Euronext 100": [
            {"ticker": "MC", "yf": "MC.PA", "name": "LVMH"},
            {"ticker": "OR", "yf": "OR.PA", "name": "L'Oréal"},
            {"ticker": "TTE", "yf": "TTE.PA", "name": "TotalEnergies"},
            {"ticker": "SU", "yf": "SU.PA", "name": "Schneider Electric"},
            {"ticker": "AIR", "yf": "AIR.PA", "name": "Airbus"},
            {"ticker": "BNP", "yf": "BNP.PA", "name": "BNP Paribas"},
            {"ticker": "SAN", "yf": "SAN.PA", "name": "Sanofi"},
            {"ticker": "ASML", "yf": "ASML.AS", "name": "ASML Holding"},
            {"ticker": "ADYEN", "yf": "ADYEN.AS", "name": "Adyen"},
            {"ticker": "INGA", "yf": "INGA.AS", "name": "ING Group"},
            {"ticker": "PRX", "yf": "PRX.AS", "name": "Prosus"},
            {"ticker": "ABI", "yf": "ABI.BR", "name": "Anheuser-Busch InBev"},
            {"ticker": "UCB", "yf": "UCB.BR", "name": "UCB"},
            {"ticker": "GLE", "yf": "GLE.PA", "name": "Société Générale"},
            {"ticker": "EDP", "yf": "EDP.LS", "name": "EDP - Energias de Portugal"},
        ],
        "CAC 40": [
            {"ticker": "MC", "yf": "MC.PA", "name": "LVMH"},
            {"ticker": "OR", "yf": "OR.PA", "name": "L'Oréal"},
            {"ticker": "TTE", "yf": "TTE.PA", "name": "TotalEnergies"},
            {"ticker": "SU", "yf": "SU.PA", "name": "Schneider Electric"},
            {"ticker": "AIR", "yf": "AIR.PA", "name": "Airbus"},
            {"ticker": "BNP", "yf": "BNP.PA", "name": "BNP Paribas"},
            {"ticker": "SAN", "yf": "SAN.PA", "name": "Sanofi"},
            {"ticker": "GLE", "yf": "GLE.PA", "name": "Société Générale"},
            {"ticker": "AI", "yf": "AI.PA", "name": "Air Liquide"},
            {"ticker": "CS", "yf": "CS.PA", "name": "AXA"},
            {"ticker": "DG", "yf": "DG.PA", "name": "Vinci"},
            {"ticker": "EL", "yf": "EL.PA", "name": "EssilorLuxottica"},
            {"ticker": "KER", "yf": "KER.PA", "name": "Kering"},
            {"ticker": "RMS", "yf": "RMS.PA", "name": "Hermès"},
            {"ticker": "CAP", "yf": "CAP.PA", "name": "Capgemini"},
        ],
        "STOXX Europe 600": [
            {"ticker": "NESN", "yf": "NESN.SW", "name": "Nestlé"},
            {"ticker": "NOVN", "yf": "NOVN.SW", "name": "Novartis"},
            {"ticker": "ROG", "yf": "ROG.SW", "name": "Roche"},
            {"ticker": "ASML", "yf": "ASML.AS", "name": "ASML Holding"},
            {"ticker": "MC", "yf": "MC.PA", "name": "LVMH"},
            {"ticker": "SAP", "yf": "SAP.DE", "name": "SAP"},
            {"ticker": "SHEL", "yf": "SHEL.L", "name": "Shell"},
            {"ticker": "AZN", "yf": "AZN.L", "name": "AstraZeneca"},
            {"ticker": "TTE", "yf": "TTE.PA", "name": "TotalEnergies"},
            {"ticker": "SIE", "yf": "SIE.DE", "name": "Siemens"},
            {"ticker": "HSBA", "yf": "HSBA.L", "name": "HSBC"},
            {"ticker": "UNA", "yf": "UNA.AS", "name": "Unilever"},
            {"ticker": "SAN", "yf": "SAN.MC", "name": "Banco Santander"},
            {"ticker": "ALV", "yf": "ALV.DE", "name": "Allianz"},
            {"ticker": "IBE", "yf": "IBE.MC", "name": "Iberdrola"},
        ],
    },
    "Schweiz": {
        "SMI": [
            {"ticker": "NESN", "yf": "NESN.SW", "name": "Nestlé"},
            {"ticker": "NOVN", "yf": "NOVN.SW", "name": "Novartis"},
            {"ticker": "ROG", "yf": "ROG.SW", "name": "Roche"},
            {"ticker": "UBSG", "yf": "UBSG.SW", "name": "UBS"},
            {"ticker": "ZURN", "yf": "ZURN.SW", "name": "Zurich Insurance"},
            {"ticker": "ABBN", "yf": "ABBN.SW", "name": "ABB"},
            {"ticker": "CFR", "yf": "CFR.SW", "name": "Richemont"},
            {"ticker": "LONN", "yf": "LONN.SW", "name": "Lonza"},
            {"ticker": "SIKA", "yf": "SIKA.SW", "name": "Sika"},
            {"ticker": "GIVN", "yf": "GIVN.SW", "name": "Givaudan"},
            {"ticker": "SREN", "yf": "SREN.SW", "name": "Swiss Re"},
            {"ticker": "SLHN", "yf": "SLHN.SW", "name": "Swiss Life"},
            {"ticker": "GEBN", "yf": "GEBN.SW", "name": "Geberit"},
            {"ticker": "HOLN", "yf": "HOLN.SW", "name": "Holcim"},
            {"ticker": "ALC", "yf": "ALC.SW", "name": "Alcon"},
        ],
    },
}



UNIVERSE_TICKER_COUNT = _universe_ticker_count()

# Bugfix (2026-09): UNIVERSE["Indizes"] ist eine Sonderkategorie für Index-Ticker
# selbst (SPY, DAX, ...), verschachtelt aber (anders als alle anderen Kategorien)
# nach "USA"/"Europa" statt nach echter Region - "Europa" mischt dabei sogar
# mehrere echte Regionen (EU/UK/Schweiz). find_region_index() interpretierte
# das bisher generisch, dadurch zeigte die Region-Spalte für Index-Ticker
# fälschlich "Indizes" statt der echten Region+Flagge, und "USA"/"Europa"
# tauchten als ungültige Einträge im Index-Auswahlmenü auf. Explizite
# Zuordnung je Ticker, da die Gruppierung selbst nicht eindeutig ist.
INDEX_TICKER_REGION = {
    "SPY": "USA", "QQQ": "USA", "DIA": "USA", "IWM": "USA",
    "DAX": "EU", "ESTX50": "EU", "FTSE": "UK", "SMI": "Schweiz",
}

HIGH_VOL_THRESHOLD = 45.0
CAPITAL = 10_000.0  # fixer Ausgangswert für Tag 1 / einen kompletten Reset






MAX_POSITION_PCT = 3.0  # max 3% des Kapitals pro Trade
REC_CACHE_SEC = 30  # Empfehlung nur alle 30s neu berechnen
REC_MIN_GAP_MIN = 15  # Mindestabstand zwischen Empfehlungen pro Ticker

# Regelbasiertes Stop-Nachziehen (statt "Stop bei jedem Tick hinter dem Kurs herschieben"):
# Ein Swing-Punkt (Higher Low bei LONG / Lower High bei SHORT) gilt erst als bestätigt,
# wenn auf beiden Seiten TRAIL_SWING_LOOKBACK Bars vorliegen (Fraktal-Muster) - der Stop
# wird also nur nachgezogen, wenn seit Kauf eine echte neue Marktstruktur entstanden ist,
# nicht bei jedem einzelnen Kurs-Update.
TRAIL_SWING_LOOKBACK = 3
TRAIL_SWING_BUFFER_PCT = 0.001  # kleiner Sicherheitsabstand unter/über dem Swing-Level
TRAIL_BREAKEVEN_MIN_R = 1.0  # ab diesem Vielfachen des Risikos wird einmalig auf Breakeven gesichert
# Absoluter Pfad statt reinem Dateinamen: ein relativer Pfad wird gegen das
# aktuelle Arbeitsverzeichnis des Prozesses aufgelöst, das sich je nach
# Startart (Icon, Task Scheduler, Dienst, anderes Terminal-cwd) unterscheiden
# kann. Dadurch landete die SQLite-Datei bei jedem Neustart potenziell an
# einem anderen Ort bzw. es wurde stillschweigend eine neue, leere DB
# angelegt - das sah wie Datenverlust (Cash/Positionen/Trades) aus, war aber
# ein Pfadproblem. Fest an den Ordner dieses Skripts gebunden, ist der Pfad
# unabhängig vom Start-cwd immer derselbe.
DB_PATH = str(Path(__file__).resolve().parent / "voltdesk_trades.db")
IMAGE_DIR = Path(__file__).resolve().parent / "images"
LOGO_PATH = IMAGE_DIR / "logo.png"
PRO_LOGO_PATH = IMAGE_DIR / "logo_pro.png"


def _logo_data_uri(pro_mode: bool) -> str:
    """PNG aus images/ als Data-URI für die Sidebar. Fehlt die Datei, leerer String."""
    path = PRO_LOGO_PATH if pro_mode else LOGO_PATH
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ""
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


VERSION = "0.9.35"
def _turso_credentials() -> tuple[Optional[str], Optional[str]]:
    """Turso-URL und DB-Token aus Streamlit Secrets (nicht loggen) — namensunabhängig.

    Vorherige Version prüfte nur eine feste Liste bekannter Namen (TURSO_DATABASE_URL /
    [turso].url / [turso].database_url usw.). Das hatte denselben blinden Fleck wie
    get_finnhub_key(): wurde der "flache" Name INNERHALB der [turso]-Sektion verwendet
    (z.B. [turso] TURSO_DATABASE_URL = "..." statt [turso] url = "..."), fand die Suche
    ihn nicht — Turso galt dann fälschlich als "nicht konfiguriert", die App fiel auf die
    lokale SQLite-Datei zurück, die bei jedem Redeploy verloren geht (Streamlit Cloud hat
    kein persistentes Dateisystem).
    Jetzt: rekursiv über alle Secret-Namen, case-insensitiv nach "turso" + ("url"/"token")
    bzw. "turso" + ("token"/"auth") gesucht — deckt jede sinnvolle Schreibweise ab.
    """
    try:
        url = token = None
        for path, val in _iter_secret_items(st.secrets):
            name_lower = path.lower().replace("-", "_")
            if "turso" not in name_lower:
                continue
            s = _as_secret_str(val)
            if not s:
                continue
            if url is None and ("url" in name_lower or "database" in name_lower):
                url = s
            if token is None and ("token" in name_lower or "auth" in name_lower):
                token = s
    except Exception:
        return None, None
    return (url or None), (token or None)


def uses_turso() -> bool:
    if not HAS_LIBSQL:
        return False
    url, token = _turso_credentials()
    return bool(url and token)


@contextmanager
def get_db_connection():
    """Eine Connection: Turso/libSQL wenn Secrets da sind, sonst lokale SQLite.

    Kein Embedded Replica — Streamlit Cloud würde die Datei beim Redeploy verlieren.
    """
    url, token = _turso_credentials()
    if HAS_LIBSQL and url and token:
        conn = libsql.connect(database=url, auth_token=token)
    else:
        conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _read_sql(sql: str, params=()) -> pd.DataFrame:
    with get_db_connection() as conn:
        try:
            return pd.read_sql_query(sql, conn, params=params)
        except Exception:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            return pd.DataFrame(list(rows), columns=cols)

# Fester Build-Zeitstempel: wird bei jeder inhaltlichen Änderung der Datei manuell
# aktualisiert (Datum/Uhrzeit des letzten Entwicklungsstands, Europe/Berlin).
# Vorher berechnete get_build() datetime.now() bei JEDEM Seitenaufruf live - das zeigte
# also nie "wann zuletzt entwickelt", sondern immer nur die aktuelle Serverzeit an.
BUILD_TIMESTAMP = "2026-09-09 14:15"
FEE_PER_TRADE = 1.0  # € je Kauf oder Verkauf


# Mood-Labels (Anzeige + interne Codes). TRADE_MOOD_CHOICES steht weiter unten
# beim Bestätigungsdialog; die Kurzcodes hier erlauben robuste Tilt-Erkennung
# unabhängig davon, ob ein Fill "😤 Frustrated" oder nur "Frustrated" speichert.
MOOD_OPTIONS = {
    "calm": "😌 Calm",
    "neutral": "🤔 Neutral",
    "anxious": "😟 Anxious",
    "euphoric": "🚀 Euphoric",
    "frustrated": "😤 Frustrated",
    "tired": "😴 Tired",
}
TILT_NEGATIVE_MOODS = ("frustrated", "anxious", "tired")
TILT_LOOKBACK_DEFAULT = 8
TILT_LOSS_RATE_DEFAULT = 60.0
TILT_CONSEC_DEFAULT = 3
TILT_WARN_DEFAULT = 50
TILT_LOCK_DEFAULT = 75














MAX_DAILY_LOSS_PCT = 2.0
MAX_LEVERAGE = 8.0  # absoluter Cap, nur mit Pro Modus erreichbar (s. effective_max_leverage())
DEFAULT_LEVERAGE_CAP = 5.0  # Cap ohne Pro Modus
MAX_TOTAL_EXPOSURE_MULT = 2.0  # Konto-weites Limit: Summe aller Notional-Werte <= 2.0x Kapital
MAX_SCALE_INS = 1  # max. Nachkäufe je Ticker+Seite zusätzlich zur Erstposition
LIVE_SLIPPAGE_PCT = 0.15  # 0,15 % adverses Slippage auf Entry/Exit im Paper
KO_WARN_PCT = 2.0  # Warnung, wenn Kurs so nah an die KO-Schwelle kommt
SESSION_GAP_MINUTES = 90  # Pause zwischen Bars, ab der ein Gap-Open gilt
ORB_WAIT_MINUTES = 30  # keine handelbaren ORB-Setups in den ersten 30 Min nach Venue-Open
# Debug-Panel im Einstellungsdialog (2026-09): auf True setzen, um es wieder
# einzublenden - Code bleibt komplett erhalten, nur per Flag ausgeblendet.
DEBUG_PANEL_ENABLED = False

# Finnhub als NEWS-Quelle vorübergehend hart deaktiviert (2026-09) - offene
# Lizenzfragen zur gewerblichen Nutzung von Finnhub-News in einem kosten-
# pflichtigen Produkt. Der Kurs-Fallback (_finnhub_quote(), separate Frage,
# nur Backup wenn yfinance fehlschlägt) ist davon NICHT betroffen und bleibt
# aktiv. Auf True setzen, sobald die Lizenzfrage geklärt ist.
FINNHUB_NEWS_DISABLED = True

IMPRESSUM_TEXT = """Impressum

Verantwortlich für den Inhalt nach § 5 DDG / § 18 MStV:
Ulrich Richard Hambuch
c/o kaisersoft.ai
Lauffener Str. 3
74226 Nordheim
Kreis Heilbronn
Deutschland

E-Mail: kaisersoft.ai@gmail.com
Web: www.kaisersoft.info

Hinweis:
VoltDesk ist ein Paper-Trading-Desk zu Trainings- und Demonstrationszwecken.
Keine Anlageberatung, keine Aufforderung zum Handel mit Hebelprodukten.
"""




DEFAULT_WATCH = ["AMD", "APP", "MU", "OHB"]
# Modul-weite Flaggen-Zuordnung für Regionen - wird sowohl im Katalog (Sidebar) als auch
# in Watchlist und Stock Ranker verwendet, damit alle drei Stellen konsistent bleiben.
# Anzeige-Reihenfolge der Regionen in der Sidebar (US vor EU vor UK vor CH) -
# unabhängig von der Einfüge-Reihenfolge im UNIVERSE-Dict.
REGION_ORDER = ["USA", "EU", "UK", "Schweiz"]
# Kurzcode für Anzeige (Picker/Ranker/Watchlist) - "USA" wird überall als "US" gezeigt.
REGION_CODE_DISPLAY = {"USA": "US", "UK": "UK", "EU": "EU", "Schweiz": "CH"}


REGION_FLAGS = {
    "USA": "🇺🇸 US",
    "UK": "🇬🇧 UK",
    "EU": "🇪🇺 EU",
    "Schweiz": "🇨🇭 CH",
}
# Nur das Flaggen-Emoji ohne Text - für kompakte Tabellenspalten (Watchlist, Stock Ranker).
REGION_FLAG_ONLY = {"USA": "🇺🇸", "UK": "🇬🇧", "EU": "🇪🇺", "Schweiz": "🇨🇭"}

# Grobe Sektor-Zuordnung für den neuen Sektor-Filter (Watchlist, Stock Picker, Stock
# Ranker). Deckt die geläufigsten/liquidesten Titel im UNIVERSE ab; alles, was hier
# nicht gelistet ist, fällt auf "Sonstige" zurück, statt den Filter zum Absturz zu
# bringen oder den Titel unsichtbar zu machen.
SECTOR_MAP = {
    "SPY": "Index", "QQQ": "Index", "DIA": "Index", "IWM": "Index",
    "DAX": "Index", "ESTX50": "Index", "FTSE": "Index", "SMI": "Index",
    # Technologie
    "NVDA": "Technologie", "AAPL": "Technologie", "MSFT": "Technologie", "GOOGL": "Technologie",
    "META": "Technologie", "AVGO": "Technologie", "ADBE": "Technologie", "CRM": "Technologie",
    "CSCO": "Technologie", "IBM": "Technologie", "INTC": "Technologie", "AMD": "Technologie",
    "APP": "Technologie", "ARM": "Technologie", "MU": "Technologie", "AMAT": "Technologie",
    "SAP": "Technologie", "IFX": "Technologie", "ASML": "Technologie", "ADYEN": "Technologie",
    "FNTN": "Technologie", "TKAM": "Technologie", "NEM": "Technologie", "AIXA": "Technologie",
    "SOW": "Technologie", "S92": "Technologie", "WAF": "Technologie", "QIA": "Technologie",
    # Kommunikation/Streaming
    "NFLX": "Kommunikation", "TMUS": "Kommunikation", "DIS": "Kommunikation", "VOD": "Kommunikation",
    "DTE": "Kommunikation", "UTDI": "Kommunikation",
    # E-Commerce/Konsum zyklisch
    "AMZN": "Konsum zyklisch", "TSLA": "Konsum zyklisch", "HD": "Konsum zyklisch",
    "MCD": "Konsum zyklisch", "NKE": "Konsum zyklisch", "ZAL": "Konsum zyklisch",
    "PRX": "Konsum zyklisch", "DHER": "Konsum zyklisch", "AUTO1": "Konsum zyklisch",
    "BMW": "Konsum zyklisch", "MBG": "Konsum zyklisch", "P911": "Konsum zyklisch",
    "PAH3": "Konsum zyklisch", "RAA": "Konsum zyklisch", "PUM": "Konsum zyklisch",
    "ADS": "Konsum zyklisch", "BOSS": "Konsum zyklisch", "RMS": "Konsum zyklisch",
    "KER": "Konsum zyklisch",
    # Konsum defensiv
    "KO": "Konsum defensiv", "PG": "Konsum defensiv", "WMT": "Konsum defensiv",
    "PEP": "Konsum defensiv", "COST": "Konsum defensiv", "ULVR": "Konsum defensiv",
    "DGE": "Konsum defensiv", "NESN": "Konsum defensiv", "OR": "Konsum defensiv",
    "BATS": "Konsum defensiv",
    # Finanzen
    "JPM": "Finanzen", "V": "Finanzen", "MA": "Finanzen", "AXP": "Finanzen", "GS": "Finanzen",
    "TRV": "Finanzen", "HSBA": "Finanzen", "LLOY": "Finanzen", "BARC": "Finanzen",
    "ALV": "Finanzen", "DWS": "Finanzen", "BNP": "Finanzen", "GLE": "Finanzen",
    "SAN": "Finanzen", "INGA": "Finanzen", "UBSG": "Finanzen", "ZURN": "Finanzen",
    "SREN": "Finanzen", "SLHN": "Finanzen",
    # Gesundheit
    "UNH": "Gesundheit", "MRK": "Gesundheit", "LLY": "Gesundheit", "JNJ": "Gesundheit",
    "AMGN": "Gesundheit", "AZN": "Gesundheit", "GSK": "Gesundheit", "FPE3": "Gesundheit",
    "SHL": "Gesundheit", "SRT3": "Gesundheit", "SRT": "Gesundheit", "NOVN": "Gesundheit",
    "ROG": "Gesundheit", "ALC": "Gesundheit", "LONN": "Gesundheit",
    # Energie
    "XOM": "Energie", "CVX": "Energie", "SHEL": "Energie", "BP": "Energie", "TTE": "Energie",
    "RWE": "Energie", "ENEL": "Energie", "IBE": "Energie", "EDP": "Energie", "NG": "Energie",
    # Industrie
    "BA": "Industrie", "CAT": "Industrie", "HON": "Industrie", "MMM": "Industrie",
    "SIE": "Industrie", "AIR": "Industrie", "RHM": "Industrie", "HEI": "Industrie",
    "KGX": "Industrie", "RENK": "Industrie", "TKMS": "Industrie", "SU": "Industrie",
    "DG": "Industrie", "AI": "Industrie", "CAP": "Industrie", "ABBN": "Industrie",
    "SIKA": "Industrie", "GEBN": "Industrie", "HOLN": "Industrie",
    # Grundstoffe/Rohstoffe
    "RIO": "Grundstoffe", "BAS": "Grundstoffe", "LXS": "Grundstoffe", "GIVN": "Grundstoffe",
    # Immobilien/Sonstiges Konsum
    "TAG": "Immobilien", "LEG": "Immobilien",
    # Versorger/Telecom Reste
    "VOW3": "Konsum zyklisch",
}




# Sektor-ETFs (SPDR Select Sector) als Referenz für die Sektor-Konfirmation. Bewusst
# als reale, liquide Proxys gewählt (analog zu INDEX_BENCH) statt einer eigenen
# Peer-Aggregation aus der Watchlist, die je nach Nutzer sehr klein/verzerrt wäre.
SECTOR_BENCH = {
    "Technologie": "XLK",
    "Kommunikation": "XLC",
    "Konsum zyklisch": "XLY",
    "Konsum defensiv": "XLP",
    "Finanzen": "XLF",
    "Gesundheit": "XLV",
    "Energie": "XLE",
    "Industrie": "XLI",
    "Grundstoffe": "XLB",
    "Immobilien": "XLRE",
}






def sector_overlay_series(intra: pd.DataFrame, sector_symbol: str) -> Optional[pd.Series]:
    """%-Veränderung des Sektor-ETF seit dem ersten Balken von `intra`, ausgerichtet
    auf denselben Zeitindex ('Option B', 2026-09-Feature: Sektor-Linie im Chart).
    Sektor-ETFs handeln fast immer zu US-Zeiten, der Fokus-Titel kann Xetra sein -
    die Zeitreihen matchen NICHT 1:1 auf denselben Timestamp, daher merge_asof
    (nächster vorheriger Sektor-Balken) statt exaktem Reindex."""
    if intra is None or intra.empty:
        return None
    try:
        sector_df = fetch_intraday(sector_symbol)
    except Exception:
        return None
    if sector_df is None or sector_df.empty or "Close" not in sector_df.columns:
        return None
    try:
        left = pd.DataFrame({"t": intra.index}).sort_values("t")
        right = sector_df[["Close"]].reset_index()
        right.columns = ["t", "sector_close"]
        right = right.sort_values("t")
        merged = pd.merge_asof(left, right, on="t", direction="backward")
        aligned = pd.Series(merged["sector_close"].values, index=intra.index)
    except Exception:
        return None
    valid = aligned.dropna()
    if valid.empty:
        return None
    base = float(valid.iloc[0])
    if not base:
        return None
    return (aligned / base - 1.0) * 100.0

NEWS_SOURCE_DEFS = {
    "yahoo": {"label": "Yahoo Finance", "kind": "yahoo", "default": True},
    "finnhub": {"label": "Finnhub", "kind": "finnhub", "default": False},
    "finanzen": {
        "label": "finanzen.net",
        "kind": "rss",
        "default": True,
        "url": "https://www.finanzen.net/rss/news",
    },
    "onvista": {
        "label": "Onvista",
        "kind": "rss",
        "default": True,
        "url": "https://news.onvista.de/rss/woche",
    },
    "handelsblatt": {
        "label": "Handelsblatt Finanzen",
        "kind": "rss",
        "default": False,
        "url": "https://www.handelsblatt.com/contentexport/feed/finanzen",
    },
    "aktionaer": {
        "label": "DER AKTIONÄR",
        "kind": "rss",
        "default": True,
        "url": "https://www.deraktionaer.de/aktionaer-news.rss",
    },
    "boerse_ffm": {
        "label": "Börse Frankfurt",
        "kind": "rss",
        "default": False,
        "url": "https://api.boerse-frankfurt.de/v1/feeds/news.rss",
    },
    "adhoc": {
        "label": "Ad-hoc / EQS",
        "kind": "rss",
        "default": True,
        "url": "https://www.ad-hoc-news.de/rss/meldungen",
        "force_tags": ("adhoc",),
    },
}

NEWS_UA = "VoltDesk/0.9 (daytrading desk; news aggregator)"



# Stammdaten-Katalog. KO/Hebel werden am Live-Kurs neu gerechnet.
# Quelle: Onvista-Stichtag wo WKN gesetzt; übrige = Desk-Katalog.
PRODUCT_CATALOG = [
    {"ticker": "AMD", "wkn": "JE673K", "isin": "DE000JE673K9", "issuer": "J.P. Morgan", "side": "LONG", "typ": "Open-End Turbo", "ko": 304.703, "spread_pct": 0.45, "source": "Onvista"},
    {"ticker": "AMD", "wkn": "HM5XZ9", "isin": "", "issuer": "HSBC", "side": "LONG", "typ": "Open-End Turbo", "ko": None, "ko_pct": 18.0, "spread_pct": 4.55, "source": "Onvista"},
    {"ticker": "OHB", "wkn": "HM99PF", "isin": "", "issuer": "HSBC", "side": "LONG", "typ": "Open-End Turbo", "ko": 237.51, "spread_pct": 7.69, "source": "Onvista"},
    {"ticker": "OHB", "wkn": "HM99PR", "isin": "", "issuer": "HSBC", "side": "LONG", "typ": "Open-End Turbo", "ko": 240.41, "spread_pct": 8.98, "source": "Onvista"},
    {"ticker": "OHB", "wkn": "HM9BHV", "isin": "", "issuer": "HSBC", "side": "LONG", "typ": "Open-End Turbo", "ko": 243.37, "spread_pct": 14.29, "source": "Onvista"},
]

SPLASH_CSS = """
<style>
.stApp { background: radial-gradient(circle at 50% 30%, #12203a 0%, #070b12 58%) !important; }
.splash-wrap { text-align: center; padding: 8vh 1rem 2rem; }
.splash-mark {
  width: 78px; height: 78px; margin: 0 auto 18px; border-radius: 22px;
  background: linear-gradient(145deg, #3dd68c, #5aa8ff);
  box-shadow: 0 0 48px rgba(61,214,140,.35);
  display: flex; align-items: center; justify-content: center;
  font-size: 38px;
}
.splash-title { font-size: 3rem; letter-spacing: .08em; margin: 0; }
.splash-sub { color: #8b9bb2; margin-top: .4rem; }
</style>
"""

APP_CSS = """
<style>
.stApp { background: #05070c !important; }
div.block-container { padding-top: 0.5rem !important; position: relative; z-index: 1; }
h1 { margin-top: 0 !important; font-size: 1.85rem !important; }
.rec-done { opacity: 0.45; }
[data-testid="stSidebar"] { background-color: #000000 !important; }
[data-testid="stSidebar"] > div { background-color: #000000 !important; }
.chart-bg {
  position: fixed; inset: 0; z-index: 0; pointer-events: none; overflow: hidden;
  opacity: 0.20;
  display: flex;
  align-items: center;
  justify-content: center;
}
.candle { animation: drift 22s linear infinite; }
@keyframes drift {
  from { transform: translateX(0); }
  to { transform: translateX(-14%); }
}
</style>
<div class="chart-bg" aria-hidden="true">
  <svg viewBox="0 0 1200 400" preserveAspectRatio="none">
    <g class="candle" fill="none" stroke-width="2">
      <path stroke="#3dd68c" d="M20 220 L80 180 L140 200 L200 150 L260 170 L320 120 L380 140 L440 90"/>
      <path stroke="#5aa8ff" d="M20 260 L90 240 L150 250 L210 210 L280 230 L340 190 L410 205 L480 160"/>
      <path stroke="#ff5d6c" d="M500 80 L560 140 L620 110 L680 190 L740 160 L800 230 L860 200 L920 280"/>
      <rect x="70" y="160" width="10" height="50" fill="#3dd68c"/>
      <rect x="130" y="175" width="10" height="40" fill="#ff5d6c"/>
      <rect x="190" y="130" width="10" height="55" fill="#3dd68c"/>
      <rect x="250" y="145" width="10" height="45" fill="#3dd68c"/>
      <rect x="310" y="100" width="10" height="60" fill="#ff5d6c"/>
      <rect x="370" y="120" width="10" height="48" fill="#3dd68c"/>
      <rect x="430" y="80" width="10" height="55" fill="#3dd68c"/>
      <rect x="540" y="120" width="10" height="50" fill="#ff5d6c"/>
      <rect x="600" y="100" width="10" height="45" fill="#3dd68c"/>
      <rect x="660" y="160" width="10" height="55" fill="#ff5d6c"/>
      <rect x="720" y="140" width="10" height="50" fill="#3dd68c"/>
      <rect x="780" y="200" width="10" height="55" fill="#ff5d6c"/>
    </g>
  </svg>
</div>
"""




_UNIVERSE_INDEX: Optional[dict] = None
_UNIVERSE_REGION_INDEX: Optional[dict] = None






def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, ticker TEXT NOT NULL,
            wkn TEXT, side TEXT, action TEXT NOT NULL, entry REAL, exit_px REAL,
            amount REAL, leverage REAL, pnl REAL, fees REAL, reason TEXT, timestamp TEXT NOT NULL,
            setup_label TEXT, mood TEXT, setup_quality TEXT)''')
        for _col in ("setup_label", "mood", "setup_quality"):
            try:
                c.execute(f"ALTER TABLE trades ADD COLUMN {_col} TEXT")
            except Exception:
                pass
        c.execute('''CREATE TABLE IF NOT EXISTS week_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, week_start TEXT NOT NULL,
            week_end TEXT NOT NULL, report_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS month_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, year_month TEXT NOT NULL,
            report_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS year_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, year TEXT NOT NULL,
            report_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS day_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL)''')
        # Live-Paper-Equity-Historie (V1.0-Lücke): bisher gab es nur die Backtest-Equity-
        # Curve, keine dauerhaft historisierte Live-Equity - ohne diese Tabelle lässt
        # sich weder eine echte Equity-Kurve noch Drawdown/Recovery Time über die
        # laufende Paper-Handelszeit fürs Live-Konto anzeigen.
        c.execute('''CREATE TABLE IF NOT EXISTS equity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            equity REAL NOT NULL,
            cash REAL,
            open_pnl REAL,
            realized_pnl REAL,
            drawdown REAL,
            drawdown_pct REAL)''')

        # Trial/Abo (Stripe) - user_id ist hier von Anfang an PRIMARY KEY (keine
        # Altlast wie bei app_state), da die Tabelle erst mit Multi-User entsteht.
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
            user_id TEXT PRIMARY KEY,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            status TEXT,
            current_period_end TEXT,
            trial_start TEXT NOT NULL,
            updated_at TEXT NOT NULL)''')

        # OAuth-State-Nonces (CSRF-Schutz für den Auth0-Login). Bewusst
        # serverseitig in der DB statt in st.session_state: Login läuft über
        # link_button in einem NEUEN Tab, der beim Auth0-Rücksprung fast nie
        # dieselbe Session ist wie der Tab, der den Login-Link erzeugt hat -
        # ein session-gebundener Nonce wäre beim Callback also nie auffindbar.
        c.execute('''CREATE TABLE IF NOT EXISTS oauth_nonces (
            nonce TEXT PRIMARY KEY,
            created_at TEXT NOT NULL)''')

        # Selektives Fehlerprotokoll (nur für ausgewählte, sonst stille
        # Hintergrundpfade - Auth0-Callback, Stripe-Sync/Reconcile,
        # DB-Migrationen; s. log_error()). Gäste-Fehler bewusst nicht
        # geloggt (user_id NOT NULL) - Gast-Modus hat konsequent keine
        # Persistenz. 30 Tage Aufbewahrung, danach automatisch aufgeräumt.
        c.execute('''CREATE TABLE IF NOT EXISTS error_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            context TEXT NOT NULL,
            message TEXT NOT NULL)''')

        # CRITICAL TRADE EVENT (2026-09): DB-persistiert (nicht nur session_state),
        # da Cold-Starts bei Streamlit Community Cloud häufig sind - eine nur im
        # Session-State gehaltene Meldung würde einen Cold-Start nicht überleben,
        # genau dann aber am dringendsten gebraucht. Deckt Ereignisse ab, bei denen
        # der Nutzer eine entsprechende Aktion im ECHTEN Broker nachziehen muss,
        # damit Desk und Broker-Portfolio nicht auseinanderlaufen (Stop/Take/KO/
        # Auto-Close/Daily-Loss-Lock/Kill-Switch). acknowledged_at bleibt bewusst
        # UPDATE statt DELETE beim Bestätigen - kostenlose Historie für später,
        # falls im Betrieb mal Fehler nachvollzogen werden müssen (aktuell nicht
        # aktiv genutzt, nur als Nebenprodukt vorhanden).
        c.execute('''CREATE TABLE IF NOT EXISTS pending_critical_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT,
            details_json TEXT NOT NULL,
            requires_broker_action INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            acknowledged_at TEXT)''')

        # --- Multi-User-Migration ---
        # Auth0 beantwortet "wer ist der Nutzer", die DB muss "welche Daten
        # gehören ihm" beantworten. Jede Tabelle bekommt dafür eine user_id-
        # Spalte (Auth0 'sub'); bestehende Zeilen bleiben zunächst user_id=NULL
        # und werden beim nächsten Login automatisch geclaimt (siehe
        # _claim_legacy_data). day_reports hatte bisher ein UNIQUE(date) -
        # das muss auf UNIQUE(date, user_id) erweitert werden, was SQLite per
        # ALTER TABLE nicht erlaubt, daher Neuaufbau der Tabelle.
        for _table in ("trades", "week_reports", "month_reports", "year_reports",
                       "equity_history", "app_state"):
            try:
                c.execute(f"ALTER TABLE {_table} ADD COLUMN user_id TEXT")
            except Exception:
                pass  # Spalte existiert schon

        _day_report_cols = [r[1] for r in c.execute("PRAGMA table_info(day_reports)").fetchall()]
        if "user_id" not in _day_report_cols:
            c.execute("ALTER TABLE day_reports RENAME TO day_reports_legacy")
            c.execute('''CREATE TABLE day_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                user_id TEXT,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(date, user_id))''')
            c.execute('''INSERT INTO day_reports (id, date, user_id, report_json, created_at)
                SELECT id, date, NULL, report_json, created_at FROM day_reports_legacy''')
            c.execute("DROP TABLE day_reports_legacy")

        # --- Zielzustand: user_id NOT NULL ---
        # Läuft bei jedem Start, rührt eine Tabelle aber nur an, wenn dort
        # aktuell KEINE Zeile mehr mit user_id IS NULL existiert - sonst würde
        # ein Rebuild noch nicht per Login geclaimte Alt-Daten verwerfen.
        # Sobald der letzte Nutzer einmal eingeloggt war, greift das beim
        # nächsten Start automatisch, ganz ohne manuellen Eingriff.
        _NN_TABLES = {
            "trades": (
                '''CREATE TABLE trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, ticker TEXT NOT NULL,
                    wkn TEXT, side TEXT, action TEXT NOT NULL, entry REAL, exit_px REAL,
                    amount REAL, leverage REAL, pnl REAL, fees REAL, reason TEXT, timestamp TEXT NOT NULL,
                    setup_label TEXT, mood TEXT, setup_quality TEXT, user_id TEXT NOT NULL)''',
                "id, date, ticker, wkn, side, action, entry, exit_px, amount, leverage, "
                "pnl, fees, reason, timestamp, setup_label, mood, setup_quality, user_id",
            ),
            "week_reports": (
                '''CREATE TABLE week_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, week_start TEXT NOT NULL,
                    week_end TEXT NOT NULL, report_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    user_id TEXT NOT NULL)''',
                "id, week_start, week_end, report_json, created_at, user_id",
            ),
            "month_reports": (
                '''CREATE TABLE month_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, year_month TEXT NOT NULL,
                    report_json TEXT NOT NULL, created_at TEXT NOT NULL, user_id TEXT NOT NULL)''',
                "id, year_month, report_json, created_at, user_id",
            ),
            "year_reports": (
                '''CREATE TABLE year_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, year TEXT NOT NULL,
                    report_json TEXT NOT NULL, created_at TEXT NOT NULL, user_id TEXT NOT NULL)''',
                "id, year, report_json, created_at, user_id",
            ),
            "day_reports": (
                '''CREATE TABLE day_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(date, user_id))''',
                "id, date, user_id, report_json, created_at",
            ),
            "equity_history": (
                '''CREATE TABLE equity_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    equity REAL NOT NULL,
                    cash REAL,
                    open_pnl REAL,
                    realized_pnl REAL,
                    drawdown REAL,
                    drawdown_pct REAL,
                    user_id TEXT NOT NULL)''',
                "id, timestamp, equity, cash, open_pnl, realized_pnl, drawdown, drawdown_pct, user_id",
            ),
        }
        for _table, (_create_sql, _cols) in _NN_TABLES.items():
            try:
                _info = c.execute(f"PRAGMA table_info({_table})").fetchall()
                _uid_col = next((r for r in _info if r[1] == "user_id"), None)
                if _uid_col is None or _uid_col[3] == 1:
                    continue  # Spalte fehlt (sollte nicht passieren) oder ist bereits NOT NULL
                _orphans = c.execute(f"SELECT COUNT(*) FROM {_table} WHERE user_id IS NULL").fetchone()[0]
                if _orphans:
                    continue  # noch nicht sicher - erneuter Versuch beim nächsten Start
                c.execute(f"ALTER TABLE {_table} RENAME TO {_table}_pre_nn")
                c.execute(_create_sql)
                c.execute(f"INSERT INTO {_table} ({_cols}) SELECT {_cols} FROM {_table}_pre_nn")
                c.execute(f"DROP TABLE {_table}_pre_nn")
            except Exception:
                pass  # Diese Migration darf den App-Start nie zum Absturz bringen
    _maybe_migrate_local_sqlite_to_turso()


def _claim_legacy_data(user_id: str):
    """Ordnet verwaiste Datensätze aus der Zeit vor Multi-User (user_id IS NULL)
    dem gerade eingeloggten Nutzer zu. Läuft bei jedem Login, ist aber
    idempotent: sobald einmal geclaimt, bleibt für spätere Logins (auch von
    anderen Nutzern) nichts mehr mit user_id IS NULL übrig."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            for _table in ("trades", "week_reports", "month_reports", "year_reports",
                           "equity_history"):
                try:
                    c.execute(f"UPDATE {_table} SET user_id = ? WHERE user_id IS NULL", (user_id,))
                except Exception as e:
                    log_error("claim_legacy_data", f"table={_table} user={user_id}: {e}", user_id=user_id)
            # day_reports hat UNIQUE(date, user_id) - ein einzelnes großes UPDATE
            # würde bei SQLite komplett aborten (auch für alle unkritischen
            # Zeilen), falls nur EIN Datum mit einem bereits vorhandenen
            # (date, user_id) kollidiert. Deshalb zeilenweise, damit ein
            # einzelner Konflikt nicht alle anderen Tage blockiert.
            try:
                _legacy_day_rows = c.execute(
                    "SELECT id FROM day_reports WHERE user_id IS NULL"
                ).fetchall()
                for (_row_id,) in _legacy_day_rows:
                    try:
                        c.execute("UPDATE day_reports SET user_id = ? WHERE id = ?", (user_id, _row_id))
                    except Exception:
                        pass  # Erwartbarer (date, user_id)-Konflikt - kein Logging-würdiger Fehler
            except Exception as e:
                log_error("claim_legacy_data", f"table=day_reports user={user_id}: {e}", user_id=user_id)
            # app_state.key ist PRIMARY KEY - ein simples "UPDATE ... SET key=..."
            # schlägt still fehl (PK-Konflikt), falls unter dem Ziel-Key
            # (z.B. durch einen früheren Testlauf ohne Claim) bereits ein
            # leerer Datensatz existiert. Deshalb explizit per INSERT OR
            # REPLACE mergen und den Legacy-Datensatz erst danach löschen -
            # die historischen Daten haben dabei immer Vorrang vor einem
            # eventuell schon vorhandenen (leeren) Datensatz unter dem neuen Key.
            try:
                legacy_row = c.execute(
                    "SELECT value_json, updated_at FROM app_state WHERE key = 'session'"
                ).fetchone()
                if legacy_row:
                    legacy_value, legacy_updated = legacy_row
                    c.execute(
                        "INSERT OR REPLACE INTO app_state (key, value_json, updated_at, user_id) "
                        "VALUES (?, ?, ?, ?)",
                        (f"session:{user_id}", legacy_value, legacy_updated, user_id),
                    )
                    c.execute("DELETE FROM app_state WHERE key = 'session'")
            except Exception as e:
                log_error("claim_legacy_data", f"table=app_state user={user_id}: {e}", user_id=user_id)
    except Exception as e:
        log_error("claim_legacy_data", f"outer user={user_id}: {e}", user_id=user_id)


def _table_count(conn, table: str) -> int:
    try:
        row = conn.cursor().execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _maybe_migrate_local_sqlite_to_turso():
    """Einmalig lokale voltdesk_trades.db nach Turso kopieren, wenn Turso leer ist."""
    if not uses_turso() or not Path(DB_PATH).is_file():
        return
    try:
        with get_db_connection() as remote:
            if (
                _table_count(remote, "trades")
                + _table_count(remote, "app_state")
                + _table_count(remote, "day_reports")
                + _table_count(remote, "week_reports")
                + _table_count(remote, "month_reports")
            ) > 0:
                return
        local = sqlite3.connect(DB_PATH)
        try:
            with get_db_connection() as remote:
                rc = remote.cursor()
                for table in ("trades", "week_reports", "month_reports", "app_state", "day_reports"):
                    rows = local.execute(f"SELECT * FROM {table}").fetchall()
                    if not rows:
                        continue
                    cols = [d[0] for d in local.execute(f"PRAGMA table_info({table})").fetchall()]
                    placeholders = ",".join("?" * len(cols))
                    col_sql = ",".join(cols)
                    for row in rows:
                        rc.execute(
                            f"INSERT OR IGNORE INTO {table} ({col_sql}) VALUES ({placeholders})",
                            row,
                        )
        finally:
            local.close()
    except Exception:
        return


def save_trade_to_db(ticker, wkn, side, action, entry, exit_px, amount, leverage, pnl, fees, reason,
                     setup_label=None, mood=None, setup_quality=None):
    user_id = current_user_id()
    if not user_id:
        return  # Gast-Modus: keine Persistenz
    now = datetime.now(TZ_BERLIN)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''INSERT INTO trades (date, ticker, wkn, side, action, entry, exit_px,
            amount, leverage, pnl, fees, reason, timestamp, setup_label, mood, setup_quality, user_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (now.strftime("%Y-%m-%d"), ticker, wkn, side, action, entry, exit_px,
             amount, leverage, pnl, fees, reason, now.isoformat(),
             setup_label, mood, setup_quality, user_id))
    # Caching-Audit (2026-09): get_trades_for_*() cachen jetzt (vorher ungecacht,
    # DB-Query bei jedem Rerun der Report-Ansicht) - damit ein gerade gespeicherter
    # Trade trotzdem sofort im Journal auftaucht statt bis zum TTL-Ablauf zu
    # "verschwinden", hier aktiv leeren statt nur auf TTL zu vertrauen.
    for fn in (get_trades_for_week, get_trades_for_month, get_trades_for_year):
        if hasattr(fn, "clear"):
            fn.clear()


@st.cache_data(ttl=30, show_spinner=False)  # Caching-Audit (2026-09) - ROLLBACK: diese
# Zeile entfernen und die drei Cache-Clear-Aufrufe in save_trade_to_db() oben
# wieder rauslöschen, dann ist der alte (ungecachte) Zustand wiederhergestellt.
def get_trades_for_week(start_date, end_date, user_id=None):
    # SICHERHEITS-FIX (2026-09): user_id MUSS als Argument hereinkommen - dadurch
    # landet sie im st.cache_data-Schluessel. Vorher las die Funktion current_user_id()
    # selbst, der Session-Kontext ging aber NICHT in den Cache-Key ein: Nutzer B bekam
    # bei denselben Argumenten die gecachten Daten von Nutzer A (Cross-User-
    # Datenleck - auf Streamlit Cloud teilen sich alle Nutzer einen Prozess/Cache).

    if not user_id:
        return pd.DataFrame()
    return _read_sql(
        "SELECT * FROM trades WHERE date >= ? AND date <= ? AND user_id = ? ORDER BY timestamp",
        (start_date, end_date, user_id),
    )


@st.cache_data(ttl=30, show_spinner=False)  # ROLLBACK: s. Kommentar bei get_trades_for_week()
def get_trades_for_month(year, month, user_id=None):
    # SICHERHEITS-FIX (2026-09): user_id MUSS als Argument hereinkommen - dadurch
    # landet sie im st.cache_data-Schluessel. Vorher las die Funktion current_user_id()
    # selbst, der Session-Kontext ging aber NICHT in den Cache-Key ein: Nutzer B bekam
    # bei denselben Argumenten die gecachten Daten von Nutzer A (Cross-User-
    # Datenleck - auf Streamlit Cloud teilen sich alle Nutzer einen Prozess/Cache).

    if not user_id:
        return pd.DataFrame()
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year+1 if month==12 else year:04d}-{1 if month==12 else month+1:02d}-01"
    return _read_sql(
        "SELECT * FROM trades WHERE date >= ? AND date < ? AND user_id = ? ORDER BY timestamp",
        (start, end, user_id),
    )


@st.cache_data(ttl=30, show_spinner=False)  # ROLLBACK: s. Kommentar bei get_trades_for_week()
def get_trades_for_year(year, user_id=None):
    # SICHERHEITS-FIX (2026-09): user_id MUSS als Argument hereinkommen - dadurch
    # landet sie im st.cache_data-Schluessel. Vorher las die Funktion current_user_id()
    # selbst, der Session-Kontext ging aber NICHT in den Cache-Key ein: Nutzer B bekam
    # bei denselben Argumenten die gecachten Daten von Nutzer A (Cross-User-
    # Datenleck - auf Streamlit Cloud teilen sich alle Nutzer einen Prozess/Cache).

    if not user_id:
        return pd.DataFrame()
    start = f"{year:04d}-01-01"
    end = f"{year+1:04d}-01-01"
    return _read_sql(
        "SELECT * FROM trades WHERE date >= ? AND date < ? AND user_id = ? ORDER BY timestamp",
        (start, end, user_id),
    )


def save_year_report(year, report):
    user_id = current_user_id()
    if not user_id:
        return
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO year_reports (year, report_json, created_at, user_id) VALUES (?,?,?,?)",
                  (year, json.dumps(report), datetime.now(TZ_BERLIN).isoformat(), user_id))


def load_year_report(year):
    user_id = current_user_id()
    if not user_id:
        return None
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT report_json FROM year_reports WHERE year=? AND user_id=? ORDER BY id DESC LIMIT 1", (year, user_id))
        row = c.fetchone()
    return json.loads(row[0]) if row else None


def save_week_report(week_start, week_end, report):
    user_id = current_user_id()
    if not user_id:
        return
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO week_reports (week_start, week_end, report_json, created_at, user_id) VALUES (?,?,?,?,?)",
                  (week_start, week_end, json.dumps(report), datetime.now(TZ_BERLIN).isoformat(), user_id))


def load_week_report(week_start, week_end):
    user_id = current_user_id()
    if not user_id:
        return None
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT report_json FROM week_reports WHERE week_start=? AND week_end=? AND user_id=? ORDER BY id DESC LIMIT 1",
            (week_start, week_end, user_id))
        row = c.fetchone()
    return json.loads(row[0]) if row else None


def save_month_report(year_month, report):
    user_id = current_user_id()
    if not user_id:
        return
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO month_reports (year_month, report_json, created_at, user_id) VALUES (?,?,?,?)",
                  (year_month, json.dumps(report), datetime.now(TZ_BERLIN).isoformat(), user_id))


def load_month_report(year_month):
    user_id = current_user_id()
    if not user_id:
        return None
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT report_json FROM month_reports WHERE year_month=? AND user_id=? ORDER BY id DESC LIMIT 1",
                  (year_month, user_id))
        row = c.fetchone()
    return json.loads(row[0]) if row else None


def save_day_report(report: dict):
    """Save a day report to DB."""
    user_id = current_user_id()
    if not user_id:
        return  # Gast-Modus: keine Persistenz
    now = datetime.now(TZ_BERLIN)
    # Nutzt report["date"] falls gesetzt (z.B. beim automatischen Sichern eines Vortags-
    # Reports bei Tageswechsel), sonst wie bisher das heutige Datum. Vorher wurde hier
    # IMMER now.strftime(...) verwendet, unabhängig vom tatsächlichen Datum des Reports.
    date_str = report.get("date") or now.strftime("%Y-%m-%d")
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO day_reports (date, user_id, report_json, created_at) VALUES (?, ?, ?, ?)",
            (date_str, user_id, json.dumps(report), now.isoformat())
        )
    # Caching-Audit (2026-09): s. Kommentar bei save_trade_to_db() - gleicher Grund.
    if hasattr(get_day_reports_for_week, "clear"):
        get_day_reports_for_week.clear()


@st.cache_data(ttl=30, show_spinner=False)  # Caching-Audit (2026-09) - ROLLBACK: diese
# Zeile + den Cache-Clear-Aufruf in save_day_report() oben entfernen, dann alter
# (ungecachter) Zustand wiederhergestellt.
def get_day_reports_for_week(start_date: str, end_date: str, user_id: str = None) -> list:
    """Fetch saved day reports for a week."""
    # SICHERHEITS-FIX (2026-09): user_id MUSS als Argument hereinkommen - dadurch
    # landet sie im st.cache_data-Schluessel. Vorher las die Funktion current_user_id()
    # selbst, der Session-Kontext ging aber NICHT in den Cache-Key ein: Nutzer B bekam
    # bei denselben Argumenten die gecachten Daten von Nutzer A (Cross-User-
    # Datenleck - auf Streamlit Cloud teilen sich alle Nutzer einen Prozess/Cache).

    if not user_id:
        return []
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT date, report_json FROM day_reports WHERE date >= ? AND date <= ? AND user_id = ? ORDER BY date",
            (start_date, end_date, user_id)
        )
        rows = c.fetchall()
    return [{"date": r[0], **json.loads(r[1])} for r in rows]


def save_app_state():
    user_id = current_user_id()
    if not user_id:
        return  # Gast-Modus: keine Persistenz - Session lebt nur im Browser-Tab
    now = datetime.now(TZ_BERLIN).isoformat()
    # Cash ergibt sich aus CAPITAL + closed_pnl - invested - fees; gespeichert
    # werden die Rohwerte plus GUI-Settings, damit Cloud-Restarts den Desk erhalten.
    state = {
        "closed_pnl": float(st.session_state.get("closed_pnl", 0.0)),
        "fees_paid": float(st.session_state.get("fees_paid", 0.0)),
        "positions": st.session_state.get("positions", []),
        "watchlist": st.session_state.get("watchlist", []),
        "fills": st.session_state.get("fills", []),
        "killed": bool(st.session_state.get("killed", False)),
        "risk_lock": bool(st.session_state.get("risk_lock", False)),
        "day_report": st.session_state.get("day_report"),
        "day_started": st.session_state.get("day_started"),
        "taken_recs": st.session_state.get("taken_recs", []),
        "focus": st.session_state.get("focus"),
        "region": st.session_state.get("region", "USA"),
        "index": st.session_state.get("index", "Nasdaq-100"),
        "high_vol_only": st.session_state.get("high_vol_only", False),
        "high_vol_threshold": float(st.session_state.get("high_vol_threshold", HIGH_VOL_THRESHOLD)),
        "mode_override": st.session_state.get("mode_override", "Auto"),
        "pos_limit_enabled": st.session_state.get("pos_limit_enabled", True),
        "pos_limit_pct": st.session_state.get("pos_limit_pct", MAX_POSITION_PCT),
        "auto_flatten": st.session_state.get("auto_flatten", True),
        "flatten_buffer_min": st.session_state.get("flatten_buffer_min", 60),
        "refresh_sec": st.session_state.get("refresh_sec", 300),
        "rec_history": st.session_state.get("rec_history", []),
        # entered / splash_done bewusst nicht speichern — Splash bei jedem
        # App-/Browser-Session-Neustart (lange Ladezeit überbrücken).
        "ticket_side": st.session_state.get("ticket_side", "LONG"),
        "ticket_leverage": float(st.session_state.get("ticket_leverage", 5.0)),
        "ticket_typ": st.session_state.get("ticket_typ", "Open-End Turbo"),
        "ticket_wkn": st.session_state.get("ticket_wkn"),
        "ticket_amount": float(st.session_state.get("ticket_amount", 500.0)),
        "news_sources": get_news_source_flags(),
        "news_sources_pro": st.session_state.get("news_sources_pro"),
        "ui_language": st.session_state.get("ui_language", "de"),
        # Bugfix: pro_mode fehlte hier komplett und wurde daher nie persistiert - bei
        # jedem Session-Neustart (z.B. Streamlit-Cloud-Prozess-Restart) fiel der Wert
        # auf False zurück, obwohl alle anderen Einstellungen erhalten blieben. Das
        # erklärte, warum der Pro Modus scheinbar "nach Refresh oder Trade" wieder aus war.
        "pro_mode": bool(st.session_state.get("pro_mode", False)),
        "_pro_mode_defaulted_once": bool(st.session_state.get("_pro_mode_defaulted_once", False)),
        "day_notes": st.session_state.get("day_notes") or {},
        "trades_opened_today": int(st.session_state.get("trades_opened_today", 0)),
        "last_loss_close_at": st.session_state.get("last_loss_close_at"),
        "risk_used_today_eur": float(st.session_state.get("risk_used_today_eur", 0.0)),
        "max_trades_per_day": st.session_state.get("max_trades_per_day", 5),
        "daily_risk_pct": float(st.session_state.get("daily_risk_pct", 2.0)),
        "max_loss_per_trade_eur": float(st.session_state.get("max_loss_per_trade_eur", 150.0)),
        "fee_per_trade": float(st.session_state.get("fee_per_trade", FEE_PER_TRADE)),
        "account_base": float(st.session_state.get("account_base", CAPITAL)),
        "tilt_enabled": bool(st.session_state.get("tilt_enabled", True)),
        "tilt_lookback": int(st.session_state.get("tilt_lookback", TILT_LOOKBACK_DEFAULT)),
        "tilt_loss_rate_threshold": float(st.session_state.get("tilt_loss_rate_threshold", TILT_LOSS_RATE_DEFAULT)),
        "tilt_consecutive_losses": int(st.session_state.get("tilt_consecutive_losses", TILT_CONSEC_DEFAULT)),
        "tilt_warn_threshold": int(st.session_state.get("tilt_warn_threshold", TILT_WARN_DEFAULT)),
        "tilt_lock_threshold": int(st.session_state.get("tilt_lock_threshold", TILT_LOCK_DEFAULT)),
        "tilt_lock": bool(st.session_state.get("tilt_lock", False)),
        "tilt_lock_reason": st.session_state.get("tilt_lock_reason"),
        "tilt_lock_score": st.session_state.get("tilt_lock_score"),
        "tilt_unlock_log": st.session_state.get("tilt_unlock_log") or [],
        "pending_close_notes": st.session_state.get("pending_close_notes") or [],
        "daily_setup_contract": st.session_state.get("daily_setup_contract"),
        "daily_setup_change_log": st.session_state.get("daily_setup_change_log") or [],
    }
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO app_state (key, value_json, updated_at, user_id) VALUES (?, ?, ?, ?)",
                  (f"session:{user_id}", json.dumps(state), now, user_id))


def load_app_state() -> dict:
    """Load persisted session state from DB - None im Gast-Modus (keine Persistenz)."""
    user_id = current_user_id()
    if not user_id:
        return None
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT value_json FROM app_state WHERE key = ? ORDER BY updated_at DESC LIMIT 1",
                  (f"session:{user_id}",))
        row = c.fetchone()
    if row:
        return json.loads(row[0])
    return None


def reset_app_state():
    """Clear all persisted state and trades - nur für den aktuell eingeloggten Nutzer."""
    user_id = current_user_id()
    if not user_id:
        return
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM app_state WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM trades WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM week_reports WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM month_reports WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM year_reports WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM day_reports WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM equity_history WHERE user_id = ?", (user_id,))


# Alle user_id-gebundenen Tabellen - eine Stelle für delete_account_data() UND
# collect_account_export_data() (2026-09), damit beide immer exakt denselben
# Umfang abdecken und nicht bei einer Änderung auseinanderlaufen.
ACCOUNT_DATA_TABLES = (
    "app_state", "trades", "week_reports", "month_reports",
    "year_reports", "day_reports", "equity_history", "subscriptions",
    "error_log", "pending_critical_events",
)


def delete_account_data() -> bool:
    """Löscht ALLE VoltDesk-Daten des aktuell eingeloggten Nutzers unwiderruflich
    (2026-09, "Account löschen" im Einstellungsdialog). Deckt alle user_id-
    gebundenen Tabellen ab - reset_app_state() deckte bisher nur 6 von 9 ab
    (subscriptions, error_log, pending_critical_events fehlten dort).

    WICHTIGE GRENZE dieser Funktion, die im UI-Text daneben auch so steht:
    Sie löscht nur VoltDesk-EIGENE Daten. Die eigentliche Login-Identität liegt
    bei Auth0 (externer Identity Provider) und wird hier NICHT gelöscht - dafür
    bräuchte es einen separaten, hier nicht eingebundenen Aufruf der Auth0
    Management API. Ein aktives Stripe-Abo wird ebenfalls NICHT automatisch
    gekündigt - das bewusst getrennt gehalten, dafür existiert bereits die
    Abo-Verwaltung (Customer Portal, render_subscription_ui())."""
    user_id = current_user_id()
    if not user_id:
        return False
    with get_db_connection() as conn:
        c = conn.cursor()
        for table in ACCOUNT_DATA_TABLES:
            c.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    return True


def collect_account_export_data() -> dict:
    """Sammelt ALLE VoltDesk-Daten des eingeloggten Nutzers für den DSGVO-
    Datenexport (2026-09, "Datenexport anfordern" im Einstellungsdialog) -
    dieselben Tabellen wie delete_account_data(), hier gelesen statt gelöscht.
    Gibt {tabellenname: [zeilen als dict]} zurück, JSON-serialisierbar (Werte
    über str() abgesichert für Typen, die json.dumps sonst nicht kennt, z.B.
    pandas Timestamps)."""
    user_id = current_user_id()
    if not user_id:
        return {}
    out = {}
    for table in ACCOUNT_DATA_TABLES:
        try:
            df = _read_sql(f"SELECT * FROM {table} WHERE user_id = ?", (user_id,))
        except Exception:
            df = pd.DataFrame()
        out[table] = json.loads(df.to_json(orient="records", date_format="iso"))
    return out




















def init_state():
    defaults = {
        "entered": False,
        "region": "USA",
        "index": "Nasdaq-100",
        "focus": "AMD",
        "high_vol_only": False,
        "_pro_mode_defaulted_once": False,
        "high_vol_threshold": HIGH_VOL_THRESHOLD,
        "positions": [],
        "killed": False,
        "trading_pause": False,
        "risk_lock": False,
        "kill_confirm_pending": False,
        "closed_pnl": 0.0,
        "watchlist": [],
        "ticket_side": "LONG",
        "ticket_leverage": 5.0,
        "ticket_typ": "Open-End Turbo",
        "ticket_wkn": None,
        "auto_flatten": True,
        "flatten_buffer_min": 60,
        "mode_override": "Auto",
        "fills": [],
        "quote_stamp": None,
        "day_report": None,
        "day_started": None,
        "taken_recs": [],
        "ticket_amount": 500.0,
        "fees_paid": 0.0,
        "pos_limit_enabled": True,
        "pos_limit_pct": MAX_POSITION_PCT,
        "refresh_sec": 300,
        "rec_history": [],
        "rec_cache": {},  # ticker -> {"rec": dict, "ts": float, "px": float}
        "last_rec_time": {},  # ticker -> last recommendation timestamp
        "news_sources": {k: v["default"] for k, v in NEWS_SOURCE_DEFS.items()},
        "ui_language": "de",
        "day_notes": {},
        "rec_hist_filter": "Alle",
        # Default True (2026-09): bei aktivem Trial/Abo soll Pro Modus ohne
        # zusätzlichen Klick aktiv sein. Bei fehlendem Zugriff wirkungslos, da die
        # Zwangsprüfung in der Sidebar/im Dialog dies bei jedem Aufruf ohnehin auf
        # False erzwingt - nur für berechtigte Nutzer macht der Default einen
        # Unterschied. Bereits explizit ausgeschaltete Nutzer behalten ihre eigene
        # gespeicherte Wahl (dieser Default gilt nur für neue/leere Sessions).
        "pro_mode": True,
        # Guardrails gegen Overtrading/Revenge-Trading (s. execute_paper_buy):
        "trades_opened_today": 0,
        "last_loss_close_at": None,
        "risk_used_today_eur": 0.0,
        "max_trades_per_day": 5,
        "daily_risk_pct": 2.0,
        "max_loss_per_trade_eur": 150.0,
        "account_base": CAPITAL,
        "tilt_enabled": True,
        "tilt_lookback": TILT_LOOKBACK_DEFAULT,
        "tilt_loss_rate_threshold": TILT_LOSS_RATE_DEFAULT,
        "tilt_consecutive_losses": TILT_CONSEC_DEFAULT,
        "tilt_warn_threshold": TILT_WARN_DEFAULT,
        "tilt_lock_threshold": TILT_LOCK_DEFAULT,
        "tilt_lock": False,
        "tilt_lock_reason": None,
        "tilt_lock_score": None,
        "tilt_unlock_log": [],
        "pending_close_notes": [],
        "daily_setup_contract": None,
        "daily_setup_change_log": [],
    }
    # Schema-Initialisierung + Migrations-Checks nur EINMAL pro Browser-Session:
    # vorher lief init_db() bei JEDEM Rerun (PRAGMA-/COUNT-Queries pro Seitenaufruf,
    # bei Turso remote mit echter Latenz) - das Schema aendert sich zwischen
    # Reruns einer Session nie.
    if not st.session_state.get("_db_initialized"):
        init_db()
        st.session_state["_db_initialized"] = True
    # WICHTIG: init_state() wird bei JEDEM Streamlit-Rerun erneut aufgerufen (main() ruft
    # es bei jeder Interaktion auf), nicht nur beim ersten Laden. Der DB-Reload darf daher
    # nur EINMAL pro Browser-Session passieren - sonst überschreibt er jede frische
    # In-Memory-Änderung (Region-/Index-Wechsel, Fokus-Klick etc.), die noch nicht via
    # save_app_state() persistiert wurde, beim nächsten Rerun wieder mit dem alten
    # DB-Stand ("State-Stomping"-Bug: Regionswechsel, Indexwechsel, Watchlist-Klick
    # wirkten dadurch wie "eingefroren").
    if not st.session_state.get("_state_loaded"):
        persisted = load_app_state()
        if persisted:
            for key, value in defaults.items():
                persisted_val = persisted.get(key)
                if persisted_val is not None:
                    st.session_state[key] = persisted_val
                else:
                    st.session_state[key] = value
            # Zusätzliche Keys aus persisted übernehmen, die nicht in defaults sind
            for key, value in persisted.items():
                if key not in defaults and not key.startswith("_"):
                    st.session_state[key] = value
        else:
            for key, value in defaults.items():
                st.session_state[key] = value
            # Nur bei einem WIRKLICH ersten Start (kein persistierter Zustand vorhanden)
            # die Standard-Watchlist befüllen. Dieser Seed-Schritt stand vorher außerhalb
            # dieses Blocks und lief dadurch bei JEDEM Rerun erneut - eine bewusst
            # geleerte Watchlist (watchlist == []) wurde beim nächsten Rerun/Neuladen
            # sofort wieder mit DEFAULT_WATCH aufgefüllt ("Watchlist leeren" wirkte
            # dadurch wie kaputt).
            for ticker in DEFAULT_WATCH:
                item = catalog_lookup(ticker)
                if item:
                    add_to_watchlist(item["ticker"], item["yf"], item["name"])
        # Splash immer bei neuem Session-Start — nie aus DB übernehmen. Ausnahme:
        # direkt nach einem Login-Callback (_handle_auth_callback erzwingt hier
        # bewusst einen Reload, um die Daten des jetzt bekannten Nutzers zu laden,
        # will den Nutzer dabei aber NICHT zurück auf den Splashscreen schicken).
        if not st.session_state.pop("_skip_splash_reset", False):
            st.session_state.entered = False
            st.session_state.splash_done = False
        # Legacy: Region "EUROPA" → "EU"
        if st.session_state.get("region") == "EUROPA":
            st.session_state.region = "EU"
        if st.session_state.get("region") not in UNIVERSE:
            st.session_state.region = "USA"
            st.session_state.index = list(UNIVERSE["USA"].keys())[0]
        elif st.session_state.get("index") not in UNIVERSE.get(st.session_state.region, {}):
            st.session_state.index = list(UNIVERSE[st.session_state.region].keys())[0]
        # Legacy journal migration: unify all fill fee keys to the internal `fee` key.
        # This prevents pandas from creating both "Gebühr" and "fee" columns when old
        # session/DB data is mixed with current Open/Close fills.
        normalized_fills = []
        for fill in st.session_state.get("fills") or []:
            f = dict(fill)
            if "fee" not in f and "Gebühr" in f:
                f["fee"] = f.pop("Gebühr")
            elif "Gebühr" in f:
                f.pop("Gebühr", None)
            normalized_fills.append(f)
        st.session_state.fills = normalized_fills
        # Legacy watchlist migration: older manually added entries may lack region/index.
        # Populate metadata once so Stock Picker/Ranker filters can classify them.
        migrated_watchlist = []
        for w in st.session_state.get("watchlist") or []:
            item = dict(w)
            if not item.get("region"):
                yf_sym = str(item.get("yf") or "").upper()
                ticker_sym = str(item.get("ticker") or "").upper()
                region, index = find_region_index(ticker_sym)
                if region == "—":
                    if yf_sym.endswith(".L") or ticker_sym.endswith(".L"):
                        region, index = "UK", "Manuell"
                    elif yf_sym.endswith(".SW") or ticker_sym.endswith(".SW"):
                        # Bugfix (2026-09): .SW (Schweizer Börse SIX) stand vorher in der EU-Gruppe -
                        # "Schweiz" ist aber ein eigenes Region-Bucket mit eigener Flagge (REGION_FLAG_ONLY).
                        region, index = "Schweiz", "Manuell"
                    elif any(yf_sym.endswith(sfx) or ticker_sym.endswith(sfx) for sfx in (".DE", ".PA", ".AS", ".MI", ".MC", ".BR")):
                        region, index = "EU", "Manuell"
                    else:
                        region, index = "USA", "Manuell"
                item["region"] = region
                item["index"] = item.get("index") or index
            migrated_watchlist.append(item)
        st.session_state.watchlist = migrated_watchlist
        st.session_state["_state_loaded"] = True
    else:
        for key, value in defaults.items():
            if key not in st.session_state:
                st.session_state[key] = value
    if "last_refresh_ts" not in st.session_state:
        # Erststart einer Session: refresh_market_data() wurde noch nicht aufgerufen
        # (Caches sind ohnehin leer) - trotzdem einen Startwert setzen, damit "Last
        # Refresh" auf der Hauptseite beim allerersten Rendern nicht leer/undefiniert ist.
        st.session_state["last_refresh_ts"] = datetime.now(TZ_BERLIN)
















PARALLEL_FETCH_WORKERS = 5  # bewusst moderat gehalten - mehr gleichzeitige Yahoo-
# Requests erhöhen das Rate-Limit-Risiko (429/leere Antworten) überproportional
# zum Geschwindigkeitsgewinn. Ausgangswert zum Testen, ggf. nach Beobachtung
# des tatsächlichen Verhaltens anpassen.














MID_STRUCTURE_MAX_DELAY_MIN = 60  # Notfall-Deckel: bleiben die Struktur-Kriterien (s.u.)
# so lange unerfüllt, wechselt VoltDesk trotzdem rein zeitbasiert in MID, damit die App bei
# unklarer Struktur (z.B. sehr ruhiger/illiquider Titel ohne klares Momentum) nicht
# dauerhaft in OPEN hängen bleibt.


def _mid_structure_signal(yf_symbol: str) -> dict:
    """Prüft, ob die Marktstruktur einen Wechsel von OPEN nach MID rechtfertigt - nicht
    nur die reine Uhrzeit (Opening Range abgeschlossen prüft bereits der Aufrufer über
    die Zeitfenster in detect_mode()). Zusätzlich müssen gelten:
      - VWAP etabliert (liegt vor - benötigt genug Intraday-Volumen für eine valide
        Berechnung, s. fetch_levels())
      - Momentum eindeutig in eine Richtung (Higher-High/Higher-Low bzw. Lower-High/
        Lower-Low der letzten 10 Bars - dieselbe Logik wie in detect_intraday_patterns())
      - Volumen nicht mehr extrem (aktuelle 5-Min-Bar nicht mehr als das Doppelte des
        gleitenden 20-Bar-Durchschnitts - ein typischer Open-Volumen-Spike hat sich gelegt)
    """
    out = {"vwap_ok": False, "momentum": None, "volume_ok": False, "ready": False}
    try:
        levels = fetch_levels(yf_symbol)
        intra = fetch_intraday(yf_symbol)
    except Exception:
        return out
    if intra is None or intra.empty:
        return out

    out["vwap_ok"] = levels.get("vwap") is not None

    if len(intra) >= 10:
        highs = intra["High"].tail(10)
        lows = intra["Low"].tail(10)
        if highs.iloc[-1] > highs.iloc[-5] > highs.iloc[-9] and lows.iloc[-1] > lows.iloc[-5]:
            out["momentum"] = "up"
        elif lows.iloc[-1] < lows.iloc[-5] < lows.iloc[-9] and highs.iloc[-1] < highs.iloc[-5]:
            out["momentum"] = "down"

    if "Volume" in intra.columns and not intra.empty:
        last_vol = float(intra["Volume"].iloc[-1]) if pd.notna(intra["Volume"].iloc[-1]) else 0.0
        vol_avg = float(intra["Volume"].tail(20).mean())
        out["volume_ok"] = (last_vol <= 2.0 * vol_avg) if vol_avg > 0 else True
    else:
        out["volume_ok"] = True  # keine Volumendaten -> Kriterium nicht künstlich blockieren

    out["ready"] = out["vwap_ok"] and out["momentum"] is not None and out["volume_ok"]
    return out


def _opening_range(intra: pd.DataFrame, minutes: int = 30):
    if intra.empty:
        return None, None
    start = intra.index[0]
    window = intra[intra.index <= start + pd.Timedelta(minutes=minutes)]
    if window.empty:
        window = intra.head(6)
    return float(window["High"].max()), float(window["Low"].min())


def minutes_since_session_open(venue: str) -> float:
    now = datetime.now(TZ_BERLIN)
    open_time = session_windows(venue)["open"][0]
    market_open = now.replace(hour=open_time.hour, minute=open_time.minute, second=0, microsecond=0)
    return (now - market_open).total_seconds() / 60.0


def _session_total_minutes(venue: str) -> float:
    """Gesamtlänge der reg. Handelssession in Minuten (Open-Start bis Close-Ende) -
    Basis für die zeitanteilige RVOL-Normalisierung unten."""
    w = session_windows(venue)
    start, end = w["open"][0], w["close"][1]
    today = datetime.now(TZ_BERLIN).date()
    start_dt = datetime.combine(today, start)
    end_dt = datetime.combine(today, end)
    return max(1.0, (end_dt - start_dt).total_seconds() / 60.0)


def compute_rvol(today_volume: Optional[float], avg_volume_20d: Optional[float], venue: str) -> Optional[float]:
    """Relatives Volumen (2026-09): heutiges Volumen bis JETZT vs. das für diese
    Tageszeit ERWARTETE Volumen (Ø der letzten 20 Tage, anteilig nach verstrichener
    Sessionzeit skaliert) - ein reiner Ratio ohne Zeitanteil würde RVOL früh am Tag
    systematisch unterschätzen (Ø-Volumen ist ein GANZTAGES-Durchschnitt).
    Nutzt ausschließlich Felder, die fetch_levels() für andere Zwecke ohnehin schon
    berechnet (today_volume, avg_volume_20d) - kein zusätzlicher API-Call."""
    if not today_volume or not avg_volume_20d or avg_volume_20d <= 0:
        return None
    elapsed_min = max(1.0, minutes_since_session_open(venue))
    elapsed_frac = min(1.0, elapsed_min / _session_total_minutes(venue))
    expected_so_far = avg_volume_20d * elapsed_frac
    if expected_so_far <= 0:
        return None
    return today_volume / expected_so_far


def classify_orb_tests(intra: pd.DataFrame, or_high, or_low, minutes: int = 30) -> dict:
    """First Break vs. Retest-Break nach Ende der Opening Range."""
    out = {
        "first_long": False, "first_short": False,
        "retest_long": False, "retest_short": False,
        "or_ready": False,
    }
    if intra is None or intra.empty or or_high is None or or_low is None:
        return out
    start = intra.index[0]
    post = intra[intra.index > start + pd.Timedelta(minutes=minutes)]
    if post.empty:
        return out
    out["or_ready"] = True
    closes = [float(x) for x in post["Close"].tolist()]
    first_long_i = first_short_i = None
    pulled_long = pulled_short = False
    for i, c in enumerate(closes):
        if first_long_i is None and c > float(or_high):
            first_long_i = i
        elif first_long_i is not None and c <= float(or_high):
            pulled_long = True
        if first_short_i is None and c < float(or_low):
            first_short_i = i
        elif first_short_i is not None and c >= float(or_low):
            pulled_short = True
    last = closes[-1]
    if first_long_i is not None:
        out["first_long"] = True
        out["retest_long"] = bool(pulled_long and last > float(or_high))
    if first_short_i is not None:
        out["first_short"] = True
        out["retest_short"] = bool(pulled_short and last < float(or_low))
    return out


def suggest_take_profit(side: str, price: float, levels: Optional[dict] = None, intra=None):
    """Nächstes logisches Ziel: PDH/OR-High über dem Kurs (Short: PDL/OR-Low)."""
    levels = levels or {}
    or_h = or_l = None
    if intra is not None and getattr(intra, "empty", True) is False:
        or_h, or_l = _opening_range(intra)
    pdh = _as_float(levels.get("prev_high"))
    pdl = _as_float(levels.get("prev_low"))
    px = _as_float(price)
    if not px:
        return None
    if side == "LONG":
        above = [lvl for lvl in (or_h, pdh) if lvl and float(lvl) > px]
        return min(above) if above else None
    below = [lvl for lvl in (or_l, pdl) if lvl and float(lvl) < px]
    return max(below) if below else None


def detect_intraday_patterns(intra: pd.DataFrame, levels: dict, mode: str = "OPEN", venue: str = "Xetra") -> list:
    """Erweiterte Mustererkennung mit Volumen-Validierung, Zeit-Filter und Konfidenz-Score."""
    hints = []
    if intra is None or len(intra) < 8:
        return hints

    # Zeit-Filter: Erste 15 Min nach Open → keine Muster.
    # Der reale Open-Zeitpunkt hängt vom Handelsplatz ab (Xetra 09:00 CET,
    # US-Werte 15:30 CET) - vorher war hier immer 09:00 hartcodiert, wodurch
    # der Filter für US-Titel faktisch nie zur echten Open-Zeit griff.
    now = datetime.now(TZ_BERLIN)
    open_time = session_windows(venue)["open"][0]
    market_open = now.replace(hour=open_time.hour, minute=open_time.minute, second=0, microsecond=0)
    if (now - market_open).total_seconds() < 900:
        return hints
    # PRE/CLOSE → keine neuen Intraday-Setups
    if mode in {"PRE", "CLOSE"}:
        return hints

    last = intra.iloc[-1]
    prev = intra.iloc[-2]
    close = float(last["Close"])
    high = float(last["High"])
    low = float(last["Low"])
    o = float(last["Open"])
    body = abs(close - o)
    rng = max(high - low, 1e-9)

    # Volumen-Validierung
    vol_ok = False
    if "Volume" in intra.columns:
        vol = float(last["Volume"]) if pd.notna(last["Volume"]) else 0
        vol_avg = float(intra["Volume"].tail(20).mean()) if len(intra) >= 20 else vol
        vol_ok = vol > 1.5 * vol_avg if vol_avg > 0 else True

    vwap = levels.get("vwap")
    pdh = levels.get("prev_high")
    pdl = levels.get("prev_low")
    atr = levels.get("atr")

    # Trend-Regime (einfacher HH/HL oder LH/LL Check)
    trend_up = False
    trend_down = False
    if len(intra) >= 10:
        highs = intra["High"].tail(10)
        lows = intra["Low"].tail(10)
        if highs.iloc[-1] > highs.iloc[-5] > highs.iloc[-9] and lows.iloc[-1] > lows.iloc[-5]:
            trend_up = True
        elif lows.iloc[-1] < lows.iloc[-5] < lows.iloc[-9] and highs.iloc[-1] < highs.iloc[-5]:
            trend_down = True

    # OR-Breakout: Wait Window, First Break nur markieren, handelbar = Retest-Break.
    or_high, or_low = _opening_range(intra)
    waited = minutes_since_session_open(venue) >= ORB_WAIT_MINUTES
    orb = classify_orb_tests(intra, or_high, or_low)
    if or_high and orb["retest_long"] and waited:
        conf = 70 + (20 if vol_ok else 0) + (10 if trend_up else 0)
        hints.append({
            "name": "Opening-Range Breakout",
            "bias": "long",
            "note": "Retest-Break über OR-High (zweiter Test).",
            "confidence": min(conf, 100),
            "orb_test": "retest",
        })
    elif or_low and orb["retest_short"] and waited:
        conf = 70 + (20 if vol_ok else 0) + (10 if trend_down else 0)
        hints.append({
            "name": "Opening-Range Breakout",
            "bias": "short",
            "note": "Retest-Break unter OR-Low (zweiter Test).",
            "confidence": min(conf, 100),
            "orb_test": "retest",
        })
    elif or_high and orb["first_long"] and close > or_high:
        conf = 40 + (10 if vol_ok else 0)
        hints.append({
            "name": "Opening-Range First Break",
            "bias": "neutral",
            "note": "Erster Bruch des OR-High — nicht handelbar, auf Retest warten.",
            "confidence": min(conf, 100),
            "orb_test": "first",
        })
    elif or_low and orb["first_short"] and close < or_low:
        conf = 40 + (10 if vol_ok else 0)
        hints.append({
            "name": "Opening-Range First Break",
            "bias": "neutral",
            "note": "Erster Bruch des OR-Low — nicht handelbar, auf Retest warten.",
            "confidence": min(conf, 100),
            "orb_test": "first",
        })

    # VWAP
    if vwap:
        above = intra["Close"] > vwap
        if close >= vwap and not bool(above.iloc[-3]) and bool(above.iloc[-1]):
            conf = 55 + (20 if vol_ok else 0)
            hints.append({"name": "VWAP-Reclaim", "bias": "long", "note": "Zurückerobern des VWAP.", "confidence": min(conf, 100)})
        elif close <= vwap and bool(above.iloc[-3]) and not bool(above.iloc[-1]):
            conf = 55 + (20 if vol_ok else 0)
            hints.append({"name": "VWAP-Verlust", "bias": "short", "note": "Kurs rutscht unter den VWAP.", "confidence": min(conf, 100)})

        # First VWAP Pullback: Impuls weg vom VWAP, erster Rücklauf in die Zone, Ablehnung.
        zone = max(float(atr or 0) * 0.12, abs(float(vwap)) * 0.0012)
        closes_l = [float(x) for x in intra["Close"].tolist()]
        lows_l = [float(x) for x in intra["Low"].tolist()]
        highs_l = [float(x) for x in intra["High"].tolist()]
        opens_l = [float(x) for x in intra["Open"].tolist()]
        n = len(closes_l)
        impulse_up = impulse_dn = None
        run_up = run_dn = 0
        for i in range(n - 2):
            if closes_l[i] > float(vwap) + zone:
                run_up += 1
                run_dn = 0
                if run_up >= 2:
                    impulse_up = i
            elif closes_l[i] < float(vwap) - zone:
                run_dn += 1
                run_up = 0
                if run_dn >= 2:
                    impulse_dn = i
            else:
                run_up = run_dn = 0
        if impulse_up is not None:
            touch = next((j for j in range(impulse_up + 1, n) if lows_l[j] <= float(vwap) + zone), None)
            earlier = any(lows_l[j] <= float(vwap) + zone for j in range(impulse_up + 1, touch or impulse_up))
            if touch is not None and touch >= n - 4 and not earlier and close >= float(vwap) and close >= o:
                conf = 62 + (18 if vol_ok else 0) + (10 if trend_up else 0)
                hints.append({
                    "name": "VWAP-Pullback",
                    "bias": "long",
                    "note": "Erster Rücklauf an den VWAP nach Impuls darüber.",
                    "confidence": min(conf, 100),
                })
        if impulse_dn is not None:
            touch = next((j for j in range(impulse_dn + 1, n) if highs_l[j] >= float(vwap) - zone), None)
            if touch is not None and touch >= n - 4 and close <= float(vwap) and close <= o:
                conf = 62 + (18 if vol_ok else 0) + (10 if trend_down else 0)
                hints.append({
                    "name": "VWAP-Pullback",
                    "bias": "short",
                    "note": "Erster Rücklauf an den VWAP nach Impuls darunter.",
                    "confidence": min(conf, 100),
                })

    # PDH/PDL
    if pdh and high >= pdh and close >= pdh:
        conf = 65 + (20 if vol_ok else 0) + (10 if trend_up else 0)
        hints.append({"name": "Break Vortag-High", "bias": "long", "note": "Schluss über PDH.", "confidence": min(conf, 100)})
    if pdl and low <= pdl and close <= pdl:
        conf = 65 + (20 if vol_ok else 0) + (10 if trend_down else 0)
        hints.append({"name": "Break Vortag-Low", "bias": "short", "note": "Schluss unter PDL.", "confidence": min(conf, 100)})
    if pdh and high >= pdh and close < pdh:
        conf = 60 + (15 if vol_ok else 0)
        hints.append({"name": "Sweep PDH + Rejection", "bias": "short", "note": "Vortag-High gestreift, Kerze schließt darunter.", "confidence": min(conf, 100)})
    if pdl and low <= pdl and close > pdl:
        conf = 60 + (15 if vol_ok else 0)
        hints.append({"name": "Sweep PDL + Reclaim", "bias": "long", "note": "Vortag-Low gestreift, Kerze schließt darüber.", "confidence": min(conf, 100)})

    # Engulfing
    prev_body = abs(float(prev["Close"]) - float(prev["Open"]))
    bull_eng = close > o and float(prev["Close"]) < float(prev["Open"]) and close >= float(prev["Open"]) and o <= float(prev["Close"])
    bear_eng = close < o and float(prev["Close"]) > float(prev["Open"]) and close <= float(prev["Open"]) and o >= float(prev["Close"])
    if bull_eng and body > prev_body * 0.8:
        conf = 55 + (20 if vol_ok else 0) + (15 if trend_up else 0)
        hints.append({"name": "Bullish Engulfing", "bias": "long", "note": "Letzte Kerze umschließt die vorherige Abwärtskerze.", "confidence": min(conf, 100)})
    if bear_eng and body > prev_body * 0.8:
        conf = 55 + (20 if vol_ok else 0) + (15 if trend_down else 0)
        hints.append({"name": "Bearish Engulfing", "bias": "short", "note": "Letzte Kerze umschließt die vorherige Aufwärtskerze.", "confidence": min(conf, 100)})

    # Inside Bar
    inside = high <= float(prev["High"]) and low >= float(prev["Low"])
    if inside:
        hints.append({"name": "Inside Bar", "bias": "neutral", "note": "Kompression — Bruch der Mutterkerze oft nächster Impuls.", "confidence": 40})

    # Dochte
    if body / rng < 0.25:
        if close > o and (high - close) / rng > 0.5:
            conf = 50 + (15 if vol_ok else 0)
            hints.append({"name": "Upper Wick / Rejection", "bias": "short", "note": "Langer Docht oben, Käufer zurückgewiesen.", "confidence": min(conf, 100)})
        if close < o and (close - low) / rng > 0.5:
            conf = 50 + (15 if vol_ok else 0)
            hints.append({"name": "Lower Wick / Absorption", "bias": "long", "note": "Langer Docht unten, Verkaufsdruck aufgenommen.", "confidence": min(conf, 100)})

    # Verbesserte Double Top/Bottom
    look = intra.tail(25).reset_index(drop=True)
    if len(look) >= 10:
        # Double Top
        high_vals = look["High"].values
        for i in range(len(high_vals) - 3):
            for j in range(i + 3, len(high_vals)):
                h1, h2 = float(high_vals[i]), float(high_vals[j])
                if abs(h1 - h2) / max(h1, 1e-9) < 0.005:
                    between_lows = look["Low"].iloc[i:j]
                    if len(between_lows) > 2 and float(between_lows.min()) < min(h1, h2) * 0.998:
                        conf = 55 + (20 if vol_ok else 0)
                        hints.append({"name": "Double Top (lokal)", "bias": "short", "note": "Zwei ähnliche Hochs mit Abstand und Tief dazwischen.", "confidence": min(conf, 100)})
                        break
            else:
                continue
            break

        # Double Bottom
        low_vals = look["Low"].values
        for i in range(len(low_vals) - 3):
            for j in range(i + 3, len(low_vals)):
                l1, l2 = float(low_vals[i]), float(low_vals[j])
                if abs(l1 - l2) / max(l1, 1e-9) < 0.005:
                    between_highs = look["High"].iloc[i:j]
                    if len(between_highs) > 2 and float(between_highs.max()) > max(l1, l2) * 1.002:
                        conf = 55 + (20 if vol_ok else 0)
                        hints.append({"name": "Double Bottom (lokal)", "bias": "long", "note": "Zwei ähnliche Tiefs mit Abstand und Hoch dazwischen.", "confidence": min(conf, 100)})
                        break
            else:
                continue
            break

    # Trend-Struktur
    closes_short = intra["Close"].tail(12)
    if len(closes_short) >= 8:
        if closes_short.iloc[-1] > closes_short.iloc[-4] > closes_short.iloc[-8]:
            conf = 45 + (15 if vol_ok else 0)
            hints.append({"name": "Höhere Hochs (kurz)", "bias": "long", "note": "Kurzfristige Aufwärtsstruktur.", "confidence": min(conf, 100)})
        elif closes_short.iloc[-1] < closes_short.iloc[-4] < closes_short.iloc[-8]:
            conf = 45 + (15 if vol_ok else 0)
            hints.append({"name": "Tiefere Tiefs (kurz)", "bias": "short", "note": "Kurzfristige Abwärtsstruktur.", "confidence": min(conf, 100)})

    # Range-Expansion
    if atr and rng > 1.4 * (atr / 78.0 * 5):
        med = float((intra["High"] - intra["Low"]).tail(20).median())
        if rng > 2.0 * max(med, 1e-9):
            hints.append({"name": "Range-Expansion", "bias": "neutral", "note": "Letzte Kerze deutlich größer als Median.", "confidence": 35})

    # Deduplizierung
    seen = set()
    uniq = []
    for h in hints:
        if h["name"] not in seen:
            seen.add(h["name"])
            uniq.append(h)

    # Nur Muster mit Confidence >= 40
    return [h for h in uniq if h.get("confidence", 0) >= 40][:6]


def build_derivative_ladder(price: float, side: str, atr: Optional[float], spread_pct: Optional[float]) -> pd.DataFrame:
    if not price:
        return pd.DataFrame()
    atr_pct = (atr / price * 100) if atr else None
    specs = [
        (2.5, "Konservativ", "Open-End Turbo"),
        (4.0, "Standard", "Open-End Turbo"),
        (5.0, "Standard+", "Open-End Turbo"),
        (6.5, "Aggressiv", "Open-End Turbo"),
        (8.0, "Max-Cap", "Open-End Turbo"),
        (3.0, "Ohne KO", "Faktor-Zertifikat"),
        (5.0, "Ohne KO", "Faktor-Zertifikat"),
    ]
    rows = []
    for lev, profil, typ in specs:
        has_ko = typ.startswith("Open-End")
        if has_ko:
            if side == "LONG":
                ko = price * (1 - 1 / lev)
            else:
                ko = price * (1 + 1 / lev)
            ko_dist = abs(price - ko) / price * 100
        else:
            ko = None
            ko_dist = None
        est_spread = (spread_pct or 0.08) * max(lev / 2.5, 1)
        ok = True
        reason = "ok für Intraday"
        if has_ko and ko_dist is not None:
            if ko_dist < 4:
                ok, reason = False, "KO zu nah"
            elif atr_pct and ko_dist < 0.7 * atr_pct:
                ok, reason = False, "ATR größer als KO-Puffer"
            elif est_spread > 0.9:
                ok, reason = False, "Spread zu weit"
            elif ko_dist >= 12:
                reason = "ruhiger Hebel, mehr Weg bis KO"
        else:
            reason = "kein KO, aber Pfadabhängigkeit / Overnight meiden"
        rows.append(
            {
                "Profil": profil,
                "Typ": typ,
                "Hebel": lev,
                "KO": ko,
                "KO-Abstand %": ko_dist,
                "Spread-Schätzung %": est_spread,
                "Eignung": "geeignet" if ok else "ungeeignet",
                "Hinweis": reason,
            }
        )
    return pd.DataFrame(rows)


def _model_products(ticker: str) -> list:
    rows = []
    # Auf 10 Hebelstufen pro Seite erweitert (vorher nur 3: 3x/5x/7x) - deckt die Spanne
    # von 2x (konservativ) bis 8x (= MAX_LEVERAGE-Cap) deutlich feiner ab.
    tiers = (
        (2.0, "Sehr konservativ", 0.30),
        (2.5, "Konservativ", 0.35),
        (3.0, "Konservativ+", 0.40),
        (3.5, "Moderat", 0.45),
        (4.0, "Moderat+", 0.50),
        (5.0, "Standard", 0.55),
        (6.0, "Standard+", 0.65),
        (7.0, "Aggressiv", 0.75),
        (7.5, "Aggressiv+", 0.85),
        (8.0, "Maximal (Cap)", 0.95),
    )
    for side in ("LONG", "SHORT"):
        for lev, profil, spread in tiers:
            wkn = f"CAT{ticker[:3]}{side[0]}{int(lev * 10)}"
            rows.append(
                {
                    "ticker": ticker,
                    "wkn": wkn,
                    "isin": "",
                    "issuer": "Desk-Katalog",
                    "side": side,
                    "typ": "Open-End Turbo",
                    "ko": None,
                    "ko_pct": 100.0 / lev,
                    "spread_pct": spread,
                    "source": "Modell",
                    "profil": profil,
                }
            )
    return rows


def live_catalog(ticker: str, price: Optional[float]) -> pd.DataFrame:
    if not ticker or not price:
        return pd.DataFrame()
    items = [p for p in PRODUCT_CATALOG if p["ticker"] == ticker]
    if not items:
        items = _model_products(ticker)
    else:
        have_sides = {p["side"] for p in items}
        if "SHORT" not in have_sides or "LONG" not in have_sides:
            items = items + [p for p in _model_products(ticker) if p["side"] not in have_sides]
    rows = []
    for p in items:
        side = p["side"]
        ko = p.get("ko")
        if ko is None and p.get("ko_pct"):
            if side == "LONG":
                ko = price * (1 - p["ko_pct"] / 100)
            else:
                ko = price * (1 + p["ko_pct"] / 100)
        if ko is None:
            continue
        if side == "LONG":
            dist = (price - ko) / price * 100
            lev = price / max(price - ko, 1e-6)
        else:
            dist = (ko - price) / price * 100
            lev = price / max(ko - price, 1e-6)
        knocked = dist <= 0
        spread = p.get("spread_pct") or 0
        _max_lev = effective_max_leverage()
        ok = (not knocked) and 2.0 <= lev <= _max_lev and dist >= 5 and spread < 2.0
        if knocked:
            reason = "ausgeknockt / KO erreicht"
        elif lev > _max_lev:
            reason = f"Hebel über Cap {_max_lev:.0f}x" + ("" if effective_pro_mode() else " (Pro Modus erlaubt bis 8x)")
        elif dist < 5:
            reason = "KO zu nah (< 5%)"
        elif spread >= 2.0:
            reason = f"Spread zu hoch ({spread:.2f}%)"
        else:
            reason = "ok für Intraday"
        rows.append(
            {
                "WKN": p["wkn"],
                "ISIN": p.get("isin") or "—",
                "Emittent": p["issuer"],
                "Seite": side,
                "Typ": p["typ"],
                "KO": ko,
                "KO-Abstand %": dist,
                "Hebel": min(lev, 99),
                "Spread %": p.get("spread_pct"),
                "Quelle": p.get("source", ""),
                "Eignung": "geeignet" if ok else "ungeeignet",
                "Hinweis": reason,
            }
        )
    return pd.DataFrame(rows)


# Gewichtete Phrasen: längere Treffer zuerst. Score ist relativ, nicht Wahrscheinlichkeit.
NEWS_POS_TERMS = (
    ("beats estimates", 3), ("übertrifft erwartungen", 3), ("raises guidance", 3),
    ("anhebung der prognose", 3), ("guidance raised", 3), ("upgrade to buy", 3),
    ("upgrade to overweight", 3), ("price target raised", 2), ("kursziel angehoben", 2),
    ("initiated buy", 2), ("overweight", 2), ("outperform", 2),
    ("record revenue", 2), ("rekordumsatz", 2), ("contract win", 2),
    ("großauftrag", 2), ("übernimmt", 2), ("acquisition", 2),
    ("beats", 2), ("surge", 2), ("soars", 2), ("rally", 2),
    ("upgrade", 2), ("record", 1),
    ("steigt", 1), ("rekord", 1), ("besser", 1), ("übertrifft", 2),
    ("auftrag", 1), ("genehmigt", 1), ("approval", 1), ("approved", 1),
    ("buyback", 2), ("aktienrückkauf", 2), ("dividendenerhöhung", 2),
)
NEWS_NEG_TERMS = (
    ("misses estimates", 3), ("verfehlt erwartungen", 3), ("cuts guidance", 3),
    ("senkt prognose", 3), ("profit warning", 3), ("gewinnwarnung", 3),
    ("downgrade to sell", 3), ("downgrade to underweight", 3),
    ("price target cut", 2), ("kursziel gesenkt", 2), ("underweight", 2),
    ("underperform", 2), ("lawsuit", 2), ("class action", 2),
    ("investigation", 2), ("ermittlung", 2), ("probe", 2),
    ("recall", 3), ("fraud", 3), ("betrug", 3), ("insolvency", 3),
    ("insolvenz", 3), ("delisting", 3), ("secondary offering", 2),
    ("kapitalerhöhung", 2), ("downgrade", 2),
    ("slump", 2), ("sinkt", 1), ("verfehlt", 2),
    ("klage", 2), ("warnung", 2), ("short seller", 2),
)

NEWS_TAG_RULES = {
    "adhoc": (
        "ad-hoc", "adhoc", "eqs-adhoc", "eqs-news", "dgap", "pflichtmitteilung",
        "directors' dealings", "directors dealings", "art. 17 mar", "art. 19 mar",
        "stimmrechte", "voting rights",
    ),
    "earnings": (
        "earnings", "quartalszahlen", "halbjahreszahlen", "jahreszahlen",
        "eps", "guidance", "prognose", "outlook", "results", "geschäftszahlen",
        "beats", "misses", "quartalsbericht",
    ),
    "rating": (
        "upgrade", "downgrade", "kursziel", "price target", "initiated",
        "overweight", "underweight", "outperform", "underperform",
        "analyst", "einstufung",
    ),
    "legal": (
        "lawsuit", "klage", "probe", "investigation", "ermittlung",
        "sec ", "bafin", "fraud", "recall", "sanktion",
    ),
    "mna": (
        "acquisition", "merger", "übernahme", "übernimmt", "buyout",
        "joint venture", "spin-off",
    ),
}

_NEWS_WS_RE = re.compile(r"\s+")
_NEWS_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def get_news_source_flags() -> dict:
    # Ohne Pro Modus sind Quellen nicht individuell wählbar - fest auf Yahoo + Finnhub
    # begrenzt, unabhängig von zuvor gespeicherten Flags (z.B. aus einer Pro-Modus-
    # Sitzung). Erst der Pro Modus schaltet die freie Auswahl aller Quellen frei.
    if not effective_pro_mode():
        base = {k: (k in ("yahoo", "finnhub")) for k in NEWS_SOURCE_DEFS}
        if FINNHUB_NEWS_DISABLED:
            base["finnhub"] = False
        return base
    # Pro Modus: standardmäßig ALLE Quellen aktiv (nicht nur die generellen
    # NEWS_SOURCE_DEFS-Defaults) - eigener State-Key, damit ein späterer Wechsel
    # zurück in den Standard-Modus die individuelle Pro-Auswahl nicht verwirft.
    stored = st.session_state.get("news_sources_pro")
    defaults = {k: True for k in NEWS_SOURCE_DEFS}
    if isinstance(stored, dict):
        defaults.update({k: bool(stored.get(k, True)) for k in defaults})
    if FINNHUB_NEWS_DISABLED:
        defaults["finnhub"] = False
    return defaults


def enabled_news_sources() -> tuple:
    flags = get_news_source_flags()
    return tuple(k for k, on in flags.items() if on)


def _as_secret_str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        return ""
    return str(val).strip().strip('"').strip("'")


def _iter_secret_items(node, prefix=""):
    """Rekursiv (secrets sind i.d.R. max. 1-2 Ebenen tief) alle (Pfad, Wert)-Paare
    aus st.secrets liefern - Top-Level-Keys und eine Sektions-Ebene darunter."""
    try:
        items = node.items() if hasattr(node, "items") else None
    except Exception:
        items = None
    if items is None:
        return
    for k, v in items:
        path = f"{prefix}{k}"
        yield path, v
        if hasattr(v, "items"):
            yield from _iter_secret_items(v, prefix=f"{path}.")


def get_finnhub_key() -> str:
    """Finnhub-Key aus Streamlit Secrets — namensunabhängig.

    Vorherige Version prüfte nur eine feste Liste bekannter Namensvarianten
    (FINNHUB_API_KEY / [finnhub].api_key / ...). Das griff ins Leere, sobald jemand
    z.B. innerhalb einer [finnhub]-Sektion denselben Namen wie die "flache" Variante
    verwendet (also [finnhub] FINNHUB_API_KEY = "..." statt [finnhub] api_key = "...") -
    diese Kombination stand auf keiner der beiden Namenslisten und der Key galt fälschlich
    als "nicht gesetzt", obwohl er in den Secrets stand.
    Jetzt: rekursiv über alle Top-Level- und Sektions-Keys iterieren und jeden Schlüssel
    nehmen, dessen Name (case-insensitiv) "finnhub" UND ("key" oder "token") enthält -
    das deckt jede sinnvolle Schreibweise ab, unabhängig von Groß-/Kleinschreibung oder
    Trennzeichen.
    """
    try:
        secrets = st.secrets
    except Exception:
        return ""

    for path, val in _iter_secret_items(secrets):
        name_lower = path.lower().replace("-", "_")
        if "finnhub" in name_lower and ("key" in name_lower or "token" in name_lower):
            s = _as_secret_str(val)
            if s:
                return s
    return ""


def debug_secret_key_names() -> list:
    """Nur die NAMEN (nicht Werte!) aller in st.secrets gefundenen Schlüssel, für die
    Diagnose-Anzeige in den Einstellungen (z.B. wenn ein Key trotz Pflege nicht erkannt wird)."""
    try:
        secrets = st.secrets
    except Exception:
        return []
    return [path for path, _ in _iter_secret_items(secrets)]


def _news_http_get(url: str, timeout: int = 8) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": NEWS_UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_news_dt(value) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_news_dt(int(text))
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _fmt_news_when(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.astimezone(TZ_BERLIN).strftime("%d.%m. %H:%M")


def _norm_news_title(title: str) -> str:
    t = html.unescape(title or "").lower()
    t = _NEWS_PUNCT_RE.sub(" ", t)
    return _NEWS_WS_RE.sub(" ", t).strip()


def _news_fingerprint(title: str) -> str:
    return hashlib.sha1(_norm_news_title(title)[:120].encode("utf-8")).hexdigest()


def _tag_news(title: str, extra: tuple = ()) -> list:
    blob = (title or "").lower()
    tags = []
    for tag, keys in NEWS_TAG_RULES.items():
        if any(k in blob for k in keys):
            tags.append(tag)
    for tag in extra or ():
        if tag not in tags:
            tags.append(tag)
    return tags


def _score_headline(title: str, tags: list) -> tuple:
    blob = (title or "").lower()
    score = 0
    hits = []
    used = set()
    for phrase, w in NEWS_POS_TERMS:
        if phrase in blob and phrase not in used:
            score += w
            hits.append(f"+{phrase}")
            used.add(phrase)
    for phrase, w in NEWS_NEG_TERMS:
        if phrase in blob and phrase not in used:
            score -= w
            hits.append(f"−{phrase}")
            used.add(phrase)
    if "adhoc" in tags:
        score = int(round(score * 1.35)) if score else score
    if "earnings" in tags and score:
        score = int(round(score * 1.2))
    if "rating" in tags and score:
        score = int(round(score * 1.15))
    return score, hits[:4]


def annotate_news_item(item: dict) -> dict:
    title = html.unescape(item.get("title") or "").strip()
    extra = tuple(item.get("force_tags") or ())
    tags = _tag_news(title, extra)
    score, hits = _score_headline(title, tags)
    dt = item.get("dt") or _parse_news_dt(item.get("published"))
    out = dict(item)
    out["title"] = title
    out["tags"] = tags
    out["score"] = score
    out["hits"] = hits
    out["dt"] = dt
    out["when"] = _fmt_news_when(dt)
    out["fp"] = _news_fingerprint(title)
    if score > 1:
        out["bias"] = "long"
    elif score < -1:
        out["bias"] = "short"
    else:
        out["bias"] = "neutral"
    return out


def _recency_weight(dt: Optional[datetime]) -> float:
    if not dt:
        return 0.6
    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if hours < 0:
        return 1.2
    if hours <= 2:
        return 2.0
    if hours <= 8:
        return 1.5
    if hours <= 24:
        return 1.0
    if hours <= 72:
        return 0.6
    return 0.25


def score_news(news: list) -> tuple:
    """Gewichtetes Sentiment über gemergte Headlines."""
    weighted = 0.0
    hits = []
    for item in news or []:
        if "score" not in item:
            item = annotate_news_item(item)
        w = _recency_weight(item.get("dt"))
        if "adhoc" in (item.get("tags") or []):
            w *= 1.4
        if "earnings" in (item.get("tags") or []) or "rating" in (item.get("tags") or []):
            w *= 1.2
        weighted += float(item.get("score") or 0) * w
        for h in item.get("hits") or []:
            if h not in hits:
                hits.append(h)
    if weighted >= 1.5:
        bias = "long"
    elif weighted <= -1.5:
        bias = "short"
    else:
        bias = "neutral"
    return bias, int(round(weighted)), hits[:6]


def _news_needles(ticker: str, yf_symbol: str, name: str) -> list:
    needles = []
    for raw in (ticker, yf_symbol, name):
        if not raw:
            continue
        s = str(raw).strip()
        needles.append(s.lower())
        if "." in s:
            needles.append(s.split(".")[0].lower())
    # Kurznamen ohne Rechtsform
    if name:
        cleaned = re.sub(
            r"\b(ag|se|inc|corp|corporation|holdings?|ltd|plc|kgaa|sa)\b",
            "",
            name,
            flags=re.I,
        )
        cleaned = _NEWS_WS_RE.sub(" ", cleaned).strip().lower()
        if len(cleaned) >= 3:
            needles.append(cleaned)
    out = []
    for n in needles:
        if n and n not in out and n not in {"de", "us"}:
            out.append(n)
    return out


def _headline_matches(title: str, needles: list) -> bool:
    blob = _norm_news_title(title)
    if not blob:
        return False
    for n in needles:
        if len(n) <= 3:
            if re.search(rf"\b{re.escape(n)}\b", blob):
                return True
        elif n in blob:
            return True
    return False


def _parse_rss_bytes(raw: bytes) -> list:
    # Groessen-Deckel zusaetzlich zum (optionalen) defusedxml-Schutz: sehr grosse
    # Feeds werden verworfen, statt Speicher/CPU zu binden.
    if len(raw) > 2_000_000:
        return []
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", "&amp;", text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    items = []
    for node in root.iter():
        tag = node.tag.lower()
        if tag.endswith("item") or tag.endswith("entry"):
            def _child(name):
                for ch in node:
                    if ch.tag.lower().endswith(name):
                        return (ch.text or "").strip() or ch.attrib.get("href") or ch.attrib.get("url")
                return ""
            title = _child("title")
            link = _child("link") or _child("guid")
            pub = _child("pubdate") or _child("published") or _child("updated") or _child("date")
            desc = _child("description") or _child("summary")
            if title:
                items.append({
                    "title": html.unescape(title),
                    "url": link,
                    "published": pub,
                    "summary": html.unescape(re.sub(r"<[^>]+>", " ", desc or "")),
                })
    return items








def _dedupe_news(items: list) -> list:
    kept = []
    fps = []
    norms = []
    for item in items:
        fp = item.get("fp") or _news_fingerprint(item.get("title") or "")
        norm = _norm_news_title(item.get("title") or "")
        tokens = set(norm.split())
        dup = False
        if fp in fps:
            dup = True
        else:
            for prev in norms:
                prev_tok = set(prev.split())
                if not tokens or not prev_tok:
                    continue
                overlap = len(tokens & prev_tok) / max(1, min(len(tokens), len(prev_tok)))
                if overlap >= 0.82 and abs(len(norm) - len(prev)) < 40:
                    dup = True
                    break
        if dup:
            continue
        fps.append(fp)
        norms.append(norm)
        kept.append(item)
    return kept




# --- Volumen-/Momentumanalyse: Stock-/Marktvolumen, Price-Direction, VoltDesk-Signal ---

def classify_price_direction(price: Optional[float], levels: dict) -> dict:
    """Klassifiziert den aktuellen Kurs relativ zu den Vortagesniveaus (PDH/PDC/PDL).

    Liefert eine von sechs sich gegenseitig ausschließenden Situationen:
    Breakout, Breakout-Zone, bullish, neutral, bearish, Breakdown.
    """
    pdh = levels.get("prev_high")
    pdl = levels.get("prev_low")
    pdc = levels.get("prev_close")
    if price is None or pdh is None or pdl is None or pdc is None or pdh <= pdl:
        return {"code": "UNKNOWN", "label": "unbekannt", "emoji": "⚪"}

    # Dynamisches "nahe"-Band: 8% der Vortagesrange, mind. 0.15% des Kurses,
    # damit sowohl sehr ruhige als auch sehr volatile Titel sinnvoll erfasst werden.
    rng = pdh - pdl
    band = max(rng * 0.08, price * 0.0015)

    if price > pdh:
        return {"code": "BREAKOUT", "label": "Breakout", "emoji": "🔥"}
    if price >= pdh - band:
        return {"code": "BREAKOUT_ZONE", "label": "Breakout-Zone", "emoji": "⚠️"}
    if abs(price - pdc) <= band:
        return {"code": "NEUTRAL", "label": "neutral", "emoji": "⚪"}
    if price > pdc:
        return {"code": "BULLISH", "label": "bullish", "emoji": "🟢"}
    if price > pdl:
        return {"code": "BEARISH", "label": "bearish", "emoji": "🟠"}
    return {"code": "BREAKDOWN", "label": "Breakdown", "emoji": "🔴"}


def _volume_pct_indicator(pct: Optional[float]) -> dict:
    """Ampel-Indikator für ein Volumen-%-Verhältnis (heute vs. Ø der letzten 20 Tage)."""
    if pct is None:
        return {"emoji": "⚪", "label": "n/v"}
    if pct >= 150:
        return {"emoji": "🟢", "label": "stark erhöht"}
    if pct >= 100:
        return {"emoji": "🟢", "label": "erhöht"}
    if pct >= 70:
        return {"emoji": "🟡", "label": "normal"}
    return {"emoji": "🔴", "label": "niedrig"}


def _volume_trend(intra: pd.DataFrame) -> Optional[str]:
    """Steigt oder fällt das Volumen innerhalb der letzten Bars (5-Min-Kerzen)?"""
    if intra is None or "Volume" not in intra.columns or len(intra) < 6:
        return None
    vol = intra["Volume"].fillna(0)
    recent = float(vol.tail(3).mean())
    prior = float(vol.tail(6).head(3).mean())
    if prior <= 0:
        return None
    if recent > prior * 1.1:
        return "rising"
    if recent < prior * 0.9:
        return "falling"
    return "flat"


@st.cache_data(ttl=120, show_spinner=False)
def _market_volume_pct(ticker: str) -> Optional[float]:
    """Volumen des Referenzindex (ETF/Index) heute vs. dessen eigenen 20-Tage-Ø - als
    Näherung für 'Marktvolumen', ohne eine eigene Sektor-Zuordnung/-Cache-Ebene zu
    benötigen (Sektorvolumen wurde bewusst ausgeklammert)."""
    bench = _bench_for_ticker(ticker)
    if not bench:
        return None
    bl = fetch_levels(bench)
    today_vol = bl.get("today_volume")
    avg_vol = bl.get("avg_volume_20d")
    if today_vol is None or not avg_vol:
        return None
    return today_vol / avg_vol * 100.0


def compute_volume_momentum(ticker: str, intra: pd.DataFrame, levels: dict, price: Optional[float]) -> dict:
    """VoltDesk-Volumen-/Momentumanalyse: kombiniert Stock-, Marktvolumen, Volumen-Trend,
    Relative Volume und Price-Direction zu Momentum- (weak/neutral/strong) und
    Confirmation-Bewertung (low/neutral/high) plus einem kompakten Score für das
    Recommendation-Scoring in recommend_product()."""
    today_vol = levels.get("today_volume")
    avg_vol = levels.get("avg_volume_20d")
    stock_vol_pct = (today_vol / avg_vol * 100.0) if (today_vol is not None and avg_vol) else None
    rvol = (today_vol / avg_vol) if (today_vol is not None and avg_vol) else None
    market_vol_pct = _market_volume_pct(ticker) if ticker else None
    vtrend = _volume_trend(intra)
    direction = classify_price_direction(price, levels)

    stock_ind = _volume_pct_indicator(stock_vol_pct)
    market_ind = _volume_pct_indicator(market_vol_pct)

    directional = direction["code"] in {"BREAKOUT", "BREAKDOWN", "BULLISH", "BEARISH"}

    # Momentum: braucht sowohl Richtung im Kurs als auch Rückenwind im Volumen.
    momentum = "neutral"
    if directional and vtrend == "rising" and (rvol or 0) >= 1.3:
        momentum = "strong"
    elif (not directional) or vtrend == "falling" or (rvol is not None and rvol < 0.7):
        momentum = "weak"

    # Confirmation: bestätigen Aktien- UND Marktvolumen die Bewegung gemeinsam?
    stock_elevated = stock_vol_pct is not None and stock_vol_pct >= 100
    market_elevated = market_vol_pct is not None and market_vol_pct >= 100
    if stock_elevated and market_elevated:
        confirmation = "high"
    elif stock_elevated or market_elevated:
        confirmation = "neutral"
    else:
        confirmation = "low"

    return {
        "stock_volume_pct": stock_vol_pct,
        "stock_volume_indicator": stock_ind,
        "market_volume_pct": market_vol_pct,
        "market_volume_indicator": market_ind,
        "relative_volume": rvol,
        "volume_trend": vtrend,
        "price_direction": direction,
        "momentum": momentum,
        "confirmation": confirmation,
    }


def classify_setup_label(patterns=None, levels=None, price=None, side=None, source=None) -> str:
    """Automatisches Setup-Label aus der Engine.

    Werte: OR-Breakout, VWAP-Reclaim, VWAP-Pullback, PDH-Break, Pullback, Muster, Empfehlung, Manuell.
    """
    scored = []
    side_bias = None
    if side == "LONG":
        side_bias = "long"
    elif side == "SHORT":
        side_bias = "short"
    for p in patterns or []:
        if side_bias and p.get("bias") not in {side_bias, "neutral", None}:
            continue
        name = str(p.get("name") or "").lower()
        conf = int(p.get("confidence") or 0)
        if "first break" in name:
            scored.append((conf, "Muster"))
        elif "opening-range" in name or "opening range" in name:
            scored.append((conf, "OR-Breakout"))
        elif "vwap-pullback" in name or "vwap pullback" in name:
            scored.append((conf + 5, "VWAP-Pullback"))
        elif "vwap" in name:
            scored.append((conf, "VWAP-Reclaim"))
        elif "pdh" in name or "vortag-high" in name:
            scored.append((conf, "PDH-Break"))
        elif "pullback" in name or "retest" in name:
            scored.append((conf, "Pullback"))
        elif name:
            scored.append((conf, "Muster"))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]
    src = str(source or "").lower()
    if src in {"empfehlung", "rec", "empfehlungen"}:
        return "Empfehlung"
    if src in {"katalog", "nachkauf", "ticket", "manual", "manual-sell", "partial-sell", "quick-50", "quick-all"}:
        return "Manuell"
    levels = levels or {}
    try:
        px = float(price) if price else None
    except (TypeError, ValueError):
        px = None
    if px:
        pdh = levels.get("prev_high")
        vwap = levels.get("vwap")
        if pdh and px >= float(pdh):
            return "PDH-Break"
        if vwap:
            return "VWAP-Reclaim"
    return "Manuell"


def compute_setup_quality(confidence=None, patterns=None, volume_momentum=None, news=None, side=None) -> int:
    """Automatische Setup-Quality 0–100 aus Konfidenz, Volumen, Mustern und News."""
    q = 0.0
    try:
        if confidence is not None:
            q += min(40.0, float(confidence) * 0.44)
    except (TypeError, ValueError):
        pass
    best_pat = 0
    for p in patterns or []:
        try:
            best_pat = max(best_pat, int(p.get("confidence") or 0))
        except (TypeError, ValueError):
            continue
    q += min(25.0, best_pat * 0.25)
    vm = volume_momentum or {}
    confirmation = vm.get("confirmation")
    if confirmation == "high":
        q += 20.0
    elif confirmation == "neutral":
        q += 10.0
    try:
        rvol = vm.get("relative_volume")
        if rvol is not None and float(rvol) >= 1.5:
            q += 5.0
    except (TypeError, ValueError):
        pass
    if news:
        try:
            bias, score, _hits = score_news(news)
            aligned = (
                (side == "LONG" and bias == "long")
                or (side == "SHORT" and bias == "short")
            )
            if aligned:
                q += min(15.0, abs(int(score or 0)) * 4.0)
            elif bias in {"long", "short"}:
                q += 0.0
            else:
                q += 4.0
        except Exception:
            pass
    return int(max(0, min(100, round(q))))


def compute_sector_relative(ticker: Optional[str], price: Optional[float] = None, levels: Optional[dict] = None, watch: Optional[pd.DataFrame] = None) -> dict:
    """Gleichgewichtete 1D-% der SECTOR_MAP-Peers vs. Titel."""
    out = {
        "sector": None, "ticker_chg": None, "sector_chg": None,
        "rel_pct": None, "peers": 0, "status": "n/a",
    }
    if not ticker:
        return out
    out["sector"] = get_sector(ticker)
    ticker_chg = None
    if watch is not None and not getattr(watch, "empty", True):
        hit = watch[watch["Ticker"] == ticker]
        if not hit.empty and hit.iloc[0].get("1D %") is not None:
            ticker_chg = _as_float(hit.iloc[0]["1D %"])
    if ticker_chg is None and levels and levels.get("prev_close") and price:
        prev = _as_float(levels.get("prev_close"))
        if prev:
            ticker_chg = (float(price) - prev) / prev * 100.0
    if ticker_chg is None:
        meta = find_meta(ticker)
        if meta:
            q = fetch_quote(meta["yf"])
            ticker_chg = _as_float((q or {}).get("chg"))
    out["ticker_chg"] = ticker_chg
    peers = [t for t, sec in SECTOR_MAP.items() if sec == out["sector"] and t != ticker]
    peer_chgs = []
    seen = set()
    if watch is not None and not getattr(watch, "empty", True):
        for _, row in watch.iterrows():
            t = row.get("Ticker")
            if t in peers and t not in seen and row.get("1D %") is not None:
                val = _as_float(row.get("1D %"))
                if val is not None:
                    peer_chgs.append(val)
                    seen.add(t)
    if len(peer_chgs) < 2:
        for t in peers:
            if t in seen:
                continue
            meta = find_meta(t)
            if not meta:
                continue
            q = fetch_quote(meta["yf"])
            val = _as_float((q or {}).get("chg"))
            if val is not None:
                peer_chgs.append(val)
                seen.add(t)
            if len(peer_chgs) >= 8:
                break
    out["peers"] = len(peer_chgs)
    if peer_chgs:
        out["sector_chg"] = sum(peer_chgs) / len(peer_chgs)
    if out["ticker_chg"] is not None and out["sector_chg"] is not None:
        out["rel_pct"] = float(out["ticker_chg"]) - float(out["sector_chg"])
        if out["rel_pct"] >= 0.15:
            out["status"] = "bestätigt"
        elif out["rel_pct"] <= -0.15:
            out["status"] = "divergiert"
        else:
            out["status"] = "neutral"
    return out


def recommend_product(intra, levels, news, events, mode: str, price: Optional[float], patterns: list = None, venue: str = "Xetra", ticker: Optional[str] = None):
    reasons = []
    if mode in {"PRE", "CLOSE"}:
        return {
            "action": "WAIT",
            "side": None,
            "product": None,
            "confidence": 0,
            "reasons": [f"Session {mode}: keine neue Empfehlung."],
        }
    if not price:
        return {
            "action": "WAIT",
            "side": None,
            "product": None,
            "confidence": 0,
            "reasons": ["Kein Kurs."],
        }

    long_pts = 0
    short_pts = 0
    if patterns is None:
        patterns = detect_intraday_patterns(intra, levels, mode, venue) if intra is not None else []
    for p in patterns:
        conf = p.get("confidence", 50)
        pts = max(1, int(conf / 25))  # 40-64 = 1pt, 65-89 = 2pt, 90+ = 3pt
        if p["bias"] == "long":
            long_pts += pts
            reasons.append(f"Chart [{conf}%]: {p['name']}")
        elif p["bias"] == "short":
            short_pts += pts
            reasons.append(f"Chart [{conf}%]: {p['name']}")

    vwap = levels.get("vwap")
    if vwap:
        if price > vwap:
            long_pts += 1
            reasons.append("Kurs über VWAP")
        else:
            short_pts += 1
            reasons.append("Kurs unter VWAP")

    or_h, or_l = _opening_range(intra) if intra is not None and not intra.empty else (None, None)
    if or_h and price > or_h:
        long_pts += 1
        reasons.append("Über Opening Range")
    if or_l and price < or_l:
        short_pts += 1
        reasons.append("Unter Opening Range")

    # VoltDesk-Volumen-/Momentumsignal: StockVolume%, MarketVolume%, Volume-Trend,
    # Relative Volume und Price-Direction gemeinsam bewertet. Fließt nur mit Gewicht
    # ein, wenn Momentum UND Confirmation übereinstimmend stark/hoch sind - eine reine
    # Volumenspitze ohne Bestätigung soll keinen Trade allein auslösen.
    vol_mom = compute_volume_momentum(ticker, intra, levels, price)
    direction_code = vol_mom["price_direction"]["code"]
    if vol_mom["momentum"] == "strong" and vol_mom["confirmation"] in {"neutral", "high"}:
        weight = 2 if vol_mom["confirmation"] == "high" else 1
        if direction_code in {"BREAKOUT", "BULLISH"}:
            long_pts += weight
            reasons.append(
                f"Volumen bestätigt Breakout/Aufwärtsbewegung "
                f"(Momentum {vol_mom['momentum']}, Confirmation {vol_mom['confirmation']})"
            )
        elif direction_code in {"BREAKDOWN", "BEARISH"}:
            short_pts += weight
            reasons.append(
                f"Volumen bestätigt Breakdown/Abwärtsbewegung "
                f"(Momentum {vol_mom['momentum']}, Confirmation {vol_mom['confirmation']})"
            )
    elif vol_mom["momentum"] == "weak" and direction_code in {"BREAKOUT", "BREAKDOWN"}:
        reasons.append("Kursbewegung ohne Volumen-Bestätigung — Vorsicht vor Fehlausbruch")

    # Sektor-Konfirmation als Fake-Breakout-Filter: bestätigt der Sektor (SPDR-ETF)
    # die Richtung eines Breakouts/Breakdowns? Ohne Bestätigung ist die Bewegung
    # häufiger nur Einzeltitel-getrieben und damit anfälliger für einen Fehlausbruch -
    # daher kein zusätzlicher Score-Abzug (das wäre zu hart für ein einzelnes Signal),
    # aber ein expliziter Warnhinweis in den Reasons. Bei Bestätigung gibt es dagegen
    # einen kleinen Bonus-Punkt, da eine breiter getragene Bewegung tendenziell robuster ist.
    if direction_code in {"BREAKOUT", "BREAKDOWN"} and ticker:
        sec_conf = sector_confirmation(ticker, "LONG" if direction_code == "BREAKOUT" else "SHORT")
        if sec_conf["available"]:
            if sec_conf["confirmed"]:
                if direction_code == "BREAKOUT":
                    long_pts += 1
                else:
                    short_pts += 1
                reasons.append(
                    f"Sektor ({sec_conf['sector']}) bestätigt die Richtung "
                    f"({sec_conf['sector_chg']:+.2f}%)"
                )
            else:
                reasons.append(
                    f"⚠️ Sektor ({sec_conf['sector']}) bestätigt NICHT "
                    f"({sec_conf['sector_chg']:+.2f}%) — Fake-Breakout-Risiko"
                )

    news_bias, news_score, news_hits = score_news(news)
    if news_bias == "long":
        long_pts += min(2, abs(news_score))
        reasons.append("News eher positiv: " + ", ".join(news_hits) if news_hits else "News eher positiv")
    elif news_bias == "short":
        short_pts += min(2, abs(news_score))
        reasons.append("News eher negativ: " + ", ".join(news_hits) if news_hits else "News eher negativ")
    else:
        reasons.append("News ohne klare Richtung")

    if ticker is None:
        try:
            ticker = st.session_state.focus
        except Exception:
            ticker = None
    sector_ctx = compute_sector_relative(ticker, price, levels)
    rel = sector_ctx.get("rel_pct")
    if rel is not None:
        if rel >= 0.15:
            long_pts += 1
            reasons.append(f"Sektor bestätigt Long (vs. {sector_ctx.get('sector')} {rel:+.2f}%)")
        elif rel <= -0.15:
            short_pts += 1
            reasons.append(f"Sektor bestätigt Short (vs. {sector_ctx.get('sector')} {rel:+.2f}%)")
        else:
            reasons.append(f"Sektor neutral (vs. {sector_ctx.get('sector')} {rel:+.2f}%)")

    earnings_soon = False
    if events.get("earnings"):
        try:
            when = pd.to_datetime(events["earnings"], utc=True, errors="coerce")
            if pd.notna(when):
                hours = (when - pd.Timestamp.now(tz="UTC")).total_seconds() / 3600
                if -6 <= hours <= 24:
                    earnings_soon = True
                    reasons.append("Earnings nahe — Hebel drosseln")
        except Exception:
            pass

    if long_pts == short_pts:
        return {
            "action": "WAIT",
            "side": None,
            "product": None,
            "confidence": max(long_pts, short_pts),
            "reasons": reasons + ["Long- und Short-Punkte gleich — kein Trade."],
            "volume_momentum": vol_mom,
            "sector": sector_ctx,
        }

    side = "LONG" if long_pts > short_pts else "SHORT"
    edge = abs(long_pts - short_pts)
    atr = levels.get("atr")
    atr_pct = (atr / price * 100) if atr else 2.0
    if earnings_soon or atr_pct > 5:
        target_lev = 3.0
        reasons.append("Konservativer Hebel wegen Risiko/Vol")
    elif edge >= 4 and atr_pct < 3:
        target_lev = 6.5
    elif edge >= 3:
        target_lev = 5.0
    else:
        target_lev = 4.0
    if mode == "MID":
        target_lev = min(target_lev, 5.0)
        reasons.append("Mid-Session: kein Maximalhebel")

    ladder = live_catalog(ticker, price)
    if ladder.empty:
        ladder = build_derivative_ladder(price, side, atr, levels.get("spread_pct"))
    if not ladder.empty and "Seite" in ladder.columns:
        ladder = ladder[ladder["Seite"] == side]
    suitable = ladder[ladder["Eignung"] == "geeignet"].copy() if not ladder.empty else pd.DataFrame()
    if suitable.empty:
        return {
            "action": "WAIT",
            "side": side,
            "product": None,
            "confidence": edge,
            "reasons": reasons + ["Kein geeignetes Produkt im Katalog."],
        }
    suitable["lev_gap"] = (suitable["Hebel"] - target_lev).abs()
    pick = suitable.sort_values(["lev_gap", "Hebel"]).iloc[0]
    confidence = min(90, 40 + edge * 12)
    setup_label = classify_setup_label(patterns, levels, price, side, source="empfehlung")
    setup_quality = compute_setup_quality(
        confidence=confidence,
        patterns=patterns,
        volume_momentum=vol_mom,
        news=news,
        side=side,
    )
    reasons.append(f"Setup-Label: {setup_label} · Quality {setup_quality}")
    return {
        "action": "BUY",
        "side": side,
        "product": pick.to_dict(),
        "target_lev": target_lev,
        "confidence": confidence,
        "reasons": reasons,
        "scores": {"long": long_pts, "short": short_pts},
        "volume_momentum": vol_mom,
        "setup_label": setup_label,
        "setup_quality": setup_quality,
        "sector": sector_ctx,
    }


def _confirmed_swing_trail(pos: dict, lookback: int = TRAIL_SWING_LOOKBACK,
                            buffer_pct: float = TRAIL_SWING_BUFFER_PCT):
    """Regelbasiertes Nachziehen: schlägt einen neuen Stop NUR dann vor, wenn seit
    Kauf eine bestätigte neue Marktstruktur entstanden ist (Higher Low bei LONG,
    Lower High bei SHORT) - nicht bei jedem einzelnen Kurs-Update.

    Vorher (Naiver Ansatz): trail = max(stop, entry, vwap) wurde bei JEDEM Aufruf neu
    berechnet und zog den Stop damit im Prinzip bei jeder Kursbewegung nach oben mit.
    Das kann gerade bei gehebelten Produkten gefährlich sein: ein zu eng/zu früh
    nachgezogener Stop wirft eine eigentlich intakte Position durch normales
    Intraday-Rauschen aus dem Trade.

    Ein Swing-Punkt gilt erst als bestätigt, wenn `lookback` Bars auf BEIDEN Seiten
    vorliegen (Fraktal-Muster) - der jeweils letzte `lookback`-Block von Bars bleibt
    also immer unbestätigt und wird ignoriert, damit nicht der letzte, noch nicht
    abgeschlossene Tick fälschlich als Struktur-Wende gewertet wird.

    Gibt None zurück, wenn (noch) keine neue, bestätigte Struktur zugunsten der
    Position vorliegt - der Aufrufer soll dann den bestehenden Stop unverändert
    lassen, statt ihn ohne Anlass zu verschieben.
    """
    side = pos.get("side")
    ticker = pos.get("ticker")
    entry_iso = pos.get("entry_at")
    stop = pos.get("stop")
    if not ticker or not entry_iso or stop is None:
        return None
    meta = find_meta(ticker)
    yf_symbol = meta.get("yf") if meta else None
    if not yf_symbol:
        return None
    try:
        entry_dt = datetime.fromisoformat(entry_iso).astimezone(timezone.utc)
        intra = fetch_intraday(yf_symbol)
        if intra is None or intra.empty:
            return None
        idx = pd.to_datetime(intra.index, utc=True)
        post = intra.loc[idx >= entry_dt]
        n = len(post)
        if n < (2 * lookback + 1):
            return None  # zu wenig Bars seit Entry für eine bestätigte Struktur

        highs = pd.to_numeric(post.get("High"), errors="coerce")
        lows = pd.to_numeric(post.get("Low"), errors="coerce")
        swing_col = lows if side == "LONG" else highs

        confirmed = []  # [(bar_index, level)] in chronologischer Reihenfolge
        for i in range(lookback, n - lookback):
            v = swing_col.iloc[i]
            if pd.isna(v):
                continue
            window = swing_col.iloc[i - lookback: i + lookback + 1]
            if window.isna().any():
                continue
            if side == "LONG" and v == window.min() and (window == v).sum() == 1:
                confirmed.append((i, float(v)))
            elif side == "SHORT" and v == window.max() and (window == v).sum() == 1:
                confirmed.append((i, float(v)))

        if len(confirmed) < 2:
            return None  # noch keine zwei bestätigten Swing-Punkte -> keine Strukturaussage möglich

        prev_level = confirmed[-2][1]
        last_i, last_level = confirmed[-1]
        is_new_structure = (last_level > prev_level) if side == "LONG" else (last_level < prev_level)
        if not is_new_structure:
            return None  # letzter bestätigter Punkt ist kein Higher Low / Lower High

        buffer = abs(last_level) * buffer_pct
        candidate = (last_level - buffer) if side == "LONG" else (last_level + buffer)

        cur_stop = float(stop)
        # Ein Trailing-Stop darf das Risiko nur reduzieren, nie wieder lockern.
        if side == "LONG" and candidate <= cur_stop:
            return None
        if side == "SHORT" and candidate >= cur_stop:
            return None

        swing_ts = post.index[last_i]
        label = "Higher Low" if side == "LONG" else "Lower High"
        return {
            "level": candidate,
            "swing_price": last_level,
            "swing_ts": swing_ts,
            "structure_label": label,
        }
    except Exception:
        # Datenprobleme dürfen nie einen Stop-Vorschlag erzwingen - im Zweifel lieber
        # gar keinen Trail vorschlagen, als einen auf Basis kaputter Daten.
        return None


def send_alert(message: str, alert_type: str = "info"):
    """Zentrale Alert-Funktion für alle Benachrichtigungen (st.toast) - alle Alert-Typen
    unten (Kurs, Stop-Nähe, Tages-P&L, Earnings, Setup, KO-Nähe) laufen hier zusammen.
    Nur In-App (kein Push/E-Mail) und nicht persistent - erscheint nur beim Rerun,
    in dem die Bedingung erfüllt ist."""
    icon_map = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "❌"}
    st.toast(f"{icon_map.get(alert_type, 'ℹ️')} {message}", icon=icon_map.get(alert_type))


def check_price_alerts(ticker: str, price: float, levels: dict):
    """Preislevel-Alert: PDH, Opening-Range-High, nächste runde Zahl."""
    if price is None or not ticker:
        return
    last_alert_price = st.session_state.get(f"_last_alert_price_{ticker}", 0)
    pdh = levels.get("prev_high")
    if pdh and price >= pdh and last_alert_price < pdh:
        send_alert(f"{ticker}: PDH erreicht! ({price:.2f} €)", "success")
        st.session_state[f"_last_alert_price_{ticker}"] = price
        return
    or_high = levels.get("or_high")
    if or_high and price >= or_high and last_alert_price < or_high:
        send_alert(f"{ticker}: Opening Range High erreicht! ({price:.2f} €)", "warning")
        st.session_state[f"_last_alert_price_{ticker}"] = price
        return
    round_level = round(price / 10) * 10
    if round_level > price and last_alert_price < round_level and abs(price - round_level) < 0.5:
        send_alert(f"{ticker}: Nächste runde Zahl bei {round_level:.0f} €", "info")
        st.session_state[f"_last_alert_price_{ticker}"] = price


def check_stop_alerts(pos: dict, price: float):
    """Stop-Loss-Nähe-Warnung (< 1% Abstand, einmal pro Stop-Level)."""
    stop = pos.get("stop")
    if not stop or price is None or price <= 0:
        return
    ticker = pos.get("ticker")
    dist_pct = abs(price - stop) / price * 100
    if dist_pct < 1:
        last_warn = st.session_state.get(f"_stop_alert_{ticker}", 0)
        if last_warn != stop:
            send_alert(f"Stop-Loss bei {ticker} ist nur noch {dist_pct:.2f}% entfernt!", "warning")
            st.session_state[f"_stop_alert_{ticker}"] = stop


def check_pnl_alerts(day_pnl: float, equity: float, day_pct: float):
    """Tages-P&L-Warnung bei -1% / -1,5% / -2% - je Schwelle nur einmal pro Tag."""
    shown = st.session_state.setdefault("_pnl_alert_shown", set())
    if day_pct <= -2.0 and "kill" not in shown:
        send_alert(f"KILL SWITCH: Tagesverlust -{abs(day_pct):.2f}% erreicht!", "error")
        shown.add("kill")
    elif day_pct <= -1.5 and "1.5" not in shown:
        send_alert(f"Tagesverlust: {day_pct:.2f}% - Risiko steigt!", "error")
        shown.add("1.5")
    elif day_pct <= -1.0 and "1.0" not in shown:
        send_alert(f"Tagesverlust: {day_pct:.2f}% (-{abs(day_pnl):.0f} €) - Vorsicht!", "warning")
        shown.add("1.0")


@st.dialog("📅 Earnings-Termin steht bevor", width="large")
def _earnings_alert_dialog(ticker: str, when_str: str, is_today: bool):
    when_desc = "heute" if is_today else "morgen"
    st.warning(
        f"**{ticker}** veröffentlicht {when_desc} Quartalszahlen ({when_str}).\n\n"
        "Nach Earnings-Veröffentlichungen kommt es häufig zu großen, teils "
        "unvorhersehbaren Kurskorrekturen — deutlich stärker als die normale "
        "Tagesschwankung. Bestehende Positionen und geplante Trades in diesem "
        "Titel sollten das Timing berücksichtigen."
    )
    st.caption(
        "Regelbasierte Terminerinnerung, kein Handelssignal. Erscheint einmal pro Earnings-Termin."
    )
    if st.button("Verstanden", type="primary", use_container_width=True, key="earnings_alert_ack"):
        st.rerun()


def _maybe_show_earnings_alert_dialog(ticker: str, events: dict):
    """Zeigt einmalig ein Popup, wenn für den Fokus-Titel ein Earnings-Termin am
    selben oder am folgenden Kalendertag ansteht (2026-09, auf Wunsch ergänzt).
    Ergänzt check_earnings_alerts() (nur st.toast, 24h/2h-Fenster, verschwindet
    von selbst) um eine deutlichere, modale Warnung, die aktiv bestätigt werden
    muss - bewusst NICHT an Handelszeiten gebunden (anders als die Drei-rote-
    Kerzen-Warnung), weil ein Heads-up VOR Handelsbeginn hier gerade der Punkt ist."""
    earnings = events.get("earnings")
    if not earnings or not ticker:
        return
    try:
        when = pd.to_datetime(earnings, utc=True, errors="coerce")
    except Exception:
        return
    if pd.isna(when):
        return
    now_berlin = datetime.now(TZ_BERLIN)
    when_berlin = when.tz_convert(TZ_BERLIN)
    days_until = (when_berlin.date() - now_berlin.date()).days
    if days_until not in (0, 1):
        return
    shown = st.session_state.setdefault("earnings_popup_shown", {})
    if shown.get(ticker) == str(earnings):
        return
    shown[ticker] = str(earnings)
    _earnings_alert_dialog(ticker, when_berlin.strftime("%d.%m.%Y %H:%M"), days_until == 0)


def check_earnings_alerts(ticker: str, events: dict):
    """Earnings-Erinnerung innerhalb der nächsten 24h / 2h."""
    earnings = events.get("earnings")
    if not earnings or not ticker:
        return
    try:
        when = pd.to_datetime(earnings, utc=True, errors="coerce")
        if pd.isna(when):
            return
        now = pd.Timestamp.now(tz="UTC")
        hours = (when - now).total_seconds() / 3600
        last_alert = st.session_state.get(f"_earnings_alert_{ticker}", "")
        if last_alert == str(earnings):
            return
        if 0 <= hours <= 2:
            send_alert(f"{ticker}: Earnings in weniger als {int(hours)} Stunden!", "warning")
            st.session_state[f"_earnings_alert_{ticker}"] = str(earnings)
        elif 2 < hours <= 24:
            send_alert(f"{ticker}: Earnings in {int(hours)} Stunden - Volatilität erwartet", "info")
            st.session_state[f"_earnings_alert_{ticker}"] = str(earnings)
    except Exception:
        pass


def check_setup_alerts(ticker: str, rec: dict, venue: str = "US Regular"):
    """Setup-Alert bei klarer Kauf-Empfehlung mit hoher Konfidenz (>=75%).

    Die Empfehlung darf auch in PRE/CLOSE berechnet werden, aber die reine
    Setup-Erkannt-Meldung ist außerhalb der tatsächlichen Handelszeit eines
    Titels überflüssig und wird deshalb unterdrückt.
    """
    if not rec or not ticker:
        return
    now = datetime.now(TZ_BERLIN)
    if now.weekday() >= 5 or is_market_holiday(venue, now.date()):
        return
    windows = session_windows(venue)
    t = now.time()
    in_session = any(start <= t < end for start, end in (windows["open"], windows["mid"], windows["close"]))
    if not in_session:
        return
    if str(rec.get("action", "")).upper() == "BUY" and float(rec.get("confidence", 0) or 0) >= 75:
        side = rec.get("side", "LONG")
        marker = f"{side}_{rec.get('confidence')}"
        last_alert = st.session_state.get(f"_setup_alert_{ticker}", "")
        if last_alert != marker:
            send_alert(
                f"{ticker}: {side}-Setup erkannt (Konfidenz {float(rec['confidence']):.0f}%) - Einstieg prüfen!",
                "success",
            )
            st.session_state[f"_setup_alert_{ticker}"] = marker


def check_ko_proximity(pos: dict, price: float) -> dict:
    """Berechnet den Abstand zur echten KO-Schwelle (pos['ko'], nicht der Paper-Stop)
    und liefert eine Warnstufe. Schwellen im Pro Modus über die Sidebar (Risiko-
    Bereich) konfigurierbar, sonst Standardwerte (Warnung 5%, Kritisch 3%)."""
    ko = pos.get("ko")
    if not ko or price is None or price <= 0:
        return {"distance_pct": None, "level": "unknown", "warning": False}
    warning_threshold = float(st.session_state.get("ko_warning_threshold", 5.0))
    critical_threshold = float(st.session_state.get("ko_critical_threshold", 3.0))
    side = pos.get("side", "LONG")
    dist_pct = (price - ko) / price * 100 if side == "LONG" else (ko - price) / price * 100
    if dist_pct < 0:
        return {"distance_pct": dist_pct, "level": "knocked", "warning": True}
    elif dist_pct < critical_threshold:
        return {"distance_pct": dist_pct, "level": "critical", "warning": True}
    elif dist_pct < warning_threshold:
        return {"distance_pct": dist_pct, "level": "warning", "warning": True}
    elif dist_pct < 10:
        return {"distance_pct": dist_pct, "level": "notice", "warning": False}
    else:
        return {"distance_pct": dist_pct, "level": "safe", "warning": False}


def check_ko_alerts(pos: dict, price: float):
    """Toast-Alert bei kritischer KO-Nähe oder Knock-Out (einmal pro Level/Position)."""
    status = check_ko_proximity(pos, price)
    ticker = pos.get("ticker", "?")
    if status["level"] not in ("critical", "knocked"):
        return
    last_level = st.session_state.get(f"_ko_alert_{ticker}", "")
    if last_level == status["level"]:
        return
    if status["level"] == "knocked":
        send_alert(f"{ticker}: KO-Schwelle wurde erreicht! Position ist ausgeknockt.", "error")
    else:
        send_alert(f"{ticker}: KO-Schwelle nur noch {status['distance_pct']:.1f}% entfernt!", "error")
    st.session_state[f"_ko_alert_{ticker}"] = status["level"]




def build_day_report() -> dict:
    fills = list(st.session_state.get("fills") or [])
    closed = [f for f in fills if f.get("action") != "Open"]
    opens_list = [f for f in fills if f.get("action") == "Open"]
    off_plan_opens = [f for f in opens_list if f.get("off_plan")]
    pnls = [float(f.get("pnl") or 0) for f in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    actual_fees = float(st.session_state.get("fees_paid") or 0)
    fee_unit = float(st.session_state.get("fee_per_trade", FEE_PER_TRADE) if effective_pro_mode() else FEE_PER_TRADE)
    open_n = len(st.session_state.get("positions") or [])
    projected_fees = fee_unit * open_n  # noch nicht belastete Exit-Gebühren
    gross_pnl = (
        float(st.session_state.get("closed_pnl") or 0)
        + sum(float(p.get("pnl") or 0) for p in st.session_state.get("positions") or [])
    )
    net_pnl = gross_pnl - actual_fees
    return {
        "time": datetime.now(TZ_BERLIN).strftime("%Y-%m-%d %H:%M"),
        "fills": fills,
        "trades": len(closed),
        "opens": len(opens_list),
        "gross_pnl": gross_pnl,
        "fees": actual_fees,
        "projected_fees": projected_fees,
        "net_pnl": net_pnl,
        "day_pnl": net_pnl,
        "day_pct": _safe_pct(net_pnl, current_capital()),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(pnls) * 100) if pnls else 0.0,
        "best": max(pnls) if pnls else 0.0,
        "worst": min(pnls) if pnls else 0.0,
        "open_positions": len(st.session_state.get("positions") or []),
        # Tages-Setup-Vertrag (2026-09): Snapshot + Off-Plan-Quote, damit sich über
        # mehrere Tage auswerten lässt, ob der Vertrag überhaupt greift.
        "daily_setup": (st.session_state.get("daily_setup_contract") or {}).get("setup"),
        "daily_setup_max_risk_eur": (st.session_state.get("daily_setup_contract") or {}).get("max_risk_eur"),
        "off_plan_trades": len(off_plan_opens),
        "on_plan_rate": (
            100.0 * (len(opens_list) - len(off_plan_opens)) / len(opens_list)
        ) if opens_list else None,
    }



def _reset_paper_day_state():
    """Setzt alle Felder eines Paper-Trading-Tages zurück (Positionen, Fills, P&L,
    Kill-Switch, Empfehlungs-Caches, ...).

    Gemeinsamer Kern für den automatischen Tageswechsel (maybe_start_new_paper_day)
    UND den manuellen "Neuen Paper-Tag starten"-Button. Beide riefen früher denselben
    Block aus session_state-Zuweisungen unabhängig voneinander auf - das ist fehleranfällig,
    weil eine künftige Änderung (z.B. ein neues Feld) leicht an nur einer der beiden Stellen
    nachgezogen wird und die beiden Reset-Pfade dann stillschweigend auseinanderlaufen.
    """
    # Cash-Übertrag: Der Kontostand zum Ende des jetzt endenden Tages wird zur neuen
    # Kapitalbasis für den kommenden Tag (current_capital()/account_base) - vorher
    # sprang das Konto bei jedem neuen Paper-Tag unabhängig vom bisherigen Ergebnis
    # wieder auf die fixen 10.000 € zurück, Gewinn/Verlust ging also "verloren".
    # Wichtig: offene Positionen sollten VOR diesem Aufruf bereits geschlossen sein
    # (maybe_start_new_paper_day() ruft dafür flatten_all() auf) - andernfalls würde
    # ihr investierter Betrag hier von der Kapitalbasis abgezogen, ohne dass ihr
    # realisierter Gewinn/Verlust je erfasst wurde.
    st.session_state.account_base = cash_available()
    st.session_state.positions = []
    st.session_state.fills = []
    st.session_state.closed_pnl = 0.0
    st.session_state.killed = False
    st.session_state.risk_lock = False
    st.session_state.day_report = None
    st.session_state.taken_recs = []
    st.session_state.fees_paid = 0.0
    st.session_state.rec_cache = {}
    st.session_state.last_rec_time = {}
    # Tages-Guardrails (Trade-Cap, Cooldown, Risiko-Budget) laufen täglich neu an.
    st.session_state.trades_opened_today = 0
    st.session_state.last_loss_close_at = None
    st.session_state.risk_used_today_eur = 0.0
    st.session_state.tilt_lock = False
    st.session_state.tilt_lock_reason = None
    st.session_state.tilt_lock_score = None
    st.session_state.pending_close_notes = []
    # Tages-Setup-Vertrag ist genau das - EIN Vertrag für den jetzt endenden Tag,
    # muss also täglich neu geschlossen werden. tilt_unlock_log/daily_setup_change_log
    # bleiben bewusst über den Reset hinweg erhalten (Audit-Trail über mehrere Tage).
    st.session_state.daily_setup_contract = None


def maybe_start_new_paper_day(watch: pd.DataFrame):
    """Erkennt einen Kalendertagwechsel (Berlin-Zeit) und startet automatisch einen neuen
    Paper-Tag, damit Trades vom Vortag nicht im aktuellen Tagesbericht auftauchen. Vorher
    musste "Neuen Paper-Tag starten" jeden Tag manuell geklickt werden - vergaß man das,
    liefen closed_pnl/fees/fills/Tagesbericht einfach über den Tageswechsel hinweg weiter.
    """
    today_str = datetime.now(TZ_BERLIN).date().isoformat()
    if st.session_state.get("day_started") == today_str:
        return  # Für heute schon initialisiert, nichts zu tun

    had_activity = bool(st.session_state.get("fills")) or bool(st.session_state.get("positions"))
    is_first_ever_start = st.session_state.get("day_started") is None

    if not is_first_ever_start and had_activity:
        # Es gab einen Vortag mit Aktivität, der nie manuell über "Handelstag abschließen"
        # beendet wurde -> vor dem Zurücksetzen noch einen Tagesbericht für den Vortag
        # sichern, damit diese Daten nicht kommentarlos verloren gehen.
        if st.session_state.positions:
            flatten_all(watch, reason="day-change")
        old_report = build_day_report()
        old_report["date"] = st.session_state.get("day_started")
        save_day_report(old_report)

    _reset_paper_day_state()
    st.session_state.day_started = today_str
    save_app_state()
    if not is_first_ever_start and had_activity:
        st.session_state["flatten_flash"] = (
            "Neuer Handelstag erkannt: Der Tagesbericht des Vortags wurde gesichert "
            "(Wochen-/Monatsbericht) und ein frischer Paper-Tag automatisch gestartet."
        )








def find_meta(ticker: str):
    if not ticker:
        return None
    for row in st.session_state.watchlist:
        if row["ticker"] == ticker:
            return row
    return catalog_lookup(ticker)














ATR_STOP_MULTIPLIER = 1.5  # Stop-Distanz = 1.5x ATR(14), Basis für dynamische Stops
# War 0.5 - für Daytrading ungewöhnlich eng, hat Positionen im normalen Intraday-
# Rauschen ausgestoppt statt der Bewegung Raum zu geben. 1.5-2.0x ATR ist der
# übliche Bereich; €-Risiko pro Trade bleibt trotz breiterem Stop unverändert,
# da calc_position_size() die Positionsgröße per R-Sizing gegenläufig anpasst
# (risk_budget / (leverage * stop_pct)) - nur die Positionsgröße (und damit auch
# der €-Gewinn bei Erreichen des kursbasierten Take-Profit-Ziels) sinkt mit.
DEFAULT_STOP_PCT = 0.03  # Fallback, falls kein ATR verfügbar ist (z.B. Datenausfall)






















def _build_watchlist_core(
    watchlist_json: str,
    high_vol_only: bool,
    threshold: float,
    region_filter: str,
    sector_filter: str,
):
    """Gecachte Kernlogik von build_watchlist().

    Bewusst ohne Zugriff auf st.session_state: st.cache_data cached anhand der
    Funktionsargumente, daher muss alles, was das Ergebnis beeinflusst (Watchlist-
    Inhalt, High-Vol-Filter/-Schwelle, Region-/Sektor-Filter), als Argument reinkommen.
    fetch_quote() selbst ist bereits mit ttl=60 gecacht - dieser zusätzliche Cache (30s)
    spart vor allem die Schleife über find_region_index()/get_sector() und den
    DataFrame-Aufbau, die sonst bei jedem Rerun erneut liefen, auch wenn sich an der
    Watchlist gar nichts geändert hat.
    """
    watchlist = json.loads(watchlist_json)
    rows = []
    hidden = 0
    failed_quotes = []
    # Parallelisiert (2026-09): Filterung (Region/Sektor, kein Netzwerk-Call) läuft
    # zuerst sequenziell, die teuren fetch_quote()-Calls dann parallel nur noch für
    # die tatsächlich verbleibenden Titel - vorher sequenziell pro Titel in dieser
    # Schleife selbst.
    filtered_items = []
    for item in watchlist:
        region, idx_name = find_region_index(item["ticker"])
        sector = get_sector(item["ticker"])
        if region_filter != "ALL" and region != region_filter:
            continue
        if sector_filter != "ALL" and sector != sector_filter:
            continue
        filtered_items.append((item, region, idx_name, sector))

    quotes = _parallel_map(lambda t: fetch_quote(t[0]["yf"]), filtered_items)
    # Volumen/Market Cap/52W-Tief kommen aus fetch_levels() - das ruft die App an
    # anderer Stelle (Chart, Ranker) für dieselben Titel ohnehin schon auf; hier
    # zusätzlich parallelisiert aufgerufen, kein neuer Datenweg/API-Endpunkt.
    levels_list = _parallel_map(lambda t: fetch_levels(t[0]["yf"]), filtered_items)

    for (item, region, idx_name, sector), q, lv in zip(filtered_items, quotes, levels_list):
        if q is None:
            q = {"price": None, "chg": None, "high": None, "low": None, "vol_1y": None, "ok": False}
        if lv is None:
            lv = {}
        if not q.get("ok"):
            # fetch_quote() liefert bei einem Fehler weiterhin einen Platzhalter mit
            # price=None zurück (kein Absturz) - aber bisher wurde das komplett
            # stillschweigend hingenommen; der Titel erschien einfach mit leeren
            # Kurs-/Prozent-Feldern in der Tabelle. Jetzt wird das gesammelt und als
            # Warnung angezeigt.
            failed_quotes.append(item["ticker"])
        vol = q["vol_1y"]
        high = bool(vol is not None and vol >= threshold)
        # High-Vol-Filter deaktiviert (s. Begründung an der Sidebar-Toggle-Stelle) -
        # bisheriges Ausblenden von Titeln unterhalb der Vola-Schwelle entfällt,
        # alle Titel der Watchlist werden jetzt unabhängig von vol_1y angezeigt.
        # if high_vol_only and not high:
        #     # Titel bleibt in st.session_state.watchlist (bleibt also "vorhanden" und wird
        #     # bei erneutem Hinzufügen korrekt als Duplikat erkannt), erscheint aber wegen
        #     # des Filters nicht in der angezeigten Tabelle. Vorher passierte das lautlos -
        #     # das wirkte wie ein fehlgeschlagenes Hinzufügen, war aber "nur" der Filter.
        #     hidden += 1
        #     continue
        # Volumen: heutiges Intraday-Volumen bis jetzt, außerhalb der Handelszeit
        # (kein today_volume) fällt das auf das Vortags-Volumen zurück.
        vol_display = lv.get("today_volume") or lv.get("prev_volume")
        price = q.get("price")
        w52_low = lv.get("week52_low")
        chg_52w_low_pct = None
        if price is not None and w52_low:
            chg_52w_low_pct = (price / w52_low - 1.0) * 100.0
        rows.append(
            {
                "Ticker": item["ticker"],
                "WKN": item.get("wkn") or "—",
                "Name": item["name"],
                "Region": f"{REGION_FLAG_ONLY.get(region, '')} {REGION_CODE_DISPLAY.get(region, region)}".strip(),
                "Sektor": sector,
                "Index": idx_name,
                "1D %": q["chg"],
                "ATR(14)": round(float(lv["atr"]), 2) if lv.get("atr") is not None else None,
                "Kurs": q["price"],
                "High": q["high"],
                "Low": q["low"],
                "Volumen": vol_display,
                "Market Cap": lv.get("market_cap"),
                "Δ 52W-Tief %": round(chg_52w_low_pct, 2) if chg_52w_low_pct is not None else None,
                # "Vol 1J %" / "High Vol": Spalten entfernt (s. Begründung an der Sidebar-
                # Toggle-Stelle) - vol/high werden oben zwar noch berechnet (fetch_quote()
                # liefert vol_1y ohnehin ohne Zusatzkosten mit, s. Redundanz-Fix), aber
                # nicht mehr in die angezeigte Tabelle übernommen.
                # "Vol 1J %": vol,
                # "High Vol": "⚡" if high else "",
                "yf": item["yf"],
            }
        )
    return pd.DataFrame(rows), hidden, failed_quotes


def build_watchlist(region_filter: str = "ALL", sector_filter: str = "ALL") -> pd.DataFrame:
    threshold = float(st.session_state.get("high_vol_threshold", HIGH_VOL_THRESHOLD))
    # sort_keys, damit die gleiche Watchlist immer denselben Cache-Key ergibt, unabhängig
    # von zufälliger Dict-Reihenfolge.
    watchlist_json = json.dumps(st.session_state.watchlist, sort_keys=True)
    df, hidden, failed_quotes = _build_watchlist_core(
        watchlist_json,
        bool(st.session_state.high_vol_only),
        threshold,
        region_filter,
        sector_filter,
    )
    # Seiteneffekte (Session State) bewusst außerhalb der gecachten Funktion, da
    # st.cache_data den Funktionskörper bei einem Cache-Hit gar nicht mehr ausführt.
    st.session_state["_watchlist_hidden_count"] = hidden
    st.session_state["_watchlist_failed_quotes"] = failed_quotes
    return df.copy()










CLOSE_NOTE_REASONS = frozenset({
    "stop", "take", "ko", "gap-stop", "gap-open",
    "session-close", "daily-loss-limit", "kill",
    "sell", "manual-sell", "manual", "quick-all", "day-end",
})

CLOSE_NOTE_PREFILL = {
    "stop": "STOP ausgelöst",
    "take": "TAKE PROFIT",
    "ko": "Knock-out",
    "gap-stop": "Gap-Stop",
    "gap-open": "Gap-Open",
    "session-close": "Session-Close",
    "daily-loss-limit": "Daily-Loss-Limit",
    "kill": "Kill Switch",
    "sell": "Manueller Close",
    "manual-sell": "Manueller Close",
    "manual": "Manueller Close",
    "quick-all": "Quick Close",
    "day-end": "Tagesende",
}





































def first_hour_warning(pos: dict) -> Optional[str]:
    label = str(pos.get("setup_label") or "").lower()
    if "or-breakout" not in label and "opening range" not in label:
        return None
    now = datetime.now(TZ_BERLIN).time()
    venue = str(pos.get("venue") or "").lower()
    in_first_hour = time(9, 0) <= now < time(10, 0) if ("xetra" in venue or "euro" in venue) else time(15, 30) <= now < time(16, 30)
    return "⚠️ Opening-Range-TP: erste Handelsstunde — OR/VWAP noch nicht ausgereift, erhöhte Volatilität." if in_first_hour else None




# Statistisch lässt die Erststunden-Dynamik nach diesen Uhrzeiten (Europe/Berlin)
# spürbar nach - Hinweis, jetzt Gewinne zu sichern bzw. Positionsentscheidungen zu
# treffen, bevor die Bewegung "verwässert". Kein echter Push (Handy/Browser-
# Benachrichtigung außerhalb des Tabs), sondern ein In-App-Popup wie bei
# Trade-Bestätigungen - der Tab muss offen sein.
MOMENTUM_FADE_ALARM_TIMES = {
    "Xetra": time(10, 25),
    "LSE": time(10, 25),
    "US Regular": time(16, 55),
}


@st.dialog("🔻 Drei rote Kerzen in Folge", width="large")
def _three_red_candles_dialog(ticker: str, last_ts: str):
    st.warning(
        f"**{ticker}**: Die letzten drei 5-Min-Kerzen sind rot geschlossen (jeweils "
        "Close < Open).\n\n"
        "Drei rote Kerzen in Folge bestätigen häufig einen bestehenden Abwärtstrend "
        "und werden von vielen Tradern als Verkaufssignal gedeutet."
    )
    st.caption(
        f"Letzte Kerze: {last_ts} · Regelbasierte Mustererkennung, kein Handelssignal. "
        "Erscheint einmal je neuer Drei-Kerzen-Folge."
    )
    if st.button("Verstanden", type="primary", use_container_width=True, key="three_red_ack"):
        st.rerun()


def _maybe_show_three_red_candles_alarm(ticker: str, intra: pd.DataFrame, sess: Optional[dict] = None):
    """Prüft die letzten 3 abgeschlossenen 5-Min-Kerzen auf drei rote Kerzen in Folge
    (Close < Open) und zeigt einmal je neu entstandener Folge ein Popup.
    Nur während der regulären Handelszeit (2026-09-Fix): vorher konnte das Popup
    auch in PRE/CLOSE oder am Wochenende/Feiertag auftauchen, wenn `intra` noch
    die letzten drei Kerzen der VORHERIGEN Session zeigte (Markt zu, aber
    zufällig noch drei rote Kerzen am Ende) - fachlich nicht falsch (die Kerzen
    WAREN rot), aber irreführend außerhalb der Handelszeit präsentiert."""
    if sess is not None and (
        sess.get("mode") not in ("OPEN", "MID") or sess.get("weekend") or sess.get("holiday")
    ):
        return
    if intra is None or len(intra) < 3 or not ticker:
        return
    last3 = intra.tail(3)
    is_red = (last3["Close"] < last3["Open"]).tolist()
    if not all(is_red):
        return
    last_ts = str(intra.index[-1])
    shown = st.session_state.setdefault("three_red_candles_shown", {})
    if shown.get(ticker) == last_ts:
        return  # bereits für genau diese Kerzenfolge gezeigt
    shown[ticker] = last_ts
    _three_red_candles_dialog(ticker, last3.index[-1].strftime("%d.%m.%Y %H:%M"))


def _maybe_show_momentum_fade_alarm(sess: dict):
    venue = sess.get("venue")
    now = sess.get("now")
    alarm_t = MOMENTUM_FADE_ALARM_TIMES.get(venue)
    if not alarm_t or not now or now.weekday() >= 5:
        return
    if is_market_holiday(venue, now.date()):
        return
    today_key = now.date().isoformat()
    shown = st.session_state.setdefault("momentum_fade_alarm_shown", {})
    if shown.get(venue) == today_key:
        return
    # Kleines Zeitfenster (statt exakter Minute), damit der Alarm nicht durch einen
    # verpassten Rerun-Zeitpunkt genau in der Zielminute komplett ausfällt.
    alarm_dt = now.replace(hour=alarm_t.hour, minute=alarm_t.minute, second=0, microsecond=0)
    if alarm_dt <= now <= alarm_dt + timedelta(minutes=5):
        shown[venue] = today_key
        _momentum_fade_alarm_dialog(venue)


@st.dialog("⏰ Erststunden-Momentum lässt nach", width="large")
def _momentum_fade_alarm_dialog(venue: str):
    venue_label = "US-Titel" if venue == "US Regular" else "EU-Titel (Xetra/LSE)"
    st.warning(
        f"**Guter Zeitpunkt für eine Entscheidung bei {venue_label}.**\n\n"
        "Statistisch ist die stärkste Marktdynamik der ersten Handelsstunde jetzt "
        "meist vorbei. Prüfe offene Positionen: Gewinne sichern (Teil- oder "
        "Komplettverkauf) oder bewusst verlängern - nicht automatisch weiterlaufen "
        "lassen, nur weil noch nichts entschieden wurde."
    )
    st.caption(
        "Dieser Hinweis ist eine statistische Faustregel, kein Handelssignal. "
        "Er erscheint einmal pro Handelstag und Venue."
    )
    if st.button("Verstanden", type="primary", use_container_width=True, key="momentum_fade_ack"):
        st.rerun()


@st.dialog("Feature-Vergleich", width="large")
def _feature_comparison_dialog():
    st.image(FEATURE_COMPARISON_URL, use_container_width=True)


CRITICAL_EVENT_LABELS = {
    "stop": "🔴 Stop ausgelöst",
    "take": "🟢 Take-Profit erreicht",
    "ko": "💥 Knock-Out",
    "gap-stop": "🔴 Gap-Stop ausgelöst",
    "gap-open": "🔴 Gap-Open-Stop ausgelöst",
    "session-close": "⏱️ Auto-Close (Handelsschluss)",
    "daily-loss-limit": "🛑 Daily Loss Limit",
    "kill": "⛔ Kill Switch",  # Bugfix (2026-09): tatsächlicher reason-String bei
    # flatten_all(watch, reason="kill") ist "kill", nicht "kill-switch" - eigener
    # Test hat das aufgedeckt, sonst wäre der Kill Switch aus dem System
    # herausgefallen, obwohl er gerade der Fall mit dem meisten Broker-Abgleich-
    # Bedarf auf einmal ist.
    "trail": "📍 Stop nachgezogen — Broker-Stop anpassen",
    "stop-set": "📍 Stop gesetzt — Broker-Stop anpassen",
}


def _format_critical_event_line(ev: dict) -> str:
    """Eine Dialogzeile: Closes mit Kurs/P&L, Trails mit alt→neu Stop."""
    label = CRITICAL_EVENT_LABELS.get(ev.get("event_type"), ev.get("event_type"))
    d = ev.get("details") or {}
    line = f"**{ev.get('ticker') or '—'}** — {label}"
    if ev.get("side"):
        line += f" ({ev['side']})"
    old_stop = d.get("old_stop")
    new_stop = d.get("new_stop")
    if old_stop is not None and new_stop is not None:
        line += f" · Stop {float(old_stop):.4f} → {float(new_stop):.4f}"
    elif new_stop is not None:
        line += f" · Stop {float(new_stop):.4f}"
    price = d.get("price")
    pnl = d.get("pnl")
    if price is not None:
        try:
            line += f" · Kurs {float(price):.2f}"
        except (TypeError, ValueError):
            pass
    if pnl is not None:
        try:
            line += f" · P&L {float(pnl):+.2f} €"
        except (TypeError, ValueError):
            pass
    return line










TRADE_MOOD_CHOICES = [
    "— bitte wählen —",
    "😌 Calm",
    "🤔 Neutral",
    "😟 Anxious",
    "🚀 Euphoric",
    "😤 Frustrated",
    "😴 Tired",
]




def _lookup_product_details(ticker: str, wkn: str | None, side: str | None = None, leverage: float | None = None):
    candidates = [p for p in PRODUCT_CATALOG if p.get("ticker") == ticker]
    if wkn:
        candidates = [p for p in candidates if p.get("wkn") == wkn] or candidates
    if side:
        side_candidates = [p for p in candidates if p.get("side") == side]
        if side_candidates:
            candidates = side_candidates
    if candidates:
        p = dict(candidates[0])
        p["leverage"] = p.get("leverage") or leverage
        return p
    return {"wkn": wkn, "isin": None, "issuer": "—", "typ": "—", "leverage": leverage, "ko": None}




def _commit_pending_trade(trade: dict, mood: str, quality: str):
    kind = trade.get("kind") or "buy"
    watch = trade.get("watch")
    if watch is None:
        try:
            watch = build_watchlist()
        except Exception:
            watch = pd.DataFrame()
    ok, msg = True, "ok"
    if kind == "buy":
        product = trade.get("product") or {}
        ok, msg = execute_paper_buy(
            trade.get("ticker"),
            trade.get("side"),
            trade.get("price"),
            trade.get("stop"),
            trade.get("amount"),
            trade.get("leverage"),
            trade.get("typ") or "Open-End Turbo",
            trade.get("wkn"),
            reason=trade.get("exec_reason") or "ticket",
            setup_label=trade.get("setup_label"),
            mood=mood,
            setup_quality=quality,
            ko=trade.get("ko") or extract_product_ko(product),
            spread_pct=trade.get("spread_pct") if trade.get("spread_pct") is not None else extract_product_spread_pct(product),
            take=trade.get("take"),
            off_plan=bool(trade.get("off_plan", False)),
        )
        if ok:
            rec = trade.get("rec")
            focus = trade.get("ticker")
            if rec and focus:
                key = rec_key(focus, rec)
                if key not in st.session_state.taken_recs:
                    st.session_state.taken_recs.append(key)
            if trade.get("update_ticket"):
                st.session_state.ticket_side = trade.get("side")
                st.session_state.ticket_leverage = trade.get("leverage")
                st.session_state.ticket_typ = trade.get("typ")
                st.session_state.ticket_wkn = trade.get("wkn")
                st.session_state.ticket_amount = trade.get("amount")
    elif kind == "sell":
        close_one(
            trade.get("pid"), watch, reason=trade.get("exec_reason") or "manual-sell",
            mood=mood, setup_quality=quality, setup_label=trade.get("setup_label"),
        )
    elif kind == "partial":
        close_partial(
            trade.get("pid"), float(trade.get("fraction") or 0.5), watch,
            reason=trade.get("exec_reason") or "partial-sell",
            mood=mood, setup_quality=quality, setup_label=trade.get("setup_label"),
        )
    elif kind == "flatten":
        flatten_all(
            watch, reason=trade.get("exec_reason") or "manual",
            mood=mood, setup_quality=quality, setup_label=trade.get("setup_label"),
        )
        st.session_state.day_report = build_day_report()
    else:
        ok, msg = False, f"Unbekannte Trade-Art: {kind}"

    st.session_state.pending_trade = None
    st.session_state.trade_confirmation = None
    if not ok:
        st.session_state.trade_abort_flash = str(msg or "Trade nicht ausgeführt.")
    else:
        st.session_state.trade_ok_flash = f"{trade.get('action') or 'Trade'} ausgeführt."
    st.rerun()




def _trade_failure(msg):
    """Einheitliche, prominente Ablehnungsmeldung für nicht ausgeführte Trades."""
    st.error("🔴 **TRADE NICHT AUSGEFÜHRT**")
    st.warning(str(msg or "Unbekannter Ablehnungsgrund."))










TZ_BERLIN = ZoneInfo("Europe/Berlin")






# Börsenfeiertage (Kalendertag, Venue-übergreifend wo üblich). Kein Live-Kalender —
# bei Bedarf jährlich pflegen. Format: "YYYY-MM-DD".
MARKET_HOLIDAYS = {
    "Xetra": {
        "2026-01-01", "2026-04-03", "2026-04-06", "2026-05-01", "2026-05-14",
        "2026-05-25", "2026-06-04", "2026-10-03", "2026-12-24", "2026-12-25",
        "2026-12-26", "2026-12-31",
        "2027-01-01", "2027-03-26", "2027-03-29", "2027-05-01", "2027-05-06",
        "2027-05-17", "2027-05-27", "2027-10-03", "2027-12-24", "2027-12-25",
        "2027-12-26", "2027-12-31",
    },
    "LSE": {
        "2026-01-01", "2026-04-03", "2026-04-06", "2026-05-04", "2026-05-25",
        "2026-08-31", "2026-12-25", "2026-12-28",
        "2027-01-01", "2027-03-26", "2027-03-29", "2027-05-03", "2027-05-31",
        "2027-08-30", "2027-12-27", "2027-12-28",
    },
    "US Regular": {
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
        "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    },
}






MODE_PLAYBOOK = {
    "PRE": {
        "title": "Pre",
        "text": "Vorbörse: letzter offizieller Schlusskurs. Beobachten, Watchlist vorbereiten, keine neuen Intraday-Käufe.",
        "buy": False,
        "manage": False,
    },
    "OPEN": {
        "title": "Open",
        "text": "Nur Opening-Range- und VWAP-Setups. Erstes Ticket, kein wildes Nachlegen.",
        "buy": True,
        "manage": True,
    },
    "MID": {
        "title": "Mid",
        "text": "Review: Stops nachziehen, reduzieren, Nachkauf nur wenn das Morgen-Szenario noch gilt.",
        "buy": True,
        "manage": True,
    },
    "CLOSE": {
        "title": "Close",
        "text": "Risikoreduktion: erst Close Preparation (Stops verschärfen, keine neuen Käufe), dann Final Close (Hebel glattstellen, Tag abschließen).",
        "buy": False,
        "manage": True,
    },
}

# Close-Phase = Risikoreduktionsphase mit zwei Unterphasen (statt eines einzigen 10-Min-
# Fensters direkt vor Schluss, das für ein Risikomanagement-System zu kurz war):
#   - Close Preparation: beginnt "Puffer vor Schluss (Min)" (flatten_buffer_min, Default/
#     Max 60 Min) vor Handelsschluss. Keine neuen Käufe (s. MODE_PLAYBOOK), Trailing wird
#     verschärft (s. manage_position()), aber noch KEIN Zwangs-Glattstellen.
#   - Final Close: die letzten CLOSE_FINAL_MINUTES vor Schluss. Hier greift erst der
#     tatsächliche Zwangs-Close (s. auto_close_due_positions()).
CLOSE_FINAL_MINUTES = 15

# Tages-Setup-Vertrag (2026-09): EIN verbindliches Setup pro Tag, vor dem ersten
# Kauf gewählt - Gegenmittel gegen "erster Impuls-Kauf ohne festgelegtes Setup".
# "Sonstiges/Discretionary" bleibt wählbar (nicht jeder Tag hat ein sauberes
# Setup), zählt aber bewusst NICHT gleichwertig: wird es gewählt, tauchen keine
# Trades als "off-plan" auf, aber genau diese Wahl ist selbst im Tagesbericht
# sichtbar - der Guard wirkt über Transparenz, nicht über ein hartes Verbot.
DAILY_SETUP_OPTIONS = [
    "OR-Breakout", "VWAP-Reclaim", "VWAP-Pullback", "PDH-Break", "Pullback",
    "Muster", "Sonstiges/Discretionary",
]










REGION_VENUE = {"USA": "US Regular", "UK": "LSE", "EU": "Xetra", "Schweiz": "Xetra"}













class BacktestDataError(Exception):
    """Kategorisierter Fehler beim Laden historischer Backtest-Daten (2026-09-Fix).
    category: 'ticker' (Symbol/Suffix existiert nicht bei Yahoo) |
              'interval' (Symbol existiert, aber keine Intraday-Daten für dieses
              Intervall verfügbar) | 'api' (Netzwerk-/API-Fehler, vermutlich transient).
    """
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


# Yahoo/yfinance begrenzt die verfügbare INTRADAY-Historie je Intervall - das ist
# ein reales Limit der Datenquelle, keine Einstellung:
#   1m               -> max.  7 Tage
#   2m/5m/15m/30m/90m -> max. 60 Tage
#   60m/90m/1h       -> max. 730 Tage
# Die alten Werte (120d/180d/365d für 5m/15m/30m) lagen für DREI von vier
# Intervallen weit über diesem Limit. Yahoo liefert dann kein Fehler-Objekt,
# sondern schlicht einen leeren DataFrame zurück - das sah aus wie ein falsches
# Symbol, war aber ein zu langer Zeitraum. 730d für 60m liegt dagegen innerhalb
# des Limits und war vermutlich nie das eigentliche Problem.
BT_FALLBACK_DAYS = {
    "5m": [59, 30, 10, 5],
    "15m": [59, 30, 10, 5],
    "30m": [59, 30, 10, 5],
    "60m": [729, 365, 180, 60],
}
















def load_focus_news(meta) -> list:
    if not meta:
        return []
    return fetch_news(
        meta.get("yf") or meta.get("ticker") or "",
        ticker=meta.get("ticker") or "",
        name=meta.get("name") or "",
        sources=enabled_news_sources(),
    )


# --- PRE Stock Picker: News → mögliche Bewegung heute ---
PICKER_TAG_WEIGHTS = {
    "adhoc": 6.0,
    "earnings": 5.0,
    "rating": 3.5,
    "mna": 4.0,
    "legal": 3.0,
}
PICKER_MIN_SCORE = 3.5
PICKER_TOP_N = 10
PICKER_MAX_AGE_H = 18.0


def score_pre_movement(news: list) -> dict:
    """Bewertet Bewegungspotenzial aus News (Richtung sekundär).

    Frage: Könnte dieser Titel heute Range/Aufmerksamkeit bekommen?
    Nicht: Long oder Short.
    """
    if not news:
        return {
            "move_score": 0.0,
            "bias": "neutral",
            "n": 0,
            "tags": [],
            "headline": "",
            "reasons": [],
            "abs_sentiment": 0.0,
        }
    now = datetime.now(timezone.utc)
    move = 0.0
    sent = 0.0
    tag_hits = set()
    reasons = []
    best = None
    best_w = -1.0
    n_recent = 0

    for item in news:
        if "score" not in item:
            item = annotate_news_item(item)
        dt = item.get("dt")
        age_h = None
        if dt is not None:
            age_h = (now - dt).total_seconds() / 3600.0
            if age_h > PICKER_MAX_AGE_H:
                continue
        n_recent += 1
        rw = _recency_weight(dt)
        tags = item.get("tags") or []
        tag_boost = 0.0
        for t in tags:
            tag_hits.add(t)
            tag_boost += PICKER_TAG_WEIGHTS.get(t, 0.0)
        raw_sc = float(item.get("score") or 0)
        # Bewegung: |Sentiment| + Tag-Katalysator, beides mit Recency
        item_move = (abs(raw_sc) * 1.1 + tag_boost) * rw
        # Mehrere Headlines addieren sich degressiv
        move += item_move
        sent += raw_sc * rw
        if item_move > best_w:
            best_w = item_move
            best = item

    # Intensität: mehrere frische Meldungen erhöhen die Chance auf Range
    if n_recent >= 3:
        move += 1.5
        reasons.append(f"{n_recent} frische Meldungen")
    elif n_recent == 2:
        move += 0.7
        reasons.append("2 frische Meldungen")

    for t in ("adhoc", "earnings", "rating", "mna", "legal"):
        if t in tag_hits:
            reasons.append({
                "adhoc": "Ad-hoc/Pflichtmeldung",
                "earnings": "Earnings/Zahlen",
                "rating": "Analyst/Rating",
                "mna": "M&A",
                "legal": "Legal/Regulatorik",
            }[t])

    if abs(sent) >= 2.5:
        reasons.append("starkes Sentiment")
    elif abs(sent) >= 1.2:
        reasons.append("klares Sentiment")

    if sent >= 1.5:
        bias = "long"
    elif sent <= -1.5:
        bias = "short"
    else:
        bias = "neutral"

    headline = (best or {}).get("title") or ""
    return {
        "move_score": round(move, 2),
        "bias": bias,
        "n": n_recent,
        "tags": sorted(tag_hits),
        "headline": headline,
        "reasons": reasons[:5],
        "abs_sentiment": round(abs(sent), 2),
        "headline_source": (best or {}).get("source") or "",
        "headline_url": (best or {}).get("url") or "",
        "headline_when": (best or {}).get("when") or "",
    }


@st.cache_data(ttl=900, show_spinner=False)  # geändert von 240 auf 900
def scan_pre_stock_picker(sources: tuple, region_filter: str = "ALL") -> list:
    """Scannt das gesamte Universe auf news-basiertes Bewegungspotenzial (PRE)."""
    if not sources:
        sources = tuple(k for k, v in NEWS_SOURCE_DEFS.items() if v["default"])

    # Parallelisiert (2026-09): Region-Filter (kein Netzwerk-Call) zuerst,
    # fetch_news() (Netzwerk-Call je Titel) dann parallel - das ist der Scan,
    # der bereits das GESAMTE Universum abdeckt, also der größte Hebel für das
    # "Universum in <1 Min."-Ziel.
    candidates = [
        item for item in all_universe_items()
        if region_filter == "ALL" or item.get("region") == region_filter
    ]
    news_lists = _parallel_map(
        lambda it: fetch_news(it["yf"], ticker=it["ticker"], name=it.get("name") or "", sources=sources),
        candidates,
    )

    # Dictionary: ticker -> bestes Ergebnis (höchster move_score)
    best_by_ticker = {}

    for item, news in zip(candidates, news_lists):
        if news is None:
            news = []
        scored = score_pre_movement(news)

        # Überspringe Kandidaten, die unter dem Mindest-Score liegen
        if scored["move_score"] < PICKER_MIN_SCORE and scored["n"] == 0:
            continue
        if scored["move_score"] < PICKER_MIN_SCORE:
            continue

        ticker = item["ticker"]
        # Nur den höchsten Score für diesen Ticker behalten
        if ticker not in best_by_ticker or scored["move_score"] > best_by_ticker[ticker]["move_score"]:
            best_by_ticker[ticker] = {
                "ticker": ticker,
                "yf": item["yf"],
                "name": item.get("name") or ticker,
                "region": item.get("region") or "",
                "index": item.get("index") or "",
                **scored,
            }
    
    # In Liste umwandeln, nach move_score absteigend sortieren und Top N zurückgeben
    rows = list(best_by_ticker.values())
    rows.sort(key=lambda r: r["move_score"], reverse=True)
    return rows[:PICKER_TOP_N]


def render_pre_stock_picker(sess: dict):
    """UI: PRE-Phase Stock Picker — welche Titel könnten heute Bewegung haben?"""
    if sess.get("mode") != "PRE":
        return

    st.subheader("Stock Picker · PRE")
    st.caption(
        "News-Scan über das gesamte Universe. "
        "Frage: Welche Titel könnten heute Bewegung haben? "
        "Kein Long/Short-Signal — nur Katalysator-Priorisierung für die Watchlist."
    )

    sources = enabled_news_sources()
    if not sources:
        st.warning("Keine News-Quellen aktiv. In der Sidebar mindestens eine Quelle einschalten.")
        return

    # Info über Quellen und Einstellungen als eigene Zeile
    st.caption(
        f"Quellen: {', '.join(NEWS_SOURCE_DEFS[s]['label'] for s in sources if s in NEWS_SOURCE_DEFS)} · "
        f"Min-Score {PICKER_MIN_SCORE} · Fenster {int(PICKER_MAX_AGE_H)}h"
    )

    # Vier Spalten: Region, Sektor, Scan-Button, Add-Button – alle auf gleicher Höhe
    c1, c1b, c2, c3 = st.columns([1.3, 1.3, 1, 1])
    with c1:
        picker_region = st.selectbox(
            "Region", ["ALL", "USA", "EU", "UK", "Schweiz"],
            format_func=lambda r: {"ALL": "Alle Märkte", "USA": "🇺🇸 US", "EU": "🇪🇺 EU", "UK": "🇬🇧 UK", "Schweiz": "🇨🇭 CH"}[r],
            key="pre_picker_region",
            label_visibility="collapsed",   # <-- entfernt die sichtbare Überschrift
        )
    with c1b:
        picker_sector = st.selectbox(
            "Sektor", ["ALL"] + sorted(set(SECTOR_MAP.values()) | {"Sonstige"}),
            format_func=lambda s: "Alle Sektoren" if s == "ALL" else s,
            key="pre_picker_sector",
            label_visibility="collapsed",
        )
    with c2:
        rescan = st.button("Picker neu scannen", key="pre_picker_rescan", use_container_width=True)
    with c3:
        add_all = st.button("Top 5 → Watchlist", key="pre_picker_add_top5", use_container_width=True)

    # Kein Auto-Scan bei jedem Rerun mehr: der Scan läuft nur beim ersten Aufruf
    # (noch kein Ergebnis im Session-State) oder wenn explizit auf "Picker neu
    # scannen" geklickt wird - vorher wurde bei jedem Rerun (z.B. Sidebar-Toggle)
    # erneut gescannt, was unnötige Last erzeugt hat.
    if rescan:
        if hasattr(scan_pre_stock_picker, "clear"):
            scan_pre_stock_picker.clear()
        for fn in (fetch_news, fetch_rss_feed, fetch_yahoo_news, fetch_finnhub_news):
            if hasattr(fn, "clear"):
                fn.clear()
        st.session_state.pop("_pre_picker_results", None)

    if "_pre_picker_results" not in st.session_state or rescan:
        with st.spinner("Universe-News für PRE-Picker…"):
            st.session_state["_pre_picker_results"] = scan_pre_stock_picker(sources, picker_region)
        st.session_state["_pre_picker_scanned_at_dt"] = datetime.now(TZ_BERLIN)
    picks = st.session_state["_pre_picker_results"]
    if picker_sector != "ALL":
        picks = [r for r in picks if get_sector(r.get("ticker", "")) == picker_sector]
    _scanned_at_dt = st.session_state.get("_pre_picker_scanned_at_dt")
    if _scanned_at_dt:
        _age_min = (datetime.now(TZ_BERLIN) - _scanned_at_dt).total_seconds() / 60
        _staleness = f" · ⚠️ {_age_min:.0f} Min. alt, ggf. nicht mehr aktuell" if _age_min >= 5 else ""
        st.caption(f"Letzter Scan: {_scanned_at_dt.strftime('%H:%M:%S')} CET{_staleness}")
    else:
        st.caption("Letzter Scan: —")

    if add_all and picks:
        added = 0
        for row in picks[:5]:
            if add_to_watchlist(row["ticker"], row["yf"], row["name"]):
                added += 1
        if added:
            st.toast(f"{added} Titel zur Watchlist hinzugefügt", icon="📌")
            st.rerun()
        else:
            st.info("Top 5 sind bereits alle auf der Watchlist.")

    if not picks:
        if sess.get("weekend") or sess.get("holiday"):
            st.info(
                "Wochenende/Feiertag: wenig frische Katalysatoren erwartet. "
                "Am Handelstag erneut scannen."
            )
        else:
            st.info(
                "Keine klaren News-Katalysatoren im Universe (oder Quellen liefern nichts). "
                "Später erneut scannen oder weitere Quellen aktivieren."
            )
        return

    owned = {w["ticker"] for w in st.session_state.watchlist}
    for i, row in enumerate(picks):
        tags = " ".join(f"`{t}`" for t in (row.get("tags") or []))
        bias = row.get("bias") or "neutral"
        bias_icon = {"long": "🟢", "short": "🔴", "neutral": "⚪"}.get(bias, "⚪")
        reasons = " · ".join(row.get("reasons") or []) or "—"
        headline = row.get("headline") or "—"
        url = row.get("headline_url") or ""
        head_md = f"[{headline}]({url})" if url else headline
        in_wl = row["ticker"] in owned

        left, mid, right = st.columns([5, 2, 2])
        with left:
            region_flag = REGION_FLAG_ONLY.get(row.get("region", ""), "")
            region_code = REGION_CODE_DISPLAY.get(row.get("region", ""), row.get("region", ""))
            st.markdown(
                f"**{i+1}. {row['ticker']}** · {row['name']} · "
                f"{region_flag} {region_code} / {row.get('index','')}  \n"
                f"{bias_icon} Move-Score **{row['move_score']:.1f}** · "
                f"{row.get('n', 0)} News · Bias {bias}  \n"
                f"{tags + '  ·  ' if tags else ''}{reasons}  \n"
                f"_{row.get('headline_when') or '—'} · {row.get('headline_source') or '—'}:_ {head_md}"
            )
        with mid:
            if st.button(
                "Fokus",
                key=f"pre_pick_focus_{row['ticker']}",
                use_container_width=True,
            ):
                st.session_state.focus = row["ticker"]
                if not in_wl:
                    add_to_watchlist(row["ticker"], row["yf"], row["name"])
                st.rerun()
        with right:
            if in_wl:
                st.caption("In Watchlist")
            elif st.button(
                "+ Watchlist",
                key=f"pre_pick_wl_{row['ticker']}",
                use_container_width=True,
            ):
                if add_to_watchlist(row["ticker"], row["yf"], row["name"]):
                    st.toast(f"{row['ticker']} hinzugefügt", icon="📌")
                    st.rerun()

    st.caption(
        "Regelbasierte News-Priorisierung für die Vorbörse. "
        "OPEN bewertet später Tradbarkeit, Range und Setup auf der Watchlist."
    )


# --- OPEN Watchlist Ranker: Tradbarkeit · Range · Setup · Marktkontext ---
OPEN_TOP_N = 5
OPEN_MIN_SCORE = 5.5
INDEX_BENCH = {
    ("USA", "S&P 500"): "SPY",
    ("USA", "Nasdaq-100"): "QQQ",
    ("UK", "FTSE 100"): "^FTSE",
    ("EU", "DAX"): "^GDAXI",
    ("EU", "TecDAX"): "^TECDAX",
    ("EU", "EURO STOXX 50"): "^STOXX50E",
    ("EU", "Euronext 100"): "^N100",
    ("EU", "CAC 40"): "^FCHI",
    ("EU", "STOXX Europe 600"): "^STOXX",
    ("Schweiz", "SMI"): "^SSMI",
}




def _bench_for_ticker(ticker: str) -> Optional[str]:
    region, index = find_region_index(ticker)
    return INDEX_BENCH.get((region, index))


def score_open_candidate(meta: dict, venue: str) -> dict:
    """Bewertet einen Watchlist-Titel für OPEN: handelbar heute über Derivate?"""
    ticker = meta.get("ticker") or ""
    yf_sym = meta.get("yf") or ticker
    name = meta.get("name") or ticker
    reasons = []
    flags = []

    q = fetch_quote(yf_sym)
    levels = fetch_levels(yf_sym)
    intra = fetch_intraday(yf_sym)
    price = q.get("price")
    chg = q.get("chg")
    vol_1y = q.get("vol_1y")
    atr = levels.get("atr")
    vwap = levels.get("vwap")
    open_px = levels.get("open")
    spread_pct = levels.get("spread_pct")

    # --- 1) Tradbarkeit (0–10) ---
    trad = 0.0
    if q.get("ok") and price:
        trad += 3.0
    else:
        flags.append("kein Kurs")
    if intra is not None and not intra.empty and len(intra) >= 4:
        trad += 2.5
        vol_sum = float(intra["Volume"].fillna(0).sum()) if "Volume" in intra.columns else 0.0
        if vol_sum > 0:
            trad += 2.0
        else:
            flags.append("kein Intraday-Volumen")
            trad -= 1.0
    else:
        flags.append("kein Intraday")
    if spread_pct is not None:
        if spread_pct <= 0.15:
            trad += 2.0
            reasons.append(f"enger Spread {spread_pct:.2f}%")
        elif spread_pct <= 0.40:
            trad += 1.0
        elif spread_pct > 1.0:
            trad -= 2.0
            flags.append(f"weiter Spread {spread_pct:.2f}%")
    else:
        trad += 0.5
    rvol = compute_rvol(levels.get("today_volume"), levels.get("avg_volume_20d"), venue)
    if rvol is not None:
        if rvol >= 2.0:
            trad += 2.0
            reasons.append(f"RVOL {rvol:.1f}x")
        elif rvol >= 1.3:
            trad += 1.0
            reasons.append(f"RVOL {rvol:.1f}x")
        elif rvol < 0.5:
            trad -= 1.0
            flags.append(f"niedriges RVOL {rvol:.1f}x")
    trad = max(0.0, min(10.0, trad))

    # --- 2) Erwartete / sichtbare Range (0–10) ---
    rng = 0.0
    atr_pct = (atr / price * 100.0) if atr and price else None
    day_range_pct = None
    if intra is not None and not intra.empty and price:
        hi = float(intra["High"].max())
        lo = float(intra["Low"].min())
        if lo > 0:
            day_range_pct = (hi - lo) / price * 100.0
    gap_pct = None
    if open_px and chg is not None and price:
        try:
            prev_close = price / (1.0 + chg / 100.0)
            if prev_close > 0:
                gap_pct = (open_px / prev_close - 1.0) * 100.0
        except Exception:
            gap_pct = None

    if atr_pct is not None:
        if atr_pct >= 4.0:
            rng += 4.0
            reasons.append(f"ATR {atr_pct:.1f}%")
        elif atr_pct >= 2.5:
            rng += 3.0
            reasons.append(f"ATR {atr_pct:.1f}%")
        elif atr_pct >= 1.5:
            rng += 1.5
        else:
            rng += 0.3
            flags.append(f"enge ATR {atr_pct:.1f}%")
    # High-Vol-Einfluss aufs Scoring deaktiviert (2026-09, auf ausdrücklichen Wunsch):
    # 1J-Vola ist für Daytrading die falsche Zeitskala und wird durch Hebel als
    # Opportunitäts-Kriterium relativiert; ATR% (oben, atr_pct) ist die inhaltlich
    # passendere, bereits vorhandene Kennzahl für denselben Zweck. Bewusst nur
    # auskommentiert, nicht gelöscht - s. Begründung an der Sidebar-Toggle-Stelle
    # in dashboard().
    # if vol_1y is not None:
    #     if vol_1y >= 45:
    #         rng += 2.0
    #     elif vol_1y >= 30:
    #         rng += 1.0
    #     elif vol_1y < 20:
    #         rng -= 1.0
    #         flags.append("niedrige 1J-Vola")
    if day_range_pct is not None:
        if day_range_pct >= 2.0:
            rng += 2.5
            reasons.append(f"Tagesrange {day_range_pct:.1f}%")
        elif day_range_pct >= 1.0:
            rng += 1.2
        elif day_range_pct < 0.4:
            rng -= 0.5
            flags.append("noch enge Tagesrange")
    if gap_pct is not None and abs(gap_pct) >= 1.0:
        rng += min(2.0, abs(gap_pct) * 0.6)
        reasons.append(f"Gap {gap_pct:+.1f}%")
    if atr_pct and day_range_pct and day_range_pct > 1.3 * atr_pct:
        rng -= 2.0
        flags.append("Range schon stark ausgeschöpft")
    rng = max(0.0, min(10.0, rng))

    # --- 3) Klares Setup (0–10) ---
    setup = 0.0
    bias = "neutral"
    patterns = (
        detect_intraday_patterns(intra, levels, "OPEN", venue)
        if intra is not None
        else []
    )
    or_h, or_l = (
        _opening_range(intra)
        if intra is not None and not intra.empty
        else (None, None)
    )

    if price and vwap:
        dist_vwap = (price - vwap) / vwap * 100.0
        if abs(dist_vwap) >= 0.35:
            setup += 2.0
            if dist_vwap > 0:
                bias = "long"
                reasons.append("über VWAP")
            else:
                bias = "short"
                reasons.append("unter VWAP")
        else:
            setup += 0.5
            flags.append("nahe VWAP (unklar)")

    if price and or_h and or_l:
        if price > or_h:
            setup += 3.0
            reasons.append("über Opening Range")
            if bias == "neutral":
                bias = "long"
            elif bias == "short":
                flags.append("OR long vs. VWAP short")
        elif price < or_l:
            setup += 3.0
            reasons.append("unter Opening Range")
            if bias == "neutral":
                bias = "short"
            elif bias == "long":
                flags.append("OR short vs. VWAP long")
        else:
            setup += 0.8
            flags.append("in Opening Range")

    if patterns:
        best_p = max(patterns, key=lambda p: p.get("confidence") or 0)
        conf = float(best_p.get("confidence") or 50) / 100.0
        setup += 2.5 * conf + 1.0
        reasons.append(best_p.get("name") or "Muster")
        pb = best_p.get("bias")
        if pb in ("long", "short"):
            if bias == "neutral":
                bias = pb
            elif bias != pb:
                setup -= 1.5
                flags.append("Muster gegen Level-Bias")

    if setup < 2.0:
        flags.append("kein klares Setup")
    setup = max(0.0, min(10.0, setup))

    # --- 4) Marktkontext / Relative Stärke (0–10) ---
    ctx = 5.0
    bench = _bench_for_ticker(ticker)
    idx_chg = fetch_index_day_chg(bench) if bench else None
    rel = None
    if chg is not None and idx_chg is not None:
        rel = chg - idx_chg
        if bias == "long" and rel >= 0.8:
            ctx += 3.0
            reasons.append(f"RS {rel:+.1f}% vs. Index")
        elif bias == "short" and rel <= -0.8:
            ctx += 3.0
            reasons.append(f"RS {rel:+.1f}% vs. Index")
        elif bias == "long" and rel <= -1.0:
            ctx -= 2.0
            flags.append("Long gegen relative Schwäche")
        elif bias == "short" and rel >= 1.0:
            ctx -= 2.0
            flags.append("Short gegen relative Stärke")
        elif abs(rel) >= 1.5:
            ctx += 1.5
            reasons.append(f"Ausreißer vs. Index {rel:+.1f}%")
            if bias == "neutral":
                bias = "long" if rel > 0 else "short"
        else:
            ctx += 0.5
    elif chg is not None:
        if abs(chg) >= 2.0:
            ctx += 1.5
            reasons.append(f"Tag {chg:+.1f}%")
        ctx += 0.5
    ctx = max(0.0, min(10.0, ctx))

    # --- Derivat-Eignung ---
    deriv_ok = False
    lev_hint = None
    side_hint = bias if bias in ("long", "short") else None
    if price:
        cat = live_catalog(ticker, price)
        if cat is not None and not cat.empty:
            deriv_ok = True
            if atr_pct is not None and atr_pct >= 4.5:
                lev_hint = 3.0
            elif atr_pct is not None and atr_pct >= 3.0:
                lev_hint = 5.0
            else:
                lev_hint = 5.0 if atr_pct is None else 7.0
        else:
            flags.append("kein Derivatkatalog")
    if not deriv_ok:
        trad = min(trad, 4.0)

    total = 0.30 * trad + 0.25 * rng + 0.30 * setup + 0.15 * ctx
    if not side_hint:
        total *= 0.75
        flags.append("keine klare Seite")

    grade = "C"
    if total >= 7.5 and side_hint and trad >= 5:
        grade = "A"
    elif total >= 6.0 and trad >= 4:
        grade = "B"

    region, index = find_region_index(ticker)
    return {
        "ticker": ticker,
        "yf": yf_sym,
        "name": name,
        "region": region,
        "index": index,
        "score": round(total, 2),
        "grade": grade,
        "bias": bias,
        "side_hint": side_hint,
        "lev_hint": lev_hint,
        "trad": round(trad, 1),
        "range": round(rng, 1),
        "setup": round(setup, 1),
        "context": round(ctx, 1),
        "price": price,
        "chg": chg,
        "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
        "day_range_pct": round(day_range_pct, 2) if day_range_pct is not None else None,
        "gap_pct": round(gap_pct, 2) if gap_pct is not None else None,
        "rvol": round(rvol, 2) if rvol is not None else None,
        "rel": round(rel, 2) if rel is not None else None,
        "reasons": reasons[:6],
        "flags": flags[:5],
        "deriv_ok": deriv_ok,
    }


def scan_open_watchlist_ranker(venue: str, region_filter: str = "ALL") -> list:
    """Bewertet die aktuelle Watchlist für OPEN und liefert Top-N."""
    # Parallelisiert (2026-09): Region-Filter (kein Netzwerk-Call) zuerst,
    # score_open_candidate() (3 Netzwerk-Calls je Titel intern) dann parallel.
    candidates = [
        w for w in (st.session_state.get("watchlist") or [])
        if region_filter == "ALL" or find_region_index(w.get("ticker"))[0] == region_filter
    ]

    def _score_safe(w):
        try:
            return score_open_candidate(w, venue)
        except Exception:
            return None

    scored_list = _parallel_map(_score_safe, candidates)
    rows = [
        scored for scored in scored_list
        if scored is not None and not (scored["score"] < 4.5 and scored.get("grade") == "C")
    ]
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:OPEN_TOP_N]


OPEN_UNIVERSE_EXTRA_TOP_N = 5


def scan_open_universe_opportunities(venue: str, region_filter: str, exclude_tickers: frozenset) -> list:
    """Bewertet das GESAMTE Universe (nicht nur die Watchlist) mit derselben
    score_open_candidate()-Logik - für das 'Zusätzliche Opportunities'-Panel
    (2026-09). Bewusst nur per explizitem Klick (s. render_open_watchlist_ranker),
    nicht automatisch: ~200 Titel x 3 Netzwerk-Calls ist spürbar teurer als der
    reine Watchlist-Scan. Nur A/B-Grade wird gezeigt (sonst zu viel Rauschen aus
    dem ganzen Universum), Watchlist-Titel werden ausgeschlossen - die stehen ja
    schon im Haupt-Ranker oben."""
    # Bugfix (2026-09): all_universe_items() kann denselben Ticker mehrfach liefern
    # (z.B. wenn er in mehreren Index-/Sektor-Untergruppen von UNIVERSE gelistet
    # ist) - ohne Dedup führte das zu zwei Kandidaten mit demselben Ticker, beide
    # A/B-Grade, und damit zwei Buttons mit identischem Key weiter unten
    # (StreamlitDuplicateElementKey). Erstes Vorkommen pro Ticker gewinnt - spart
    # nebenbei auch unnötige doppelte Netzwerk-Calls für denselben Titel.
    seen_tickers = set()
    candidates = []
    for item in all_universe_items():
        if item["ticker"] in exclude_tickers or item["ticker"] in seen_tickers:
            continue
        if region_filter != "ALL" and item.get("region") != region_filter:
            continue
        seen_tickers.add(item["ticker"])
        candidates.append(item)

    def _score_safe(item):
        try:
            return score_open_candidate(item, venue)
        except Exception:
            return None

    scored_list = _parallel_map(_score_safe, candidates)
    rows = [s for s in scored_list if s is not None and s.get("grade") in ("A", "B")]
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:OPEN_UNIVERSE_EXTRA_TOP_N]


@st.fragment
def _render_ranker_scan_universe_checkbox():
    """Eigenes Mini-Fragment fürs 'Auch außerhalb der Watchlist suchen'-Häkchen
    (2026-09): Umschalten allein löst jetzt nur noch einen winzigen isolierten
    Rerun aus (nur diese Checkbox), nicht mehr den kompletten teuren Ranker-
    Durchlauf. Der eigentliche Scan liest den Wert wie bisher aus session_state
    und läuft weiterhin nur bei explizitem Klick auf "Ranker neu bewerten" -
    das Panel darunter reagiert also bewusst erst dann, nicht live beim
    Umschalten selbst (genau das war die Anforderung).
    """
    st.checkbox(
        f"Auch außerhalb der Watchlist suchen ({UNIVERSE_TICKER_COUNT} Titel, langsamer)",
        value=False,
        key="open_ranker_scan_universe",
        help="Bewertet zusätzlich das gesamte Universe mit derselben Logik und "
             "zeigt A/B-Grade-Titel außerhalb deiner Watchlist in einem eigenen "
             "Panel - nichts wird automatisch zur Watchlist hinzugefügt.",
    )


def render_open_watchlist_ranker(sess: dict):
    """UI: OPEN — Top-Titel der Watchlist für den Handelstag (Derivate)."""
    if sess.get("mode") != "OPEN":
        return

    st.subheader("Stock Ranker · OPEN")
    st.caption(
        "Bewertet nur die Watchlist: Tradbarkeit, erwartete Range, klares Setup, Marktkontext. "
        "Top-Vorschläge für Derivate heute — kein automatisches Ticket."
    )

    wl = st.session_state.get("watchlist") or []
    if not wl:
        st.info(
            "Watchlist leer. In PRE über den News-Picker füllen oder manuell Titel hinzufügen."
        )
        return

    c1, c2, c2b, c3 = st.columns([2.0, 1.1, 1.1, 1.0])
    with c1:
        st.caption(
            f"{len(wl)} Titel auf der Watchlist · Anzeige Top {OPEN_TOP_N}"
        )
    with c2:
        ranker_region = st.selectbox(
            "Region", ["ALL", "USA", "EU", "UK", "Schweiz"],
            format_func=lambda r: {"ALL": "Alle Märkte", "USA": "🇺🇸 US", "EU": "🇪🇺 EU", "UK": "🇬🇧 UK", "Schweiz": "🇨🇭 CH"}[r],
            key="open_ranker_region",
            label_visibility="collapsed",
        )
    with c2b:
        ranker_sector = st.selectbox(
            "Sektor", ["ALL"] + sorted(set(SECTOR_MAP.values()) | {"Sonstige"}),
            format_func=lambda s: "Alle Sektoren" if s == "ALL" else s,
            key="open_ranker_sector",
            label_visibility="collapsed",
        )
    with c3:
        rescan_clicked = st.button("Ranker neu bewerten", key="open_ranker_rescan")
        if rescan_clicked:
            for fn in (
                fetch_quote,
                fetch_intraday,
                fetch_levels,
                fetch_index_day_chg,
                fetch_focus_quote,
            ):
                if hasattr(fn, "clear"):
                    fn.clear()

    _render_ranker_scan_universe_checkbox()
    scan_universe = bool(st.session_state.get("open_ranker_scan_universe", False))
    # Bugfix (2026-09): scan_open_watchlist_ranker() lief vorher bei JEDEM Rerun
    # automatisch (auch bei Preis-Autorefresh, Sektor-Filter-Wechsel etc.) - das
    # war der teuerste Einzelaufruf auf der Seite (score_open_candidate() pro
    # Watchlist-Titel, je 3 Netzwerk-Calls, s. Redundanz-Auflösung weiter oben).
    # Jetzt: session-lokaler Cache pro (Venue, Region), neu berechnet wird nur
    # beim ersten Anzeigen dieser Kombination oder auf expliziten Klick - sonst
    # zeigen wir die zuletzt berechneten Picks mit einem Alters-Hinweis.
    _ranker_cache = st.session_state.setdefault("_open_ranker_cache", {})
    _cache_key = (sess.get("venue") or "Xetra", ranker_region)
    if rescan_clicked or _cache_key not in _ranker_cache:
        with st.spinner("Watchlist für OPEN bewerten…"):
            _picks_fresh = scan_open_watchlist_ranker(*_cache_key)
        _ranker_cache[_cache_key] = (_picks_fresh, datetime.now(TZ_BERLIN))
    picks, _scanned_at = _ranker_cache[_cache_key]
    if ranker_sector != "ALL":
        picks = [r for r in picks if get_sector(r.get("ticker", "")) == ranker_sector]
    _age_min = (datetime.now(TZ_BERLIN) - _scanned_at).total_seconds() / 60
    _staleness = f" · ⚠️ {_age_min:.0f} Min. alt, ggf. nicht mehr aktuell" if _age_min >= 5 else ""
    st.caption(f"Letzte Bewertung: {_scanned_at.strftime('%H:%M:%S')} CET{_staleness}")

    # Universum-Scan (2026-09, "Option C" aus der Picker/Ranker-Diskussion): läuft
    # NUR wenn die Checkbox an ist UND explizit auf "Ranker neu bewerten" geklickt
    # wurde - kein Auto-Scan, weder bei Checkbox-Toggle noch bei normalem Rerun.
    extra_opportunities, extra_scanned_at = None, None
    if scan_universe:
        _extra_cache = st.session_state.setdefault("_open_ranker_extra_cache", {})
        _extra_key = _cache_key
        if rescan_clicked:
            watchlist_tickers = frozenset(w.get("ticker") for w in wl)
            with st.spinner(
                f"Scanne {UNIVERSE_TICKER_COUNT} Titel im Universum ({PARALLEL_FETCH_WORKERS} Worker)…"
            ):
                _extra_fresh = scan_open_universe_opportunities(*_extra_key, watchlist_tickers)
            _extra_cache[_extra_key] = (_extra_fresh, datetime.now(TZ_BERLIN))
        if _extra_key in _extra_cache:
            extra_opportunities, extra_scanned_at = _extra_cache[_extra_key]

    if not picks:
        if sess.get("weekend") or sess.get("holiday"):
            st.info(
                "Markt zu (Wochenende/Feiertag): OPEN-Setups begrenzt. Ranker am Handelstag nutzen."
            )
        else:
            st.info(
                "Keine ausreichend klaren OPEN-Setups auf der Watchlist. "
                "Warten auf OR/VWAP oder Watchlist in PRE schärfen."
            )
    else:
        for i, row in enumerate(picks):
            bias = row.get("bias") or "neutral"
            icon = {"long": "🟢", "short": "🔴", "neutral": "⚪"}.get(bias, "⚪")
            grade = row.get("grade") or "C"
            grade_col = {"A": "🟢", "B": "🟡", "C": "⚪"}.get(grade, "⚪")
            reasons = " · ".join(row.get("reasons") or []) or "—"
            flags = row.get("flags") or []
            flag_txt = (" ⚠️ " + " · ".join(flags)) if flags else ""
            side = row.get("side_hint")
            lev = row.get("lev_hint")
            deriv = ""
            if side and lev:
                deriv = f" · Vorschlag **{side.upper()} ~{lev:.0f}x**"
            elif side:
                deriv = f" · Seite **{side.upper()}**"

            metrics = (
                f"Trad {row['trad']:.0f}/10 · Range {row['range']:.0f}/10 · "
                f"Setup {row['setup']:.0f}/10 · Kontext {row['context']:.0f}/10"
            )
            extra = []
            if row.get("chg") is not None:
                extra.append(f"Tag {row['chg']:+.1f}%")
            if row.get("atr_pct") is not None:
                extra.append(f"ATR {row['atr_pct']:.1f}%")
            if row.get("day_range_pct") is not None:
                extra.append(f"Range {row['day_range_pct']:.1f}%")
            if row.get("rel") is not None:
                extra.append(f"RS {row['rel']:+.1f}%")
            extra_txt = " · ".join(extra)

            left, mid, right = st.columns([5, 2, 2])
            with left:
                row_region, row_index = find_region_index(row["ticker"])
                row_flag = REGION_FLAG_ONLY.get(row_region, "")
                row_region_code = REGION_CODE_DISPLAY.get(row_region, row_region)
                st.markdown(
                    f"**{i+1}. {row['ticker']}** · {row['name']} · "
                    f"{grade_col} **{grade}** · Score **{row['score']:.1f}** {icon}{deriv} · "
                    f"{row_flag} {row_region_code} / {row_index}  \n"
                    f"{metrics}  \n"
                    f"{extra_txt}  \n"
                    f"{reasons}{flag_txt}"
                )
            with mid:
                if st.button(
                    "Fokus",
                    key=f"open_rank_focus_{row['ticker']}",
                    use_container_width=True,
                ):
                    st.session_state.focus = row["ticker"]
                    if side:
                        st.session_state.ticket_side = (
                            "LONG" if side == "long" else "SHORT"
                        )
                    if lev:
                        st.session_state.ticket_leverage = float(lev)
                    # Kein st.rerun() nötig: focus/ticket_side/ticket_leverage werden von
                    # allen späteren Abschnitten in diesem selben Durchlauf (Chart, Katalog,
                    # persist via save_app_state) frisch aus st.session_state gelesen, nicht
                    # aus einer vorher berechneten Variable - der Button-Klick selbst hat den
                    # Rerun schon ausgelöst.
            with right:
                st.caption("auf Watchlist")

        st.caption(
            "A = klar handelbar · B = beobachtenswert · C = schwach/unklar. "
            "Hebelhinweis nur Orientierung an ATR — KO/Spread im Katalog prüfen."
        )

    if scan_universe:
        st.markdown("---")
        st.markdown("#### 💡 Zusätzliche Opportunities (außerhalb der Watchlist)")
        st.caption(
            "A/B-Grade-Titel aus dem gesamten Universe, die nicht auf deiner Watchlist "
            "stehen - reiner Hinweis, nichts wird automatisch hinzugefügt."
        )
        if extra_opportunities is None:
            st.info("Klicke „Ranker neu bewerten“, um auch das Universum zu durchsuchen.")
        elif not extra_opportunities:
            st.caption("Keine A/B-Grade-Titel außerhalb der Watchlist gefunden.")
        else:
            _extra_age_min = (datetime.now(TZ_BERLIN) - extra_scanned_at).total_seconds() / 60
            _extra_staleness = f" · ⚠️ {_extra_age_min:.0f} Min. alt" if _extra_age_min >= 5 else ""
            st.caption(f"Letzter Scan: {extra_scanned_at.strftime('%H:%M:%S')} CET{_extra_staleness}")
            for j, row in enumerate(extra_opportunities):
                bias = row.get("bias") or "neutral"
                icon = {"long": "🟢", "short": "🔴", "neutral": "⚪"}.get(bias, "⚪")
                grade = row.get("grade") or "C"
                grade_col = {"A": "🟢", "B": "🟡", "C": "⚪"}.get(grade, "⚪")
                reasons = " · ".join(row.get("reasons") or []) or "—"
                flags = row.get("flags") or []
                flag_txt = (" ⚠️ " + " · ".join(flags)) if flags else ""
                side = row.get("side_hint")
                lev = row.get("lev_hint")
                deriv = ""
                if side and lev:
                    deriv = f" · Vorschlag **{side.upper()} ~{lev:.0f}x**"
                elif side:
                    deriv = f" · Seite **{side.upper()}**"
                metrics = (
                    f"Trad {row['trad']:.0f}/10 · Range {row['range']:.0f}/10 · "
                    f"Setup {row['setup']:.0f}/10 · Kontext {row['context']:.0f}/10"
                )
                extra = []
                if row.get("chg") is not None:
                    extra.append(f"Tag {row['chg']:+.1f}%")
                if row.get("rvol") is not None:
                    extra.append(f"RVOL {row['rvol']:.1f}x")
                if row.get("atr_pct") is not None:
                    extra.append(f"ATR {row['atr_pct']:.1f}%")
                extra_txt = " · ".join(extra)

                left, mid, right = st.columns([5, 2, 2])
                with left:
                    row_region, row_index = find_region_index(row["ticker"])
                    row_flag = REGION_FLAG_ONLY.get(row_region, "")
                    row_region_code = REGION_CODE_DISPLAY.get(row_region, row_region)
                    st.markdown(
                        f"**{j+1}. {row['ticker']}** · {row['name']} · "
                        f"{grade_col} **{grade}** · Score **{row['score']:.1f}** {icon}{deriv} · "
                        f"{row_flag} {row_region_code} / {row_index}  \n"
                        f"{metrics}  \n"
                        f"{extra_txt}  \n"
                        f"{reasons}{flag_txt}"
                    )
                with mid:
                    if st.button(
                        "Fokus", key=f"open_extra_focus_{row['ticker']}", use_container_width=True,
                    ):
                        st.session_state.focus = row["ticker"]
                        if side:
                            st.session_state.ticket_side = "LONG" if side == "long" else "SHORT"
                        if lev:
                            st.session_state.ticket_leverage = float(lev)
                with right:
                    if st.button(
                        "→ Watchlist", key=f"open_extra_addwl_{row['ticker']}", use_container_width=True,
                    ):
                        row_yf = next(
                            (it["yf"] for it in all_universe_items() if it["ticker"] == row["ticker"]),
                            row["ticker"],
                        )
                        add_to_watchlist(row["ticker"], row_yf, row["name"])
                        st.rerun()


def _advice_severity(action: str) -> str:
    a = (action or "").upper()
    if a in {"EXIT", "CLOSE"}:
        return "red"
    if a in {"REDUCE", "TRAIL", "ADD"}:
        return "yellow"
    if a in {"HOLD"}:
        return "green"
    return "gray"


def evaluate_positions_advice(sess: dict) -> list:
    """Pro offener Position manage_position-Empfehlung mit eigenen Levels/Rec."""
    rows = []
    mode = sess.get("mode") or "MID"
    venue = sess.get("venue") or "Xetra"
    for pos in list(st.session_state.get("positions") or []):
        ticker = pos.get("ticker") or ""
        meta = find_meta(ticker) or catalog_lookup(ticker)
        yf_sym = (meta or {}).get("yf") or ticker
        try:
            levels = fetch_levels(yf_sym)
            intra = fetch_intraday(yf_sym)
            q = fetch_quote(yf_sym)
            news = fetch_news(
                yf_sym,
                ticker=ticker,
                name=(meta or {}).get("name") or "",
                sources=enabled_news_sources(),
            )
            events = fetch_events(yf_sym, ticker)
            rec = recommend_product(
                intra,
                levels,
                news,
                events,
                mode,
                q.get("price"),
                venue=venue,
                ticker=ticker,
            )
            advice = manage_position(pos, rec or {}, levels or {}, mode, sess.get("close_subphase"))
        except Exception:
            advice = {
                "action": "HOLD",
                "label": "Bewertung fehlgeschlagen — manuell prüfen.",
                "trail": None,
            }
            levels = {}
            rec = {}
        sev = _advice_severity(advice.get("action"))
        rows.append(
            {
                "pos": pos,
                "advice": advice,
                "severity": sev,
                "levels": levels,
                "rec": rec,
            }
        )
    # Rot zuerst
    order = {"red": 0, "yellow": 1, "green": 2, "gray": 3}
    rows.sort(key=lambda r: order.get(r["severity"], 9))
    return rows


def render_mid_position_cockpit(sess: dict, advice_rows: list):
    """MID: Ampel-Aggregation oben + Sprung zu Positionen."""
    if sess.get("mode") != "MID":
        return

    st.subheader("Positions-Cockpit · MID")
    st.caption(
        "Überwacht offene Positionen: Stop nachziehen, reduzieren, nachkaufen oder liquidieren. "
        "Keine neuen Universe-Scans — Fokus auf Bestand."
    )

    if not advice_rows:
        st.info("Keine offenen Positionen. Cockpit aktiv, sobald Bestand aufgebaut ist.")
        return

    n_red = sum(1 for r in advice_rows if r["severity"] == "red")
    n_yel = sum(1 for r in advice_rows if r["severity"] == "yellow")
    n_grn = sum(1 for r in advice_rows if r["severity"] == "green")
    n_gry = sum(1 for r in advice_rows if r["severity"] == "gray")

    gray_suffix = f" · ⚪ Neutral: {n_gry}" if n_gry else ""
    if n_red:
        st.error(f"🔴 **Handeln:** {n_red} · 🟡 Anpassen: {n_yel} · 🟢 Halten: {n_grn}{gray_suffix}")
    elif n_yel:
        st.warning(f"🟡 **Anpassen:** {n_yel} · 🟢 Halten: {n_grn} · 🔴 Handeln: {n_red}{gray_suffix}")
    else:
        st.success(f"🟢 **Halten:** {n_grn} · 🟡 Anpassen: {n_yel} · 🔴 Handeln: {n_red}{gray_suffix}")

    action_rows = [r for r in advice_rows if r["severity"] in ("red", "yellow")]
    hold_rows = [r for r in advice_rows if r["severity"] not in ("red", "yellow")]
    if action_rows:
        lines = []
        for r in action_rows:
            pos = r["pos"]
            adv = r["advice"]
            icon = {"red": "🔴", "yellow": "🟡"}.get(r["severity"], "⚪")
            lines.append(
                f"{icon} **{pos.get('ticker')}** {pos.get('side')} · "
                f"{adv.get('action')} — {adv.get('label')}"
            )
        st.markdown("\n\n".join(lines))
    if hold_rows:
        with st.expander(f"🟢 Halten / neutral ({len(hold_rows)})", expanded=False):
            for r in hold_rows:
                pos = r["pos"]
                adv = r["advice"]
                icon = {"green": "🟢", "gray": "⚪"}.get(r["severity"], "⚪")
                st.markdown(
                    f"{icon} **{pos.get('ticker')}** {pos.get('side')} · "
                    f"{adv.get('action')} — {adv.get('label')}"
                )

    trailable = [
        r
        for r in advice_rows
        if r["advice"].get("action") == "TRAIL" and r["advice"].get("trail") is not None
    ]
    c1, c2 = st.columns([2, 2])
    with c1:
        st.markdown(
            '<a href="#bestand-mgmt" style="color:#00e5ff;font-weight:600;">↓ Zu Positionen / Aktionen</a>',
            unsafe_allow_html=True,
        )
    with c2:
        if trailable and st.button(
            f"Alle Stops nachziehen ({len(trailable)})",
            key="mid_bulk_trail",
            use_container_width=True,
        ):
            n = 0
            for r in trailable:
                pid = r["pos"].get("id")
                trail = r["advice"].get("trail")
                if pid and trail is not None:
                    update_stop(pid, float(trail))
                    n += 1
            st.toast(f"🛑 {n} Stop(s) nachgezogen", icon="📍")
            # Kein st.rerun(): update_stop() schreibt direkt in st.session_state.positions,
            # das weiter unten (Bestand/Positionen-Abschnitt) ohnehin frisch aus dem
            # Session State gelesen wird - der Button-Klick hat den Rerun schon ausgelöst.


def refresh_market_data():
    """Clear live-data caches so a UI filter change forces fresh market data."""
    for func_name in (
        "fetch_quote",
        "fetch_focus_quote",
        "fetch_intraday",
        "_fetch_intraday_raw",
        "_fetch_daily_history",
        "build_focus_chart_figure",
        "fetch_news",
        "fetch_rss_feed",
        "fetch_yahoo_news",
        "fetch_finnhub_news",
        "fetch_levels",
        "fetch_events",
        "fetch_index_day_chg",
    ):
        func = globals().get(func_name)
        if func is not None and hasattr(func, "clear"):
            func.clear()
    st.session_state.rec_cache = {}
    st.session_state["last_refresh_ts"] = datetime.now(TZ_BERLIN)
    # Cash-Bestand-Eingabefeld (Pro Modus) bei jedem echten Refresh auf die aktuell
    # berechneten Daten zurücksetzen - ein manuell eingegebener, noch nicht über-
    # nommener Wert soll einen Refresh nicht überdauern.
    st.session_state.pop("sidebar_cash_override", None)


def _disable_browser_back():
    """Neutralisiert den Browser-Zurück-Button innerhalb der App.

    Echtes 'Verbieten' des Zurück-Buttons ist aus Sicherheitsgründen in keinem Browser
    möglich - Streamlit-Custom-Components laufen zudem in einem eigenen iframe, dessen
    JS-History NICHT die des Haupt-Browserfensters ist. Der Zugriff erfolgt daher bewusst
    über window.parent (Streamlit-Components sind same-origin, das ist erlaubt).
    Wirkung: Bei jedem Zurück-Versuch wird sofort wieder derselbe Zustand in die History
    gepusht, sodass der Nutzer auf der App bleibt, statt zur vorherigen Seite/Tab-Historie
    zu gelangen - kein echtes "disable", sondern ein "abfangen und zurückhalten".
    """
    components.html(
        """
        <script>
        (function() {
            try {
                var w = window.parent;
                w.history.pushState(null, "", w.location.href);
                w.addEventListener("popstate", function() {
                    w.history.pushState(null, "", w.location.href);
                });
            } catch (e) {
                // Cross-Origin-Fall (z.B. eingebettetes iFrame auf fremder Domain) -
                // dann ist ein Eingriff in die Parent-History technisch nicht möglich,
                // die App bleibt aber ansonsten voll funktionsfähig.
            }
        })();
        </script>
        """,
        height=0,
        width=0,
    )



def get_selected_rows(event):
    """Extrahiert die ausgewählten Zeilenindizes aus dem Rückgabewert von st.dataframe."""
    if event is None:
        return []
    if hasattr(event, "selection"):
        sel = event.selection
        if hasattr(sel, "rows"):
            return sel.rows
        if isinstance(sel, list):
            return sel
    # Fallback: falls event selbst eine Liste ist (ältere Versionen)
    if isinstance(event, list):
        return event
    return []

def _intraday_vwap_bands(intra: pd.DataFrame) -> pd.DataFrame:
    """Session-VWAP plus Expanding-σ der Typical-Price-Abweichung.

    defensiv: Yahoo-OHLC kann object/tz-gemischt kommen — astype(float)
    auf (tp - vwap) ist in dem Fall abgestürzt (0.9.27).
    """
    empty = pd.DataFrame()
    if intra is None or intra.empty:
        return empty
    need = {"High", "Low", "Close"}
    if not need.issubset(intra.columns):
        return empty
    try:
        high = pd.to_numeric(intra["High"], errors="coerce")
        low = pd.to_numeric(intra["Low"], errors="coerce")
        close = pd.to_numeric(intra["Close"], errors="coerce")
        tp = (high + low + close) / 3.0
        if "Volume" in intra.columns:
            vol = pd.to_numeric(intra["Volume"], errors="coerce").fillna(0).clip(lower=0)
            pv = tp * vol
            cum_vol = vol.cumsum()
            cum_vol = cum_vol.where(cum_vol > 0)
            vwap = pv.cumsum() / cum_vol
        else:
            vwap = tp.expanding(min_periods=1).mean()
        vwap = pd.to_numeric(vwap, errors="coerce").ffill()
        tp = pd.to_numeric(tp, errors="coerce")
        dev = pd.to_numeric(tp.sub(vwap), errors="coerce")
        sigma = dev.expanding(min_periods=6).std()
        sigma = pd.to_numeric(sigma, errors="coerce")
        out = pd.DataFrame({"vwap": vwap, "sigma": sigma}, index=intra.index)
        out["p1"] = out["vwap"] + out["sigma"]
        out["m1"] = out["vwap"] - out["sigma"]
        out["p2"] = out["vwap"] + 2.0 * out["sigma"]
        out["m2"] = out["vwap"] - 2.0 * out["sigma"]
        return out
    except Exception:
        return empty


def _chart_session_markers(venue: str, chart_day, x0, x1) -> list:
    """Wenige vertikale Marken: Börsen-Open plus Desk-Scale-out (keine Sessiongrenzen)."""
    marks = []
    windows = session_windows(venue or "US Regular")
    open_t = windows["open"][0]
    marks.append((open_t, "OPEN"))
    if venue in ("Xetra", "LSE"):
        # 10:25 = erster Teilgewinn, nicht Ende der Opening Range (die geht bis 10:30).
        marks.append((time(10, 25), "1. Scale-out"))
        marks.append((time(15, 30), "US Open"))
    else:
        marks.append((time(16, 25), "1. Scale-out"))
    out = []
    for tmark, label in marks:
        ts = datetime.combine(chart_day, tmark, tzinfo=TZ_BERLIN)
        if x0 is None or x1 is None:
            continue
        if ts < x0 or ts > x1:
            continue
        out.append((ts, label))
    return out


def _position_crv(pos: dict) -> Optional[float]:
    try:
        entry = float(pos.get("entry"))
        stop = float(pos.get("stop"))
        take = pos.get("take")
        if take is None:
            return None
        take = float(take)
        risk = abs(entry - stop)
        if risk <= 1e-12:
            return None
        return abs(take - entry) / risk
    except (TypeError, ValueError):
        return None


@st.cache_data(ttl=330, show_spinner=False)
def build_focus_chart_figure(
    ticker: str,
    fingerprint: tuple,
    _intra: pd.DataFrame,
    _levels: dict,
    chart_title: Optional[str],
    _ticker_fills: tuple,
    _show_volume: bool = False,
    _venue: str = "US Regular",
    _position: Optional[dict] = None,
    _sector_series: Optional[pd.Series] = None,
    _sector_label: Optional[str] = None,
):
    """Baut die Fokus-Chart-Figure (Candlestick + PDH/PDL/Open/VWAP-Linien +
    Vortag-Marker + Kauf-/Verkaufs-Linien).

    Caching-Audit (2026-09) - ROLLBACK: @st.cache_data-Zeile entfernen und die
    Underscore-Präfixe bei _intra/_levels/_ticker_fills weglassen, dann ist das
    exakt der alte, inline im Chart-Abschnitt stehende Aufbau (nur eben in eine
    Funktion ausgelagert - könnte man auch einfach wieder zurückkopieren).

    Der Underscore-Präfix weist Streamlit an, _intra/_levels/_ticker_fills NICHT
    zu hashen (würde bei jedem Aufruf das ganze DataFrame hashen - selbst teuer).
    Stattdessen entscheidet der kompakte `fingerprint`-Parameter über Cache-Treffer/
    -Miss: (Anzahl Kerzen, letzter Zeitstempel, letzter Schlusskurs, VWAP,
    Fills für diesen Ticker heute). Ändert sich keiner dieser Werte (z.B. reiner
    Autorefresh-Tick ohne neue 5-Min-Kerze), kommt die fertige Figure aus dem
    Cache, statt bei jedem Rerun komplett neu mit Plotly aufgebaut zu werden -
    das war die identifizierte Hauptursache der GUI-Trägheit.

    TTL 330s (5,5 Min., knapp über der 5-Min-Kerzenlänge) statt ursprünglich 20s:
    der Fingerprint ist der eigentliche Gültigkeits-Check, die TTL wirkt nur noch
    als Obergrenze/Sicherheitsnetz - eine kürzere TTL würde bei UNVERÄNDERTEM
    Fingerprint trotzdem unnötig neu bauen. VWAP wurde deshalb explizit in den
    Fingerprint aufgenommen (ändert sich sonst ggf. innerhalb einer Kerze, ohne
    dass Kerzenzahl/letzter Kurs das anzeigen würden) - ohne das wäre eine so
    lange TTL riskant gewesen (VWAP-Linie könnte bis zu 5 Min. veraltet stehen).
    Fills-Komponente im Fingerprint sorgt dafür, dass ein gerade geschlossener
    Trade trotzdem sofort im Chart erscheint.
    """
    show_vol = bool(_show_volume) and "Volume" in _intra.columns and not _intra.empty
    show_sector = _sector_series is not None and not _sector_series.dropna().empty
    if show_vol or show_sector:
        if show_vol:
            fig = make_subplots(
                rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                row_heights=[0.74, 0.26],
                specs=[[{"secondary_y": True}], [{"secondary_y": False}]],
            )
        else:
            fig = make_subplots(rows=1, cols=1, specs=[[{"secondary_y": True}]])
        fig.add_trace(
            go.Candlestick(
                x=_intra.index,
                open=_intra["Open"],
                high=_intra["High"],
                low=_intra["Low"],
                close=_intra["Close"],
                name="Preis",
                showlegend=False,
            ),
            row=1, col=1,
        )
        if show_vol:
            up = _intra["Close"] >= _intra["Open"]
            vol_colors = ["#22c55e" if u else "#ff5d6c" for u in up]
            fig.add_trace(
                go.Bar(
                    x=_intra.index,
                    y=_intra["Volume"].fillna(0),
                    marker_color=vol_colors,
                    name="Volumen",
                    showlegend=False,
                    hovertemplate="Volumen: %{y:,.0f}<extra></extra>",
                ),
                row=2, col=1,
            )
        candle_row = 1
    else:
        fig = go.Figure(
            data=[
                go.Candlestick(
                    x=_intra.index,
                    open=_intra["Open"],
                    high=_intra["High"],
                    low=_intra["Low"],
                    close=_intra["Close"],
                    name="Preis",
                    showlegend=False,
                )
            ]
        )
        candle_row = None
    today_high = float(_intra["High"].max()) if not _intra.empty else None
    today_low = float(_intra["Low"].min()) if not _intra.empty else None
    color_map = {
        "today_high": ("#22c55e", "Tages-Hoch"),
        "today_low": ("#22c55e", "Tages-Tief"),
        "prev_high": ("#ff5d6c", "PDH"),
        "prev_low": ("#ff5d6c", "PDL"),
        "open": ("#8b9bb2", "Open"),
        "vwap": ("#5aa8ff", "VWAP"),
    }
    level_values = {**_levels, "today_high": today_high, "today_low": today_low}
    for key, (color, label) in color_map.items():
        val = level_values.get(key)
        if val:
            hkwargs = dict(
                y=val, line_color=color, line_width=1, line_dash="dot",
                annotation_text=label, annotation_font_size=10,
                annotation_font_color=color,
            )
            if candle_row:
                hkwargs["row"] = candle_row
                hkwargs["col"] = 1
            fig.add_hline(**hkwargs)
    marker_x = [_intra.index[0]] if not _intra.empty else []
    if _levels.get("prev_open") and marker_x:
        tkwargs = dict(row=candle_row, col=1) if candle_row else {}
        fig.add_trace(go.Scatter(
            x=marker_x, y=[_levels["prev_open"]], mode="markers",
            marker=dict(symbol="triangle-right", size=11, color="#8b9bb2"),
            name="Vortag Open",
            showlegend=False,
            hovertemplate="Vortag Open: %{y:.2f}<extra></extra>",
        ), **tkwargs)
    if _levels.get("prev_close") and marker_x:
        tkwargs = dict(row=candle_row, col=1) if candle_row else {}
        fig.add_trace(go.Scatter(
            x=marker_x, y=[_levels["prev_close"]], mode="markers",
            marker=dict(symbol="diamond", size=11, color="#eab308"),
            name="Vortag Close",
            showlegend=False,
            hovertemplate="Vortag Close: %{y:.2f}<extra></extra>",
        ), **tkwargs)
    # fills speichern nur "HH:MM:SS" ohne Datum - da die App den Trading-Tag
    # täglich zurücksetzt, wird das Datum des zuletzt sichtbaren Chart-Balkens
    # verwendet, um daraus einen vollständigen Zeitstempel zu bilden.
    chart_day = _intra.index[-1].date()
    for t_str, side in _ticker_fills:
        try:
            t_parts = datetime.strptime(t_str, "%H:%M:%S").time()
        except ValueError:
            continue
        fill_ts = datetime.combine(chart_day, t_parts, tzinfo=TZ_BERLIN)
        if fill_ts < _intra.index[0] or fill_ts > _intra.index[-1]:
            continue  # außerhalb des sichtbaren Chart-Fensters
        is_buy = side == "BUY"
        vkwargs = dict(
            x=fill_ts,
            line_color="#22c55e" if is_buy else "#ff5d6c",
            line_width=1.5,
            line_dash="solid",
            annotation_text=("🟢 Kauf" if is_buy else "🔴 Verkauf"),
            annotation_position="top",
            annotation_font_size=9,
            annotation_font_color="#22c55e" if is_buy else "#ff5d6c",
        )
        if candle_row:
            vkwargs["row"] = candle_row
            vkwargs["col"] = 1
        fig.add_vline(**vkwargs)
    bands = _intraday_vwap_bands(_intra)
    if not bands.empty and bands["sigma"].notna().any():
        tkwargs = dict(row=candle_row, col=1) if candle_row else {}
        band_specs = (
            ("p2", "#5aa8ff", "dot", "VWAP +2σ"),
            ("p1", "#5aa8ff", "dash", "VWAP +1σ"),
            ("m1", "#5aa8ff", "dash", "VWAP −1σ"),
            ("m2", "#5aa8ff", "dot", "VWAP −2σ"),
        )
        for colname, color, dash, name in band_specs:
            series = bands[colname].dropna()
            if series.empty:
                continue
            fig.add_trace(go.Scatter(
                x=series.index, y=series.values, mode="lines",
                line=dict(color=color, width=1, dash=dash),
                name=name, hovertemplate=name + ": %{y:.2f}<extra></extra>",
                showlegend=False,
            ), **tkwargs)
    if not _intra.empty:
        for ts, label in _chart_session_markers(
            _venue, chart_day, _intra.index[0], _intra.index[-1]
        ):
            vkwargs = dict(
                x=ts, line_color="#94a3b8", line_width=1, line_dash="dash",
                annotation_text=label, annotation_position="bottom",
                annotation_font_size=9, annotation_font_color="#94a3b8",
            )
            if candle_row:
                vkwargs["row"] = candle_row
                vkwargs["col"] = 1
            fig.add_vline(**vkwargs)
    if _position:
        pos_lines = (
            ("entry", "#e2e8f0", "solid", "ENTRY"),
            ("stop", "#ff5d6c", "dash", "STOP"),
            ("take", "#22c55e", "dash", "TAKE"),
        )
        for key, color, dash, label in pos_lines:
            raw = _position.get(key)
            try:
                val = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                val = None
            if val is None:
                continue
            hkwargs = dict(
                y=val, line_color=color, line_width=1.5, line_dash=dash,
                annotation_text=label, annotation_font_size=10,
                annotation_font_color=color,
            )
            if candle_row:
                hkwargs["row"] = candle_row
                hkwargs["col"] = 1
            fig.add_hline(**hkwargs)
    if show_sector:
        # Sektor-Linie (2026-09, "Option B"): % Veränderung des Sektor-ETF seit
        # Chart-Start, auf zweiter Y-Achse im selben Feld wie die Kerzen - bewusst
        # standardmäßig AUS (Toggle im Dashboard), um die Kerzen nicht permanent
        # mit einer zweiten Skala zu überladen.
        fig.add_trace(
            go.Scatter(
                x=_sector_series.index, y=_sector_series.values, mode="lines",
                name=f"Sektor ({_sector_label})" if _sector_label else "Sektor",
                line=dict(color="#eab308", width=1.3, dash="dot"),
                hovertemplate="Sektor: %{y:+.2f}%<extra></extra>",
                showlegend=True,
            ),
            row=candle_row, col=1, secondary_y=True,
        )
    fig.update_layout(
        height=460 if show_vol else 340,
        margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="#070b12",
        plot_bgcolor="#101826",
        font_color="#e8eef7",
        xaxis_rangeslider_visible=False,
        showlegend=show_sector,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, font=dict(size=10)),
    )
    if show_sector:
        fig.update_yaxes(
            title_text="Sektor %", secondary_y=True, row=candle_row, col=1,
            showgrid=False, zeroline=True, zerolinecolor="#334155", zerolinewidth=1,
        )
    if show_vol:
        fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
        fig.update_xaxes(rangeslider_visible=False, row=2, col=1)
        fig.update_yaxes(title_text="", row=2, col=1, showgrid=False)
    return fig













def main():
    # Bind the complete app namespace into extracted modules before any app logic runs.
    _ctx = globals()
    for _module in (_core, _data, _trading, _analytics, _ui):
        _module.bind_context(_ctx)
    init_state()
    _init_auth_state()
    # Absicherung: der reguläre Legacy-Claim läuft nur beim frischen OAuth-
    # Code-Austausch in _handle_auth_callback(). Wer zum Zeitpunkt eines Deploys
    # bereits eingeloggt war (Session lief weiter, kein neuer ?code=...), hätte
    # sonst nie geclaimte Alt-Daten gesehen ("Journal/Berichte leer trotz
    # Login"). Deshalb hier zusätzlich: einmal pro Session nachholen.
    if is_authenticated() and not st.session_state.get("_legacy_claim_done"):
        _claim_legacy_data(current_user_id())
        _ensure_trial_started(current_user_id())
        _sync_subscription_status(current_user_id(), force=True)
        st.session_state["_legacy_claim_done"] = True
        st.session_state["_skip_splash_reset"] = True
        st.session_state.pop("_state_loaded", None)
        st.rerun()
    _handle_auth_callback()
    _handle_stripe_return()
    if not st.session_state.entered:
        splash()
    else:
        dashboard()


if __name__ == "__main__":
    main()
