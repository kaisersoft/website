const form = document.getElementById("chat-form");
const prompt = document.getElementById("prompt");
const send = document.getElementById("send");
const messagesEl = document.getElementById("messages");
const canvas = document.getElementById("stars");
const ctx = canvas.getContext("2d");

const history = [];
let stars = [];
let busy = false;

function resizeStars() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(innerWidth * dpr);
  canvas.height = Math.floor(innerHeight * dpr);
  canvas.style.width = `${innerWidth}px`;
  canvas.style.height = `${innerHeight}px`;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  stars = Array.from({ length: Math.min(260, Math.floor(innerWidth * innerHeight / 7000)) }, () => ({
    x: Math.random() * innerWidth,
    y: Math.random() * innerHeight,
    r: Math.random() * 1.25 + .2,
    a: Math.random() * .65 + .18,
    p: Math.random() * Math.PI * 2,
    s: Math.random() * .012 + .002
  }));
}

function animateStars() {
  ctx.clearRect(0, 0, innerWidth, innerHeight);
  for (const star of stars) {
    star.p += star.s;
    const alpha = star.a + Math.sin(star.p) * .16;
    ctx.beginPath();
    ctx.fillStyle = `rgba(225,232,255,${Math.max(.06, alpha)})`;
    ctx.arc(star.x, star.y, star.r, 0, Math.PI * 2);
    ctx.fill();
  }
  requestAnimationFrame(animateStars);
}

function addMessage(role, text) {
  const row = document.createElement("div");
  row.className = `message ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  row.appendChild(bubble);
  messagesEl.appendChild(row);
  document.querySelector(".chat").scrollTop = document.querySelector(".chat").scrollHeight;
  return bubble;
}

function autoSize() {
  prompt.style.height = "auto";
  prompt.style.height = `${Math.min(prompt.scrollHeight, 160)}px`;
}

async function submitMessage(event) {
  event.preventDefault();
  const text = prompt.value.trim();
  if (!text || busy) return;

  busy = true;
  send.disabled = true;
  addMessage("user", text);
  history.push({ role: "user", content: text });
  prompt.value = "";
  autoSize();
  const bubble = addMessage("assistant", "KaiserChat denkt nach …");
  bubble.classList.add("typing");

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "API error");
    bubble.classList.remove("typing");
    bubble.textContent = data.message;
    history.push({ role: "assistant", content: data.message });
  } catch (error) {
    bubble.classList.remove("typing");
    bubble.textContent = "KaiserChat ist gerade nicht erreichbar. Bitte versuche es noch einmal.";
    history.pop();
    console.error(error);
  } finally {
    busy = false;
    send.disabled = false;
    prompt.focus();
  }
}

prompt.addEventListener("input", autoSize);
prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});
form.addEventListener("submit", submitMessage);
window.addEventListener("resize", resizeStars);
resizeStars();
animateStars();
prompt.focus();
