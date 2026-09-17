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
- API key entered directly in the GUI; no repository secret is required

## API key

Enter your own Runware API key in the password field when the app starts.

The key is not stored in GitHub, Streamlit Secrets, or a project configuration file. It is used only during the current Streamlit session for requests to Runware.

## Run locally

```bash
cd runware-video
pip install -r requirements.txt
streamlit run app.py
```

The app requires Python 3.11+ for the current Runware SDK.

## Deployment

Deploy `runware-video/app.py` as its own Streamlit app. The repository can remain the public Kaisersoft website repository; the Streamlit app uses only the files inside this folder.

Each user supplies their own Runware API key in the GUI. This prevents the public Streamlit deployment from consuming a centrally stored Kaisersoft Runware balance.
