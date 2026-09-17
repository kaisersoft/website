from __future__ import annotations

import asyncio
import base64
import io
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

import requests
import streamlit as st
from PIL import Image
from runware import Runware


st.set_page_config(
    page_title="New Horizon Video Generator",
    page_icon="🎬",
    layout="wide",
)


@dataclass(frozen=True)
class ModelConfig:
    label: str
    model_id: str
    durations: tuple[int, ...]
    resolutions: tuple[str, ...]
    fps: tuple[int, ...]
    notes: str


MODELS = {
    "P-Video-2": ModelConfig(
        label="P-Video-2",
        model_id="prunaai:p-video@2",
        durations=(3, 4, 5, 6, 7, 8, 10),
        resolutions=("720p", "1080p"),
        fps=(24, 48),
        notes="Quality-focused model with strong input-image consistency. 24 fps is native.",
    ),
    "Vidu 2.0": ModelConfig(
        label="Vidu 2.0",
        model_id="vidu:2@0",
        durations=(4, 8),
        resolutions=("720p", "1080p"),
        fps=(24,),
        notes="Fast image-to-video model. Duration and aspect-ratio rules are model-specific.",
    ),
    "Wan 3.0": ModelConfig(
        label="Wan 3.0",
        model_id="alibaba:wan@3.0",
        durations=(5, 6, 8, 10),
        resolutions=("720p", "1080p"),
        fps=(24,),
        notes="Higher-cost general video model; use for quality comparison.",
    ),
    "Runway Gen-4.5": ModelConfig(
        label="Runway Gen-4.5",
        model_id="runway:gen4.5",
        durations=(5, 8, 10),
        resolutions=("720p",),
        fps=(24,),
        notes="Premium comparison model; image-to-video supports a first frame.",
    ),
}


def get_api_key() -> str | None:
    try:
        key = st.secrets.get("RUNWARE_API_KEY")
    except Exception:
        key = None
    if not key:
        key = os.getenv("RUNWARE_API_KEY")
    return str(key).strip() if key else None


def prepare_image(uploaded: Any, target_ratio: str) -> tuple[bytes, str]:
    image = Image.open(uploaded).convert("RGB")
    target_w, target_h = {
        "9:16": (704, 1280),
        "16:9": (1280, 720),
        "1:1": (960, 960),
    }[target_ratio]

    source_ratio = image.width / image.height
    desired_ratio = target_w / target_h

    if source_ratio > desired_ratio:
        crop_w = int(image.height * desired_ratio)
        left = (image.width - crop_w) // 2
        image = image.crop((left, 0, left + crop_w, image.height))
    else:
        crop_h = int(image.width / desired_ratio)
        top = max(0, (image.height - crop_h) // 2)
        image = image.crop((0, top, image.width, top + crop_h))

    image = image.resize((target_w, target_h), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def data_uri(image_bytes: bytes, mime: str) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def run_generation(api_key: str, config: ModelConfig, image_uri: str, prompt: str,
                   duration: int, resolution: str, fps: int, include_audio: bool,
                   seed: int | None) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        async with Runware(api_key=api_key, transport="rest") as client:
            params: dict[str, Any] = {
                "taskType": "videoInference",
                "model": config.model_id,
                "positivePrompt": prompt,
                "inputs": {"frameImages": [{"image": image_uri, "frame": "first"}]},
                "duration": duration,
                "deliveryMethod": "async",
                "includeCost": True,
                "outputType": "URL",
            }

            if config.model_id == "prunaai:p-video@2":
                params["resolution"] = resolution
                params["fps"] = fps
                params["settings"] = {"audio": include_audio, "promptUpsampling": True}
            elif config.model_id == "vidu:2@0":
                if duration == 8:
                    params["width"], params["height"] = 1280, 720
                else:
                    params["width"], params["height"] = 720, 1280
                params["providerSettings"] = {"vidu": {"movementAmplitude": "small"}}
            elif config.model_id == "alibaba:wan@3.0":
                params["resolution"] = resolution
                params["settings"] = {"audio": include_audio}
            elif config.model_id == "runway:gen4.5":
                params["width"], params["height"] = (720, 1280)

            if seed is not None:
                params["seed"] = seed

            result = await client.run(params)
            if not result:
                raise RuntimeError("Runware returned no result.")
            return dict(result[0])

    return asyncio.run(_run())


def download_video(url: str) -> bytes:
    response = requests.get(url, timeout=180)
    response.raise_for_status()
    return response.content


def convert_to_24fps(video_bytes: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "source.mp4")
        target = os.path.join(tmp, "output.mp4")
        with open(source, "wb") as handle:
            handle.write(video_bytes)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", source, "-vf", "fps=24", "-c:v", "libx264",
                 "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "192k",
                 "-movflags", "+faststart", target],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return video_bytes
        with open(target, "rb") as handle:
            return handle.read()


st.title("🎬 New Horizon Video Generator")
st.caption("Internal tool for Runware image-to-video generation")

api_key = get_api_key()
if not api_key:
    st.error("RUNWARE_API_KEY is missing. Add it to Streamlit Secrets.")
    st.stop()

with st.sidebar:
    st.header("Generation")
    model_name = st.selectbox("Model", list(MODELS.keys()), index=0)
    config = MODELS[model_name]
    st.caption(config.notes)

    target_ratio = st.selectbox("Format", ["9:16", "16:9", "1:1"], index=0)
    duration = st.selectbox("Duration", config.durations, index=0)
    resolution = st.selectbox("Resolution", config.resolutions, index=0)
    fps = st.selectbox("FPS", config.fps, index=0)
    include_audio = st.checkbox("Generate audio", value=True)
    seed_input = st.number_input("Seed (0 = random)", min_value=0, max_value=2147483647, value=0, step=1)
    postprocess_24 = st.checkbox("Post-process to 24 fps", value=False,
                                 help="Use this for models that do not natively produce 24 fps.")

uploaded = st.file_uploader("Persona image", type=["jpg", "jpeg", "png", "webp"])

prompt = st.text_area(
    "Motion prompt",
    value=(
        "Bring the person naturally to life. Keep the face, identity, hairstyle, clothing and "
        "overall composition stable. Subtle realistic head and body movement, natural blinking, "
        "gentle breathing and a slight confident smile. Slow cinematic camera movement, realistic "
        "lighting, no scene change, no morphing, no extra people, no text or logos."
    ),
    height=150,
)

if uploaded:
    preview_bytes, _ = prepare_image(uploaded, target_ratio)
    col1, col2 = st.columns(2)
    with col1:
        st.image(preview_bytes, caption=f"Prepared input – {target_ratio}", use_container_width=True)
    with col2:
        st.info(
            "The uploaded image is cropped to the selected format before being sent to Runware. "
            "For New Horizon Shorts, 9:16 is the recommended setting."
        )

if st.button("🎬 Generate video", type="primary", disabled=uploaded is None or not prompt.strip()):
    try:
        image_bytes, mime = prepare_image(uploaded, target_ratio)
        image_uri = data_uri(image_bytes, mime)
        seed = seed_input or None

        with st.spinner(f"Generating with {model_name}…"):
            result = run_generation(
                api_key=api_key,
                config=config,
                image_uri=image_uri,
                prompt=prompt.strip(),
                duration=duration,
                resolution=resolution,
                fps=fps,
                include_audio=include_audio,
                seed=seed,
            )
            video_url = result.get("videoURL")
            if not video_url:
                raise RuntimeError(f"Runware returned no video URL: {result}")
            video_bytes = download_video(video_url)

        if postprocess_24 and fps != 24:
            with st.spinner("Converting output to 24 fps…"):
                video_bytes = convert_to_24fps(video_bytes)

        st.session_state["generated_video"] = video_bytes
        st.session_state["generated_result"] = result

    except Exception as exc:
        st.error(f"Generation failed: {exc}")

if "generated_video" in st.session_state:
    st.subheader("Result")
    result = st.session_state.get("generated_result", {})
    video_bytes = st.session_state["generated_video"]
    st.video(video_bytes)

    cost = result.get("cost")
    if cost is not None:
        st.success(f"Runware cost: ${float(cost):.4f}")
    if result.get("taskUUID"):
        st.caption(f"Task: {result['taskUUID']}")

    st.download_button(
        "⬇️ Download MP4",
        data=video_bytes,
        file_name="new_horizon_video.mp4",
        mime="video/mp4",
        type="primary",
    )

st.divider()
st.caption("Runware API key is read from Streamlit Secrets and is never written to the repository.")
