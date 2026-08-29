import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    page_title="Kaisersoft.ai",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    html, body, [data-testid="stAppViewContainer"], .stApp {
        margin: 0 !important;
        padding: 0 !important;
        width: 100% !important;
        height: 100% !important;
        min-height: 100vh !important;
        overflow: hidden !important;
        background: #000 !important;
    }
    header, footer, #MainMenu,
    [data-testid="stHeader"], [data-testid="stToolbar"],
    [data-testid="stDecoration"] {
        display: none !important;
        visibility: hidden !important;
    }
    .block-container {
        padding: 0 !important;
        margin: 0 !important;
        max-width: none !important;
        height: 100vh !important;
    }
    iframe[title="st.components.v1.html"] {
        position: fixed !important;
        inset: 0 !important;
        width: 100vw !important;
        height: 100vh !important;
        min-height: 0 !important;
        border: 0 !important;
        z-index: 999999 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
    width: 100%; height: 100%; overflow: hidden; background: #000;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
}
body { overscroll-behavior: none; }

#app {
    position: fixed; inset: 0; width: 100%; height: 100%; overflow: hidden;
    background:
        radial-gradient(circle at 50% 46%, rgba(0,229,255,.055), transparent 34%),
        #000;
}

/* ---------------- LOADING ---------------- */
#loading {
    position: absolute; inset: 0; z-index: 20;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    padding: clamp(18px, 4vw, 40px);
    background: #000;
    transition: opacity .95s ease, visibility .95s ease;
}
#loading.done { opacity: 0; visibility: hidden; pointer-events: none; }

.loading-stack {
    width: min(90vw, 520px);
    display: flex; flex-direction: column; align-items: center;
    transform: translateY(clamp(-18px, -2vh, -8px));
}

/* Exact Kaisersoft/AI mark used by AstroWeather */
.brand-mark {
    width: clamp(110px, 20vw, 185px);
    height: clamp(110px, 20vw, 185px);
    filter: drop-shadow(0 0 15px rgba(0,229,255,.48)) drop-shadow(0 0 38px rgba(0,229,255,.18));
    margin-bottom: clamp(8px, 1.8vh, 18px);
}

.brand-name {
    color: #fff;
    font-size: clamp(1.7rem, 5vw, 3.15rem);
    line-height: 1;
    font-weight: 700;
    letter-spacing: .075em;
    text-align: center;
    text-shadow: 0 0 16px rgba(0,229,255,.18);
    margin-bottom: clamp(22px, 4.2vh, 38px);
}

.loading-text {
    color: rgba(255,255,255,.9);
    font-size: clamp(.82rem, 2vw, 1.08rem);
    letter-spacing: .14em;
    text-align: center;
    margin-bottom: clamp(15px, 2.4vh, 22px);
}

.progress-wrap { width: min(440px, 78vw); }
.progress-track {
    width: 100%; height: clamp(3px, .45vh, 5px);
    overflow: hidden; border-radius: 999px;
    background: rgba(255,255,255,.11);
    box-shadow: 0 0 14px rgba(0,229,255,.08);
}
.progress-bar {
    width: 0%; height: 100%; border-radius: inherit;
    background: #00e5ff;
    box-shadow: 0 0 8px rgba(0,229,255,.95), 0 0 25px rgba(0,229,255,.45);
}
.progress-percent {
    color: rgba(255,255,255,.38);
    font-size: .66rem;
    letter-spacing: .12em;
    text-align: center;
    margin-top: 8px;
}

/* ---------------- FIREFLIES ---------------- */
#experience {
    position: absolute; inset: 0; z-index: 5;
    opacity: 0; transition: opacity 1.2s ease;
}
#experience.visible { opacity: 1; }
#fireflies { position: absolute; inset: 0; width: 100%; height: 100%; }

.final-center {
    position: absolute; inset: 0; z-index: 10;
    display: flex; align-items: center; justify-content: center;
    padding: 20px; pointer-events: none;
}
.final-stack {
    display: flex; flex-direction: column; align-items: center;
    transform: translateY(-2vh);
}
.final-mark {
    width: clamp(82px, 14vw, 135px);
    height: clamp(82px, 14vw, 135px);
    filter: drop-shadow(0 0 16px rgba(0,229,255,.48));
    margin-bottom: 15px;
}
.final-name {
    color: #fff;
    font-size: clamp(1.55rem, 4.5vw, 2.8rem);
    font-weight: 700;
    letter-spacing: .075em;
    text-shadow: 0 0 18px rgba(0,229,255,.22);
}

/* ---------------- INSTAGRAM ---------------- */
.social {
    position: absolute; z-index: 15;
    left: 50%; bottom: max(18px, 4vh);
    transform: translateX(-50%);
}
.instagram {
    width: 42px; height: 42px;
    display: flex; align-items: center; justify-content: center;
    color: #fff; opacity: .62; text-decoration: none;
    transition: opacity .2s ease, transform .2s ease;
}
.instagram:hover, .instagram:focus { opacity: 1; transform: scale(1.08); }
.instagram svg { width: 27px; height: 27px; fill: none; stroke: currentColor; stroke-width: 1.8; }

/* Keep everything inside short laptop/tablet viewports */
@media (max-height: 700px) {
    .brand-mark { width: 105px; height: 105px; margin-bottom: 8px; }
    .brand-name { font-size: 1.65rem; margin-bottom: 20px; }
    .loading-text { margin-bottom: 14px; }
    .final-mark { width: 78px; height: 78px; }
    .final-name { font-size: 1.5rem; }
}
@media (max-width: 480px) {
    .brand-name, .final-name { letter-spacing: .045em; }
    .loading-text { letter-spacing: .075em; }
    .progress-wrap { width: 82vw; }
}
@media (max-height: 560px) and (orientation: landscape) {
    .loading-stack { transform: none; }
    .brand-mark { width: 72px; height: 72px; margin-bottom: 5px; }
    .brand-name { font-size: 1.25rem; margin-bottom: 12px; }
    .loading-text { margin-bottom: 9px; font-size: .72rem; }
    .progress-percent { margin-top: 5px; }
}
</style>
</head>
<body>
<div id="app">

<section id="loading">
    <div class="loading-stack">
        <svg class="brand-mark" viewBox="0 0 200 200" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="Kaisersoft AI logo">
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

        <div class="brand-name">kaisersoft.ai</div>
        <div class="loading-text">something Powerful is loading...</div>

        <div class="progress-wrap">
            <div class="progress-track"><div id="progress-bar" class="progress-bar"></div></div>
            <div id="progress-percent" class="progress-percent">0%</div>
        </div>
    </div>
</section>

<section id="experience">
    <canvas id="fireflies"></canvas>
    <div class="final-center">
        <div class="final-stack">
            <svg class="final-mark" viewBox="0 0 200 200" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                <circle cx="48" cy="30" r="5" fill="#00E5FF"/><circle cx="48" cy="70" r="4.5" fill="#00E5FF"/><circle cx="48" cy="100" r="6" fill="#00E5FF"/><circle cx="48" cy="130" r="4.5" fill="#00E5FF"/><circle cx="48" cy="170" r="5" fill="#00E5FF"/>
                <circle cx="78" cy="70" r="4" fill="#00E5FF"/><circle cx="95" cy="55" r="3.5" fill="#00E5FF"/><circle cx="112" cy="40" r="4.5" fill="#00E5FF"/><circle cx="135" cy="28" r="5" fill="#00E5FF"/>
                <circle cx="78" cy="130" r="4" fill="#00E5FF"/><circle cx="95" cy="145" r="3.5" fill="#00E5FF"/><circle cx="112" cy="160" r="4.5" fill="#00E5FF"/><circle cx="135" cy="172" r="5" fill="#00E5FF"/><circle cx="70" cy="100" r="3.5" fill="#00E5FF"/><circle cx="100" cy="100" r="5" fill="#00E5FF"/>
                <g stroke="#00E5FF" stroke-width="1.4" stroke-linecap="round" opacity="0.9">
                    <line x1="48" y1="30" x2="48" y2="70"/><line x1="48" y1="70" x2="48" y2="100"/><line x1="48" y1="100" x2="48" y2="130"/><line x1="48" y1="130" x2="48" y2="170"/><line x1="48" y1="100" x2="70" y2="100"/><line x1="70" y1="100" x2="100" y2="100"/>
                    <line x1="48" y1="70" x2="78" y2="70"/><line x1="78" y1="70" x2="95" y2="55"/><line x1="95" y1="55" x2="112" y2="40"/><line x1="112" y1="40" x2="135" y2="28"/><line x1="48" y1="100" x2="95" y2="55"/><line x1="100" y1="100" x2="112" y2="40"/>
                    <line x1="48" y1="130" x2="78" y2="130"/><line x1="78" y1="130" x2="95" y2="145"/><line x1="95" y1="145" x2="112" y2="160"/><line x1="112" y1="160" x2="135" y2="172"/><line x1="48" y1="100" x2="95" y2="145"/><line x1="100" y1="100" x2="112" y2="160"/>
                </g>
            </svg>
            <div class="final-name">kaisersoft.ai</div>
        </div>
    </div>

    <div class="social">
        <a class="instagram" href="https://www.instagram.com/kaisersoft.ai" target="_blank" rel="noopener noreferrer" aria-label="Kaisersoft AI on Instagram">
            <svg viewBox="0 0 24 24" aria-hidden="true">
                <rect x="3.2" y="3.2" width="17.6" height="17.6" rx="5"></rect>
                <circle cx="12" cy="12" r="4.15"></circle>
                <circle cx="17.55" cy="6.55" r="1" fill="currentColor" stroke="none"></circle>
            </svg>
        </a>
    </div>
</section>
</div>

<script>
const canvas = document.getElementById('fireflies');
const ctx = canvas.getContext('2d');
let W = 0, H = 0, D = 1, particles = [], raf = null;

function resize() {
    D = Math.min(window.devicePixelRatio || 1, 2);
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = Math.floor(W * D);
    canvas.height = Math.floor(H * D);
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(D, 0, 0, D, 0, 0);
    createParticles();
}

function createParticles() {
    const count = Math.max(28, Math.min(105, Math.round((W * H) / 15000)));
    particles = Array.from({length: count}, () => ({
        x: Math.random() * W,
        y: Math.random() * H,
        vx: (Math.random() - .5) * .24,
        vy: (Math.random() - .5) * .24,
        r: .7 + Math.random() * 1.8,
        phase: Math.random() * Math.PI * 2,
        speed: .008 + Math.random() * .018,
        glow: .35 + Math.random() * .65
    }));
}

function drawFireflies() {
    ctx.clearRect(0, 0, W, H);
    for (const p of particles) {
        p.x += p.vx; p.y += p.vy; p.phase += p.speed;
        if (p.x < -20) p.x = W + 20;
        if (p.x > W + 20) p.x = -20;
        if (p.y < -20) p.y = H + 20;
        if (p.y > H + 20) p.y = -20;

        const alpha = p.glow * (.35 + .65 * ((Math.sin(p.phase) + 1) / 2));
        const glow = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r * 9);
        glow.addColorStop(0, `rgba(0,229,255,${alpha})`);
        glow.addColorStop(.18, `rgba(0,229,255,${alpha * .45})`);
        glow.addColorStop(1, 'rgba(0,229,255,0)');
        ctx.beginPath(); ctx.fillStyle = glow;
        ctx.arc(p.x, p.y, p.r * 9, 0, Math.PI * 2); ctx.fill();
        ctx.beginPath(); ctx.fillStyle = `rgba(220,255,255,${Math.min(1, alpha + .12)})`;
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill();
    }
    raf = requestAnimationFrame(drawFireflies);
}

window.addEventListener('resize', resize, {passive: true});
resize();

const loading = document.getElementById('loading');
const experience = document.getElementById('experience');
const bar = document.getElementById('progress-bar');
const percent = document.getElementById('progress-percent');
const start = performance.now();
const duration = 3600;

function loadingLoop(now) {
    const progress = Math.min(100, ((now - start) / duration) * 100);
    bar.style.width = progress + '%';
    percent.textContent = Math.round(progress) + '%';

    if (progress < 100) {
        requestAnimationFrame(loadingLoop);
        return;
    }

    setTimeout(() => {
        loading.classList.add('done');
        experience.classList.add('visible');
        if (!raf) raf = requestAnimationFrame(drawFireflies);
    }, 180);
}

requestAnimationFrame(loadingLoop);
</script>
</body>
</html>'''

components.html(HTML, height=767, scrolling=False)
