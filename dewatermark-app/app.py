import io
import time
from pathlib import Path

import requests
import streamlit as st

BASE_URL = "https://platform.dewatermark.ai"
API_KEY_NAME = "DEWATERMARK_API_KEY"
MAX_FILE_MB = 200
POLL_INTERVAL = 3
POLL_TIMEOUT = 15 * 60

st.set_page_config(page_title="DeWatermarkApp", page_icon="💧", layout="centered")


def get_api_key() -> str:
    try:
        return st.secrets[API_KEY_NAME]
    except Exception:
        return ""


def api_headers() -> dict:
    key = get_api_key()
    return {"X-API-KEY": key}


def get_credit_balance() -> float | None:
    response = requests.get(
        f"{BASE_URL}/api/creditInfo",
        headers=api_headers(),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("data", {}).get("available_credit")


def create_upload_task() -> dict:
    response = requests.post(
        f"{BASE_URL}/api/video/v1/upload",
        headers=api_headers(),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def upload_video(upload_url: str, video_bytes: bytes) -> None:
    response = requests.put(
        upload_url,
        data=video_bytes,
        headers={"Content-Type": "application/octet-stream"},
        timeout=180,
    )
    response.raise_for_status()


def submit_task(task_id: str) -> dict:
    response = requests.post(
        f"{BASE_URL}/api/video/v1/tasks",
        headers=api_headers(),
        files={"task_id": (None, task_id)},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def poll_task(task_id: str) -> dict:
    started = time.monotonic()
    while time.monotonic() - started < POLL_TIMEOUT:
        response = requests.get(
            f"{BASE_URL}/api/video/v1/tasks/{task_id}",
            headers=api_headers(),
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        status = payload.get("status")
        progress = float(payload.get("progress") or 0)
        st.session_state["progress"] = max(0.0, min(progress, 1.0))

        if status in {"COMPLETED", "FAILED"}:
            return payload
        time.sleep(POLL_INTERVAL)
    raise TimeoutError("DeWatermark did not finish within the polling timeout.")


st.title("💧 DeWatermarkApp")
st.caption("Eigene kleine Test-App für die DeWatermark Video API")

api_key = get_api_key()
if not api_key:
    st.error(
        f"Kein API-Key gefunden. Bitte in Streamlit Secrets den Eintrag "
        f"`{API_KEY_NAME}` anlegen."
    )
    st.stop()

with st.sidebar:
    st.header("API")
    if st.button("Credits aktualisieren", use_container_width=True):
        st.rerun()
    try:
        credits = get_credit_balance()
        st.metric("API-Credits", credits)
    except requests.HTTPError as exc:
        st.error(f"Credit-Abfrage fehlgeschlagen: {exc}")
    except requests.RequestException as exc:
        st.error(f"Netzwerkfehler: {exc}")

st.markdown("### Video testen")
uploaded = st.file_uploader(
    "MP4-Video auswählen",
    type=["mp4"],
    help="DeWatermark erwartet für Video MP4 mit 24 fps und maximal 9 Minuten.",
)

if uploaded:
    size_mb = uploaded.size / (1024 * 1024)
    st.write(f"**Datei:** {uploaded.name} · {size_mb:.1f} MB")
    if size_mb > MAX_FILE_MB:
        st.error(f"Datei ist größer als {MAX_FILE_MB} MB.")
        st.stop()

    video_bytes = uploaded.getvalue()
    st.video(video_bytes)

    if st.button("▶ Video mit DeWatermark verarbeiten", type="primary", use_container_width=True):
        try:
            with st.status("DeWatermark-Verarbeitung läuft …", expanded=True) as status:
                st.write("1/4 Upload-Task anlegen …")
                task = create_upload_task()
                task_id = task["task_id"]
                upload_url = task["upload_signed_url"]
                st.write(f"Task: `{task_id}`")

                st.write("2/4 Video hochladen …")
                upload_video(upload_url, video_bytes)

                st.write("3/4 Verarbeitung starten …")
                submit_task(task_id)

                st.write("4/4 Ergebnis abwarten …")
                progress_bar = st.progress(0.0)
                started = time.monotonic()
                result = None
                while time.monotonic() - started < POLL_TIMEOUT:
                    response = requests.get(
                        f"{BASE_URL}/api/video/v1/tasks/{task_id}",
                        headers=api_headers(),
                        timeout=30,
                    )
                    response.raise_for_status()
                    result = response.json()
                    progress = float(result.get("progress") or 0)
                    progress_bar.progress(max(0.0, min(progress, 1.0)))
                    current_status = result.get("status")
                    if current_status in {"COMPLETED", "FAILED"}:
                        break
                    time.sleep(POLL_INTERVAL)

                if not result or result.get("status") not in {"COMPLETED", "FAILED"}:
                    raise TimeoutError("DeWatermark did not finish within the polling timeout.")

                if result.get("status") == "FAILED":
                    status.update(label="Verarbeitung fehlgeschlagen", state="error")
                    st.error("DeWatermark meldet FAILED. Laut API-Dokumentation werden Credits bei einem fehlgeschlagenen Task zurückerstattet.")
                    st.json(result)
                else:
                    status.update(label="Verarbeitung abgeschlossen", state="complete")
                    result_url = result.get("download_signed_url")
                    if not result_url:
                        st.error("Kein Download-Link in der API-Antwort gefunden.")
                        st.json(result)
                    else:
                        cleaned = requests.get(result_url, timeout=180)
                        cleaned.raise_for_status()
                        output_bytes = cleaned.content
                        st.success("Video erfolgreich verarbeitet.")
                        st.video(output_bytes)
                        st.download_button(
                            "⬇ Bereinigtes Video herunterladen",
                            data=output_bytes,
                            file_name=f"dewatermarked_{Path(uploaded.name).name}",
                            mime="video/mp4",
                            use_container_width=True,
                        )

                        try:
                            credits_after = get_credit_balance()
                            st.metric("API-Credits nach Verarbeitung", credits_after)
                        except requests.RequestException:
                            pass

        except requests.HTTPError as exc:
            st.error(f"DeWatermark API HTTP-Fehler: {exc}")
        except requests.RequestException as exc:
            st.error(f"Netzwerkfehler: {exc}")
        except Exception as exc:
            st.error(f"Fehler: {exc}")
else:
    st.info("Lade den vorbereiteten Maria-Clip hoch, um den ersten API-Test zu starten.")

st.divider()
st.caption("Der API-Key wird ausschließlich über Streamlit Secrets gelesen und nicht im Quellcode gespeichert.")
