# New Horizon Video Generator

Small internal Streamlit frontend for Runware image-to-video generation.

## Features

- Persona image upload
- 9:16 / 16:9 / 1:1 input preparation
- Model selection
- Duration, resolution and FPS controls where supported
- Motion prompt
- Optional generated audio
- Optional seed
- Optional 24 fps post-processing for models without native 24 fps output
- MP4 preview and download
- Runware cost shown when returned by the API

## Streamlit Secrets

Create a Streamlit secret named:

```toml
RUNWARE_API_KEY = "your-runware-api-key"
```

Do not commit the real API key to GitHub.

## Run locally

```bash
cd runware-video
pip install -r requirements.txt
streamlit run app.py
```

The app requires Python 3.11+ for the current Runware SDK.

## Deployment

Deploy `runware-video/app.py` as its own Streamlit app. The repository can remain the public website repository; the Streamlit app uses only the files inside this folder.

Runware's Python SDK supports REST transport for one-off generation requests and async delivery/polling for video jobs. Input images can be sent as data URIs/base64, so the app does not need a separate image-storage service.
