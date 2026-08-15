"""
Kaisersoft.ai – Coming Soon
CI 2 Minimal (Black + Cyan) · Laser-Show Animation
Streamlit Webapp
"""

import streamlit as st

st.set_page_config(
    page_title="Kaisersoft.ai – Coming Soon",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Hide Streamlit chrome for immersive full-screen look
st.markdown(
    """
    <style>
    #MainMenu, footer, header, [data-testid="stToolbar"],
    [data-testid="stDecoration"], [data-testid="stStatusWidget"],
    .stDeployButton { display: none !important; visibility: hidden !important; }
    .block-container {
        padding: 0 !important;
        max-width: 100% !important;
    }
    html, body, [data-testid="stAppViewContainer"],
    [data-testid="stApp"] {
        background: #0a0a0a !important;
        overflow-x: hidden;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

LASER_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Kaisersoft.ai</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap');

  * { margin: 0; padding: 0; box-sizing: border-box; }

  html, body {
    width: 100%;
    height: 100%;
    background: #0a0a0a;
    overflow: hidden;
    font-family: 'Inter', system-ui, sans-serif;
  }

  #stage {
    position: relative;
    width: 100vw;
    height: 100vh;
    background: #0a0a0a;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
  }

  canvas#lasers {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    z-index: 1;
    pointer-events: none;
  }

  .grid {
    position: absolute;
    inset: 0;
    background-image:
      linear-gradient(rgba(0, 229, 255, 0.04) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0, 229, 255, 0.04) 1px, transparent 1px);
    background-size: 48px 48px;
    z-index: 0;
    mask-image: radial-gradient(ellipse at center, black 20%, transparent 75%);
    -webkit-mask-image: radial-gradient(ellipse at center, black 20%, transparent 75%);
  }

  .content {
    position: relative;
    z-index: 2;
    text-align: center;
    padding: 2rem;
  }

  /* Neural K logo – SVG */
  .logo-wrap {
    width: min(220px, 42vw);
    height: min(220px, 42vw);
    margin: 0 auto 1.75rem;
    filter: drop-shadow(0 0 18px rgba(0, 229, 255, 0.55))
            drop-shadow(0 0 40px rgba(0, 229, 255, 0.25));
    animation: logoPulse 3.2s ease-in-out infinite;
  }

  .logo-wrap svg {
    width: 100%;
    height: 100%;
  }

  @keyframes logoPulse {
    0%, 100% { filter: drop-shadow(0 0 14px rgba(0,229,255,0.45)) drop-shadow(0 0 28px rgba(0,229,255,0.2)); transform: scale(1); }
    50%      { filter: drop-shadow(0 0 28px rgba(0,229,255,0.85)) drop-shadow(0 0 55px rgba(0,229,255,0.35)); transform: scale(1.03); }
  }

  h1 {
    color: #ffffff;
    font-weight: 700;
    font-size: clamp(1.6rem, 5vw, 2.75rem);
    letter-spacing: -0.02em;
    margin-bottom: 0.35rem;
  }

  .tagline {
    color: #00e5ff;
    font-weight: 400;
    font-size: clamp(0.85rem, 2.2vw, 1.05rem);
    letter-spacing: 0.28em;
    text-transform: uppercase;
    opacity: 0.9;
    margin-bottom: 2.25rem;
  }

  /* Coming soon typewriter */
  .coming {
    display: inline-block;
    color: rgba(255,255,255,0.88);
    font-weight: 300;
    font-size: clamp(1.1rem, 3.2vw, 1.55rem);
    letter-spacing: 0.12em;
    min-height: 1.6em;
  }

  .coming .cursor {
    display: inline-block;
    width: 2px;
    height: 1.05em;
    background: #00e5ff;
    margin-left: 3px;
    vertical-align: text-bottom;
    animation: blink 0.85s step-end infinite;
    box-shadow: 0 0 8px #00e5ff;
  }

  @keyframes blink {
    50% { opacity: 0; }
  }

  .footer {
    position: absolute;
    bottom: 1.4rem;
    left: 0; right: 0;
    text-align: center;
    z-index: 2;
    color: rgba(255,255,255,0.28);
    font-size: 0.72rem;
    letter-spacing: 0.08em;
  }

  .scanline {
    position: absolute;
    left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg, transparent, rgba(0,229,255,0.35), transparent);
    z-index: 3;
    pointer-events: none;
    animation: scan 5.5s linear infinite;
    opacity: 0.5;
  }

  @keyframes scan {
    0%   { top: -2%; }
    100% { top: 102%; }
  }
</style>
</head>
<body>
<div id="stage">
  <div class="grid"></div>
  <canvas id="lasers"></canvas>
  <div class="scanline"></div>

  <div class="content">
    <div class="logo-wrap">
      <!-- Abstract neural K – CI 2 Minimal -->
      <svg viewBox="0 0 200 200" fill="none" xmlns="http://www.w3.org/2000/svg">
        <!-- Nodes forming K -->
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

        <!-- Edges -->
        <g stroke="#00E5FF" stroke-width="1.4" stroke-linecap="round" opacity="0.85">
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
    </div>

    <h1>Kaisersoft.ai</h1>
    <div class="tagline">Build · Automate · Scale</div>
    <div class="coming"><span id="typed"></span><span class="cursor"></span></div>
  </div>

  <div class="footer">© Kaisersoft.ai · Coming soon</div>
</div>

<script>
(function () {
  const canvas = document.getElementById('lasers');
  const ctx = canvas.getContext('2d');
  let W, H, beams = [], t = 0;

  function resize() {
    W = canvas.width = window.innerWidth;
    H = canvas.height = window.innerHeight;
  }
  window.addEventListener('resize', resize);
  resize();

  function rand(a, b) { return a + Math.random() * (b - a); }

  function spawnBeam() {
    const fromEdge = Math.floor(Math.random() * 4);
    let x0, y0, x1, y1;
    const cx = W * 0.5, cy = H * 0.42;
    if (fromEdge === 0) { x0 = rand(0, W); y0 = -20; }
    else if (fromEdge === 1) { x0 = W + 20; y0 = rand(0, H); }
    else if (fromEdge === 2) { x0 = rand(0, W); y0 = H + 20; }
    else { x0 = -20; y0 = rand(0, H); }

    // Aim near logo center with spread
    x1 = cx + rand(-W * 0.15, W * 0.15);
    y1 = cy + rand(-H * 0.12, H * 0.12);

    beams.push({
      x0, y0, x1, y1,
      life: 0,
      maxLife: rand(18, 42),
      width: rand(1.2, 3.2),
      hue: rand(175, 195), // cyan range
      alpha: rand(0.35, 0.85),
    });
  }

  function draw() {
    t++;
    ctx.fillStyle = 'rgba(10, 10, 10, 0.22)';
    ctx.fillRect(0, 0, W, H);

    if (Math.random() < 0.22) spawnBeam();
    if (Math.random() < 0.06) { spawnBeam(); spawnBeam(); }

    for (let i = beams.length - 1; i >= 0; i--) {
      const b = beams[i];
      b.life++;
      if (b.life > b.maxLife) {
        beams.splice(i, 1);
        continue;
      }
      const p = b.life / b.maxLife;
      const fade = p < 0.15 ? p / 0.15 : p > 0.7 ? (1 - p) / 0.3 : 1;
      const a = b.alpha * fade;

      // Core
      ctx.beginPath();
      ctx.moveTo(b.x0, b.y0);
      ctx.lineTo(b.x1, b.y1);
      ctx.strokeStyle = `hsla(${b.hue}, 100%, 65%, ${a})`;
      ctx.lineWidth = b.width;
      ctx.shadowBlur = 12;
      ctx.shadowColor = `hsla(${b.hue}, 100%, 60%, ${a})`;
      ctx.stroke();

      // Outer glow
      ctx.beginPath();
      ctx.moveTo(b.x0, b.y0);
      ctx.lineTo(b.x1, b.y1);
      ctx.strokeStyle = `hsla(${b.hue}, 100%, 70%, ${a * 0.25})`;
      ctx.lineWidth = b.width * 4;
      ctx.shadowBlur = 28;
      ctx.stroke();
    }

    // Occasional center burst
    if (t % 90 === 0) {
      for (let k = 0; k < 8; k++) {
        const ang = (k / 8) * Math.PI * 2 + rand(-0.2, 0.2);
        const len = rand(H * 0.25, H * 0.55);
        beams.push({
          x0: W * 0.5,
          y0: H * 0.42,
          x1: W * 0.5 + Math.cos(ang) * len,
          y1: H * 0.42 + Math.sin(ang) * len,
          life: 0,
          maxLife: rand(12, 28),
          width: rand(1.5, 2.8),
          hue: rand(180, 195),
          alpha: rand(0.5, 0.9),
        });
      }
    }

    requestAnimationFrame(draw);
  }
  draw();

  // Typewriter
  const phrases = [
    "Coming soon...",
    "Something powerful is loading...",
    "Build. Automate. Scale.",
    "Coming soon...",
  ];
  let pi = 0, ci = 0, deleting = false;
  const el = document.getElementById('typed');

  function typeLoop() {
    const phrase = phrases[pi];
    if (!deleting) {
      el.textContent = phrase.slice(0, ++ci);
      if (ci === phrase.length) {
        deleting = true;
        setTimeout(typeLoop, 1800);
        return;
      }
      setTimeout(typeLoop, 55 + Math.random() * 40);
    } else {
      el.textContent = phrase.slice(0, --ci);
      if (ci === 0) {
        deleting = false;
        pi = (pi + 1) % phrases.length;
        setTimeout(typeLoop, 400);
        return;
      }
      setTimeout(typeLoop, 28);
    }
  }
  setTimeout(typeLoop, 600);
})();
</script>
</body>
</html>
"""

st.components.v1.html(LASER_HTML, height=900, scrolling=False)
