# VOLTDESK_EVENT_SOURCE_SWITCHES_V1
# VOLTDESK_PROFIT_MACRO_INSIDER_PATCH_V1
# ==============================================================================
# app.py – VoltDesk Paper-Trading Desk
# Version 0.9.34 – Event-Uhr cleanup, Makro-Deduplizierung, Refresh 60s
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
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO pending_critical_events "
                "(user_id, event_type, ticker, side, details_json, requires_broker_action, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, event_type, ticker, side, json.dumps(details),
                 1 if requires_broker_action else 0, datetime.now(TZ_BERLIN).isoformat()),
            )
    except Exception as e:
        log_error("critical_trade_event", f"{event_type} {ticker}: {e}", user_id=user_id)


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
        # Statt stiller Leerliste: nicht lesbare KRITISCHE Events duerfen nicht
        # unter den Tisch fallen - ins error_log, UI bleibt vorsichtshalber leer.
        log_error("get_pending_critical_events", str(e))
        return []
    return [
        {
            "id": r[0], "event_type": r[1], "ticker": r[2], "side": r[3],
            "details": json.loads(r[4]), "requires_broker_action": bool(r[5]),
            "created_at": r[6],
        }
        for r in rows
    ]


def acknowledge_critical_events(ids: list) -> None:
    if not ids:
        return
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


VERSION = "0.9.33"
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
BUILD_TIMESTAMP = "2026-09-09 13:45"
def get_build() -> str:
    return BUILD_TIMESTAMP
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



def effective_max_leverage() -> float:
    """Ohne Pro Modus bleibt der Hebel-Cap bei 5x - 8x ist bereits sehr hoch und wird nur
    im Pro Modus freigeschaltet, analog zu den anderen Pro-Modus-Guardrails."""
    return MAX_LEVERAGE if effective_pro_mode() else DEFAULT_LEVERAGE_CAP

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


def get_sector(ticker: str) -> str:
    return SECTOR_MAP.get(ticker, "Sonstige")


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


def get_position_limit_pct() -> float:
    """Aktuelles Positionslimit in % vom Cash.

    Liefert 100% (= faktisch kein Limit), wenn der Schalter in der Sidebar
    das Limit deaktiviert hat, sonst den eingestellten Slider-Wert
    (0-10%, Default MAX_POSITION_PCT)."""
    if not st.session_state.get("pos_limit_enabled", True):
        return 100.0
    return float(st.session_state.get("pos_limit_pct", MAX_POSITION_PCT))

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


_UNIVERSE_INDEX: Optional[dict] = None
_UNIVERSE_REGION_INDEX: Optional[dict] = None


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


PARALLEL_FETCH_WORKERS = 5  # bewusst moderat gehalten - mehr gleichzeitige Yahoo-
# Requests erhöhen das Rate-Limit-Risiko (429/leere Antworten) überproportional
# zum Geschwindigkeitsgewinn. Ausgangswert zum Testen, ggf. nach Beobachtung
# des tatsächlichen Verhaltens anpassen.


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


def check_setup_alerts(ticker: str, rec: dict):
    """Setup-Alert bei klarer Kauf-Empfehlung mit hoher Konfidenz (>=75%)."""
    if not rec or not ticker:
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
                f"Ca. 50 % reduzieren (Last {_px(last)}, Stop {_px(stop)})."
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


def find_meta(ticker: str):
    if not ticker:
        return None
    for row in st.session_state.watchlist:
        if row["ticker"] == ticker:
            return row
    return catalog_lookup(ticker)


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


ATR_STOP_MULTIPLIER = 1.5  # Stop-Distanz = 1.5x ATR(14), Basis für dynamische Stops
# War 0.5 - für Daytrading ungewöhnlich eng, hat Positionen im normalen Intraday-
# Rauschen ausgestoppt statt der Bewegung Raum zu geben. 1.5-2.0x ATR ist der
# übliche Bereich; €-Risiko pro Trade bleibt trotz breiterem Stop unverändert,
# da calc_position_size() die Positionsgröße per R-Sizing gegenläufig anpasst
# (risk_budget / (leverage * stop_pct)) - nur die Positionsgröße (und damit auch
# der €-Gewinn bei Erreichen des kursbasierten Take-Profit-Ziels) sinkt mit.
DEFAULT_STOP_PCT = 0.03  # Fallback, falls kein ATR verfügbar ist (z.B. Datenausfall)


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


def mark_positions(watch: pd.DataFrame) -> float:
    px = (
        {r["Ticker"]: (r["Kurs"], r.get("High"), r.get("Low")) for _, r in watch.iterrows()}
        if not watch.empty
        else {}
    )
    open_pnl = 0.0
    for pos in st.session_state.positions:
        row = px.get(pos["ticker"])
        last, _day_high, _day_low = row if row else (None, None, None)
        if last is None:
            q = fetch_quote(find_meta(pos["ticker"])["yf"]) if find_meta(pos["ticker"]) else {}
            last = q.get("price")
        if last is None:
            continue
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
        gap_open = _session_gap_open(pos, float(last))

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

        hit = knocked or taken or gap_open_hit or gapped or (
            stop is not None and (
                (pos["side"] == "LONG" and float(pos["last"]) <= stop)
                or (pos["side"] == "SHORT" and float(pos["last"]) >= stop)
            )
        )
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


def rec_key(ticker: str, rec: dict) -> str:
    prod = rec.get("product") or {}
    wkn = prod.get("WKN") or prod.get("Typ") or "NA"
    return f"{ticker}|{rec.get('side')}|{wkn}"



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


def first_hour_warning(pos: dict) -> Optional[str]:
    label = str(pos.get("setup_label") or "").lower()
    if "or-breakout" not in label and "opening range" not in label:
        return None
    now = datetime.now(TZ_BERLIN).time()
    venue = str(pos.get("venue") or "").lower()
    in_first_hour = time(9, 0) <= now < time(10, 0) if ("xetra" in venue or "euro" in venue) else time(15, 30) <= now < time(16, 30)
    return "⚠️ Opening-Range-TP: erste Handelsstunde — OR/VWAP noch nicht ausgereift, erhöhte Volatilität." if in_first_hour else None


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


TRADE_MOOD_CHOICES = [
    "— bitte wählen —",
    "😌 Calm",
    "🤔 Neutral",
    "😟 Anxious",
    "🚀 Euphoric",
    "😤 Frustrated",
    "😴 Tired",
]


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


def _show_trade_confirmation(ticker, side, amount, price, fee, reason, action="Trade", pnl=None, wkn=None, typ=None, leverage=None, stop=None, **kwargs):
    """Kompatibilität: leitet auf den Bestätigungsdialog vor Ausführung um."""
    request_trade_confirmation(
        kind=kwargs.get("kind") or "buy",
        ticker=ticker, side=side, amount=amount, price=price, fee=fee,
        reason=reason, action=action, pnl=pnl, wkn=wkn, typ=typ,
        leverage=leverage, stop=stop, **{k: v for k, v in kwargs.items() if k != "kind"},
    )


def _trade_failure(msg):
    """Einheitliche, prominente Ablehnungsmeldung für nicht ausgeführte Trades."""
    st.error("🔴 **TRADE NICHT AUSGEFÜHRT**")
    st.warning(str(msg or "Unbekannter Ablehnungsgrund."))


def update_stop(pid: str, new_stop: float, event_type: str = "trail"):
    """Setzt den Paper-Stop und erzeugt bei echter Änderung ein Critical Event,
    damit der Nutzer denselben Stop im echten Broker nachzieht."""
    try:
        new_stop = float(new_stop)
    except (TypeError, ValueError):
        return
    for pos in st.session_state.positions:
        if pos.get("id") != pid:
            continue
        try:
            old_stop = float(pos.get("stop")) if pos.get("stop") is not None else None
        except (TypeError, ValueError):
            old_stop = None
        if old_stop is not None and abs(old_stop - new_stop) < 1e-9:
            return
        pos["stop"] = new_stop
        kind = event_type if event_type in CRITICAL_EVENT_LABELS else "trail"
        log_critical_trade_event(
            event_type=kind,
            ticker=pos.get("ticker") or "",
            side=pos.get("side"),
            details={
                "old_stop": old_stop,
                "new_stop": new_stop,
                "price": pos.get("last") or pos.get("entry"),
            },
        )
        save_app_state()
        return


def update_take(pid: str, new_take: float, event_type: str = "take-set"):
    for pos in st.session_state.get("positions") or []:
        if pos.get("id") != pid:
            continue
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


TZ_BERLIN = ZoneInfo("Europe/Berlin")


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


def get_daily_setup_contract() -> Optional[dict]:
    """Liefert den Tages-Setup-Vertrag, falls er HEUTE gesetzt wurde - sonst None
    (auch wenn noch ein Vertrag vom Vortag im Session-State hängt)."""
    contract = st.session_state.get("daily_setup_contract")
    today_str = datetime.now(TZ_BERLIN).date().isoformat()
    if contract and contract.get("date") == today_str:
        return contract
    return None


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


def close_subphase_label(subphase: Optional[str]) -> str:
    return {"prep": "Close Preparation", "final": "Final Close"}.get(subphase, "Close")


REGION_VENUE = {"USA": "US Regular", "UK": "LSE", "EU": "Xetra", "Schweiz": "Xetra"}


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
    x=_bt_prepare(df, int(params.get('opening_minutes',30)))
    if x.empty or len(x)<20:
        return pd.DataFrame(), pd.DataFrame()
    fee=float(params.get('fee_pct',0.0025))
    slip=float(params.get('slippage_pct',0.0010))
    risk_pct=float(params.get('risk_pct',0.01))
    take_r=float(params.get('take_r',2.0))
    capital=float(params.get('capital',10000.0))
    max_pos=float(params.get('max_position_pct',0.35))
    stop_mult=float(params.get('stop_atr',1.2))
    one_per_day=bool(params.get('one_trade_per_day',True))
    trades=[]; equity=[]; eq=capital; pos=None; traded_days=set()
    for i in range(1,len(x)):
        row=x.iloc[i]; day=row['_day']
        prev=x.iloc[i-1]
        if pos is not None:
            high=float(row['High']); low=float(row['Low']); close=float(row['Close'])
            exit_px=None; reason=None
            if pos['side']=='LONG':
                if low<=pos['stop']: exit_px=pos['stop']*(1-slip); reason='stop'
                elif high>=pos['take']: exit_px=pos['take']*(1-slip); reason='take'
            else:
                if high>=pos['stop']: exit_px=pos['stop']*(1+slip); reason='stop'
                elif low<=pos['take']: exit_px=pos['take']*(1+slip); reason='take'
            next_day = (i==len(x)-1) or (x.iloc[i+1]['_day']!=day)
            if exit_px is None and next_day:
                exit_px=close*(1-slip if pos['side']=='LONG' else 1+slip); reason='eod'
            if exit_px is not None:
                gross=(exit_px/pos['entry']-1.0)*(1 if pos['side']=='LONG' else -1)*pos['notional']
                exit_fee=pos['notional']*fee
                net=gross-pos['entry_fee']-exit_fee
                r=net/max(pos['risk_eur'],1e-9)
                eq+=net
                trades.append({'date':str(day),'side':pos['side'],'entry':pos['entry'],'exit':exit_px,'reason':reason,'gross_pnl':gross,'fees':pos['entry_fee']+exit_fee,'net_pnl':net,'R':r,'equity':eq})
                equity.append({'time':x.index[i],'equity':eq})
                pos=None
            continue
        if one_per_day and day in traded_days:
            continue
        if not bool(row['_active']):
            continue
        if pd.isna(row['VWAP']) or pd.isna(row['TR']):
            continue
        long_sig=float(row['Close'])>float(row['ORH']) and float(row['Close'])>float(row['VWAP']) and float(prev['Close'])<=float(prev['ORH'])
        short_sig=float(row['Close'])<float(row['ORL']) and float(row['Close'])<float(row['VWAP']) and float(prev['Close'])>=float(prev['ORL'])
        if not (long_sig or short_sig):
            continue
        side='LONG' if long_sig else 'SHORT'
        entry=float(row['Close'])*(1+slip if side=='LONG' else 1-slip)
        atr=max(float(row['TR']),entry*0.002)
        stop_dist=max(atr*stop_mult,entry*0.001)
        risk_eur=eq*risk_pct
        notional=min(eq*max_pos, risk_eur/(stop_dist/entry))
        if notional<=0: continue
        stop=entry-stop_dist if side=='LONG' else entry+stop_dist
        take=entry+take_r*stop_dist if side=='LONG' else entry-take_r*stop_dist
        entry_fee=notional*fee
        pos={'side':side,'entry':entry,'stop':stop,'take':take,'notional':notional,'risk_eur':risk_eur,'entry_fee':entry_fee}
        traded_days.add(day)
    t=pd.DataFrame(trades)
    e=pd.DataFrame(equity)
    if not e.empty:
        e['peak']=e['equity'].cummax(); e['drawdown']=e['equity']-e['peak']; e['drawdown_pct']=e['drawdown']/e['peak']*100
    return t,e


def metrics_from_trades(t: pd.DataFrame) -> dict:
    if t is None or t.empty:
        return {'trades':0,'win_rate':0.0,'profit_factor':0.0,'expectancy':0.0,'avg_r':0.0,'net':0.0,'gross':0.0,'fees':0.0,'max_dd':0.0}
    pnl=t['net_pnl'].astype(float); wins=pnl[pnl>0]; losses=pnl[pnl<0]
    gp=float(wins.sum()); gl=float(abs(losses.sum()))
    return {'trades':int(len(t)),'win_rate':float((pnl>0).mean()*100),'profit_factor':float(gp/gl) if gl>0 else float('inf'),'expectancy':float(pnl.mean()),'avg_r':float(t['R'].mean()),'net':float(pnl.sum()),'gross':float(t['gross_pnl'].sum()),'fees':float(t['fees'].sum()),'max_dd':0.0}


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
    rows=[]
    for opening,take,stop in product([15,30,45],[1.5,2.0,2.5],[0.8,1.2,1.6]):
        p={**base,'opening_minutes':opening,'take_r':take,'stop_atr':stop}
        t,e=run_backtest(df,p); m=metrics_from_trades(t)
        rows.append({'OR (Min)':opening,'Take R':take,'Stop ATR':stop,**m})
    out=pd.DataFrame(rows)
    return out.sort_values(['profit_factor','net','win_rate'],ascending=[False,False,False]) if not out.empty else out


def walk_forward_backtest(df: pd.DataFrame, base: dict, train_days: int = 20, test_days: int = 10) -> tuple[pd.DataFrame,pd.DataFrame]:
    if df is None or df.empty: return pd.DataFrame(),pd.DataFrame()
    x=df.copy(); days=sorted(pd.Series(x.index.date).unique())
    results=[]; all_test=[]
    for start in range(0,max(0,len(days)-train_days-test_days+1),test_days):
        tr_days=days[start:start+train_days]; te_days=days[start+train_days:start+train_days+test_days]
        if len(tr_days)<train_days or len(te_days)<test_days: break
        tr=x[pd.Series(x.index.date,index=x.index).isin(tr_days)]
        te=x[pd.Series(x.index.date,index=x.index).isin(te_days)]
        grid=compare_parameters(tr,base)
        if grid.empty: continue
        best=grid.iloc[0]
        p={**base,'opening_minutes':int(best['OR (Min)']),'take_r':float(best['Take R']),'stop_atr':float(best['Stop ATR'])}
        tt,ee=run_backtest(te,p); m=metrics_from_trades(tt)
        results.append({'train_start':str(tr_days[0]),'train_end':str(tr_days[-1]),'test_start':str(te_days[0]),'test_end':str(te_days[-1]),'OR':p['opening_minutes'],'TakeR':p['take_r'],'StopATR':p['stop_atr'],**m})
        if not tt.empty: all_test.append(tt)
    return pd.DataFrame(results), (pd.concat(all_test,ignore_index=True) if all_test else pd.DataFrame())


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
            st.session_state.bt_params={'opening_minutes':opening,'take_r':take_r,'stop_atr':stop_atr,'risk_pct':risk/100,'fee_pct':0.0025,'slippage_pct':0.001,'capital':CAPITAL,'max_position_pct':0.35,'one_trade_per_day':one}
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
        check_setup_alerts(focus, rec or {})

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
            _ef = st.session_state.get("event_source_flags", {"fred": True, "bls": True, "eurostat": True, "sec": True})
            _ec1, _ec2, _ec3, _ec4 = st.columns(4)
            with _ec1:
                _fred_on = st.toggle("FRED", value=bool(_ef.get("fred", True)), disabled=not bool(FRED_API_KEY), key="event_src_fred")
            with _ec2:
                _bls_on = st.toggle("BLS", value=bool(_ef.get("bls", True)), key="event_src_bls")
            with _ec3:
                _eu_on = st.toggle("EUROSTAT", value=bool(_ef.get("eurostat", True)), key="event_src_eurostat")
            with _ec4:
                _sec_on = st.toggle("SEC Form 4", value=bool(_ef.get("sec", True)), key="event_src_sec")
            st.session_state.event_source_flags = {"fred": _fred_on, "bls": _bls_on, "eurostat": _eu_on, "sec": _sec_on}
            if not FRED_API_KEY:
                st.caption("FRED deaktiviert: FRED_API_KEY fehlt.")
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
            st.markdown(
                f"""
                <div style="background:{pnl_bg};border:1px solid {pnl_border};
                            border-radius:8px;padding:0.5rem 0.75rem;margin-bottom:0.3rem;">
                    {sev_icon} <b>{pos['ticker']}</b> {pos.get('wkn') or '—'} · {pos['side']} · 
                    {held:.0f} € · Last {last_txt} · P&amp;L {pnl_val:+.2f} €
                </div>
                """,
                unsafe_allow_html=True,
            )
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
                new_stop = st.number_input(
                    "Stop",
                    value=float(pos.get("stop") or 0),
                    key=f"stop_{pid}",
                    format="%.4f",
                )
                new_take = st.number_input("Take-Profit", value=float(pos.get("take") or 0), key=f"take_{pid}", format="%.4f")
            with c:
                if st.button("Take setzen", key=f"set_take_{pid}", use_container_width=True):
                    if update_take(pid, new_take):
                        st.success("Take-Profit gesetzt.")
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
            with d:
                sell_c1, sell_c2, sell_c3 = st.columns(3)
                with sell_c1:
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
                with sell_c2:
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
                with sell_c3:
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

def main():
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
