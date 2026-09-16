# DeWatermarkApp

Kleine Streamlit-Test-App für die DeWatermark Video API.

## Streamlit Secret

In Streamlit Cloud unter **Settings → Secrets** anlegen:

```toml
DEWATERMARK_API_KEY = "dein-api-key"
```

Der Key wird nicht im Repository gespeichert.

## App starten

Entry point:

```text
dewatermark-app/app.py
```

Die App fragt den API-Creditstand ab, lädt ein MP4 über den von DeWatermark bereitgestellten Signed Upload URL hoch, startet den Video-Task, wartet auf `COMPLETED` und stellt das bereinigte MP4 zum Download bereit.

DeWatermark verlangt für Video laut aktueller API-Dokumentation MP4 mit 24 fps und maximal 9 Minuten. Die API berechnet aktuell 0,5 Credits pro Sekunde Video; fehlgeschlagene oder Timeout-Tasks werden laut Dokumentation erstattet.
