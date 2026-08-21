"""
Kaisersoft.ai – Coming Soon
CI 2 Minimal (Black + Cyan) · Laser-Show Animation
Streamlit Webapp — RESPONSIVE VERSION
"""

import streamlit as st

st.set_page_config(
    page_title="Kaisersoft.ai – Coming Soon",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Hide Streamlit chrome for immersive full-screen look + make iframe fill viewport
st.markdown(
    """
    <style>
    /* Hide Streamlit UI chrome */
    #MainMenu {visibility: hidden;}
    header {visibility: hidden;}
    footer {visibility: hidden;}
    .stApp { background: #000000; }

    /* Remove default Streamlit padding & margins */
    .block-container {
        padding: 0 !important;
        margin: 0 !important;
        max-width: 100% !important;
    }
    .st-emotion-cache-1jicfl2 {
        padding: 0 !important;
    }

    /* Force the HTML component iframe to fill the entire viewport */
    iframe[title="st.components.v1.html"] {
        width: 100vw !important;
        height: 100vh !important;
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        border: none !important;
        display: block !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

LASER_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Kaisersoft.ai</title>
<style>
/* ── RESET & FULL VIEWPORT ── */
*, *::before, *::after {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}
html, body {
    width: 100%;
    height: 100%;
    overflow: hidden;
    background: #000;
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
}

/* ── CANVAS BACKGROUND ── */
#laser-canvas {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    z-index: 1;
    display: block;
}

/* ── CENTER CONTENT ── */
.content {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    z-index: 10;
    text-align: center;
    width: 90%;
    max-width: 900px;
    pointer-events: none; /* let clicks pass through to canvas if needed */
}

.content h1 {
    color: #00ffff;
    font-size: clamp(2.2rem, 10vw, 6rem);
    font-weight: 900;
    letter-spacing: 0.08em;
    text-shadow:
        0 0 10px rgba(0, 255, 255, 0.4),
        0 0 40px rgba(0, 255, 255, 0.2);
    margin-bottom: 0.3em;
    line-height: 1.1;
}

.content .tagline {
    color: #ffffff;
    font-size: clamp(0.9rem, 3vw, 1.4rem);
    font-weight: 300;
    letter-spacing: 0.25em;
    text-transform: uppercase;
    opacity: 0.85;
    text-shadow: 0 0 10px rgba(255, 255, 255, 0.2);
}

/* ── OPTIONAL: BOTTOM BADGE ── */
.badge {
    position: fixed;
    bottom: 4vh;
    left: 50%;
    transform: translateX(-50%);
    z-index: 10;
    color: #00ffff;
    font-size: clamp(0.7rem, 1.5vw, 0.9rem);
    letter-spacing: 0.2em;
    opacity: 0.6;
    pointer-events: none;
}

/* ── MEDIA QUERIES FOR FINE-TUNING ── */
@media (max-width: 480px) {
    .content h1 { letter-spacing: 0.04em; }
    .content .tagline { letter-spacing: 0.15em; }
}

@media (min-width: 2000px) {
    .content { max-width: 1200px; }
}
</style>
</head>
<body>
<canvas id="laser-canvas"></canvas>

<div class="content">
    <h1>Kaisersoft.ai</h1>
    <p class="tagline">Build · Automate · Scale</p>
</div>

<div class="badge">COMING SOON</div>

<script>
/* ═══════════════════════════════════════════════
   RESPONSIVE CANVAS SETUP
   ═══════════════════════════════════════════════ */
const canvas = document.getElementById('laser-canvas');
const ctx = canvas.getContext('2d');

let width, height;

function resize() {
    // Handle HiDPI / Retina displays
    const dpr = window.devicePixelRatio || 1;
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener('resize', resize);
resize();

/* ═══════════════════════════════════════════════
   LASER DEMO (PLATZHALTER)
   → Ersetze diesen Block durch deinen originalen
     Laser-Animationscode aus der alten app.py
   ═══════════════════════════════════════════════ */
const lasers = [];
const LASER_COUNT = 6;

class Laser {
    constructor() {
        this.reset();
    }
    reset() {
        this.x = Math.random() * width;
        this.y = Math.random() * height;
        this.vx = (Math.random() - 0.5) * 4;
        this.vy = (Math.random() - 0.5) * 4;
        this.hue = 180 + Math.random() * 40; // cyan range
        this.width = 1 + Math.random() * 2;
        this.trail = [];
        this.maxTrail = 20 + Math.floor(Math.random() * 30);
    }
    update() {
        this.trail.push({x: this.x, y: this.y});
        if (this.trail.length > this.maxTrail) this.trail.shift();

        this.x += this.vx;
        this.y += this.vy;

        // Bounce off edges
        if (this.x < 0 || this.x > width) this.vx *= -1;
        if (this.y < 0 || this.y > height) this.vy *= -1;

        // Random direction change
        if (Math.random() < 0.02) {
            this.vx += (Math.random() - 0.5) * 2;
            this.vy += (Math.random() - 0.5) * 2;
            // Clamp speed
            const speed = Math.sqrt(this.vx*this.vx + this.vy*this.vy);
            if (speed > 6) {
                this.vx = (this.vx / speed) * 6;
                this.vy = (this.vy / speed) * 6;
            }
        }
    }
    draw() {
        if (this.trail.length < 2) return;
        ctx.beginPath();
        ctx.moveTo(this.trail[0].x, this.trail[0].y);
        for (let i = 1; i < this.trail.length; i++) {
            ctx.lineTo(this.trail[i].x, this.trail[i].y);
        }
        ctx.strokeStyle = `hsla(${this.hue}, 100%, 60%, 0.8)`;
        ctx.lineWidth = this.width;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        ctx.shadowColor = `hsla(${this.hue}, 100%, 50%, 1)`;
        ctx.shadowBlur = 15;
        ctx.stroke();
        ctx.shadowBlur = 0;

        // Head glow
        ctx.beginPath();
        ctx.arc(this.x, this.y, 3, 0, Math.PI * 2);
        ctx.fillStyle = `hsla(${this.hue}, 100%, 80%, 1)`;
        ctx.shadowColor = `hsla(${this.hue}, 100%, 50%, 1)`;
        ctx.shadowBlur = 20;
        ctx.fill();
        ctx.shadowBlur = 0;
    }
}

for (let i = 0; i < LASER_COUNT; i++) {
    lasers.push(new Laser());
}

function animate() {
    // Fade effect
    ctx.fillStyle = 'rgba(0, 0, 0, 0.15)';
    ctx.fillRect(0, 0, width, height);

    // Draw connecting lines between nearby lasers
    for (let i = 0; i < lasers.length; i++) {
        for (let j = i + 1; j < lasers.length; j++) {
            const dx = lasers[i].x - lasers[j].x;
            const dy = lasers[i].y - lasers[j].y;
            const dist = Math.sqrt(dx*dx + dy*dy);
            if (dist < 200) {
                ctx.beginPath();
                ctx.moveTo(lasers[i].x, lasers[i].y);
                ctx.lineTo(lasers[j].x, lasers[j].y);
                ctx.strokeStyle = `hsla(180, 100%, 50%, ${0.15 * (1 - dist/200)})`;
                ctx.lineWidth = 0.5;
                ctx.stroke();
            }
        }
    }

    lasers.forEach(l => { l.update(); l.draw(); });
    requestAnimationFrame(animate);
}
animate();
</script>
</body>
</html>
"""

# Render full-viewport HTML component
# height=0 is a fallback; CSS forces 100vw × 100vh
st.components.v1.html(LASER_HTML, height=800, scrolling=False)
