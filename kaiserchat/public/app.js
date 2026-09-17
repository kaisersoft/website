const form = document.getElementById("chat-form");
const prompt = document.getElementById("prompt");
const send = document.getElementById("send");
const messagesEl = document.getElementById("messages");
const canvas = document.getElementById("stars");
const ctx = canvas.getContext("2d");
const chatList = document.getElementById("chat-list");
const toast = document.getElementById("toast");

const history = [];
const meterKey = "kaiserchat-api-meter-v1";
const budget = 5.00;
let meter = loadMeter();
let stars = [];
let busy = false;
let currentTitle = "Neuer Chat";

function loadMeter() {
  try {
    const saved = JSON.parse(localStorage.getItem(meterKey) || "null");
    if (saved && typeof saved === "object") {
      return { cost: Number(saved.cost) || 0, requests: Number(saved.requests) || 0, startedAt: saved.startedAt || new Date().toISOString() };
    }
  } catch (error) { console.warn("Could not load API meter", error); }
  return { cost: 0, requests: 0, startedAt: new Date().toISOString() };
}

function saveMeter() {
  try { localStorage.setItem(meterKey, JSON.stringify(meter)); } catch (error) { console.warn(error); }
}

function updateMeter() {
  const used = Math.max(0, meter.cost);
  const remaining = Math.max(0, budget - used);
  const percent = Math.min(100, (used / budget) * 100);
  const avg = meter.requests ? used / meter.requests : 0;
  document.getElementById("remaining").textContent = `$${remaining.toFixed(2)}`;
  document.getElementById("used-percent").textContent = `${percent.toFixed(1)}% used`;
  document.getElementById("tacho-bar").style.width = `${percent}%`;
  document.getElementById("request-count").textContent = String(meter.requests);
  document.getElementById("used-cost").textContent = `$${used.toFixed(2)}`;
  document.getElementById("avg-cost").textContent = `$${avg.toFixed(4)}`;
}

function addRequestCost(cost) {
  meter.requests += 1;
  meter.cost += Math.max(0, Number(cost) || 0);
  saveMeter();
  updateMeter();
}

function resizeStars() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(innerWidth * dpr);
  canvas.height = Math.floor(innerHeight * dpr);
  canvas.style.width = `${innerWidth}px`;
  canvas.style.height = `${innerHeight}px`;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  stars = Array.from({ length: Math.min(320, Math.floor(innerWidth * innerHeight / 6200)) }, () => ({
    x: Math.random() * innerWidth, y: Math.random() * innerHeight, r: Math.random() * 1.25 + .2,
    a: Math.random() * .65 + .18, p: Math.random() * Math.PI * 2, s: Math.random() * .012 + .002
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
  const chat = document.querySelector(".chat");
  chat.scrollTop = chat.scrollHeight;
  return bubble;
}

function updateChatList() {
  chatList.innerHTML = "";
  if (!currentTitle || currentTitle === "Neuer Chat") return;
  const item = document.createElement("button");
  item.type = "button";
  item.className = "chat-item active";
  item.innerHTML = `<span class="chat-title"></span><span class="chat-time">Gerade eben</span>`;
  item.querySelector(".chat-title").textContent = currentTitle;
  chatList.appendChild(item);
}

function autoSize() {
  prompt.style.height = "auto";
  prompt.style.height = `${Math.min(prompt.scrollHeight, 150)}px`;
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 2500);
}

function startNewChat() {
  history.length = 0;
  messagesEl.innerHTML = "";
  currentTitle = "Neuer Chat";
  updateChatList();
  addMessage("assistant", "Willkommen bei KaiserChat. Was möchtest du wissen?");
  prompt.focus();
}

async function submitMessage(event) {
  event.preventDefault();
  const text = prompt.value.trim();
  if (!text || busy) return;

  busy = true;
  send.disabled = true;
  if (history.length === 0) { currentTitle = text; updateChatList(); }
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
    addRequestCost(data.estimatedCostUsd);
  } catch (error) {
    bubble.classList.remove("typing");
    bubble.textContent = "KaiserChat ist gerade nicht erreichbar. Bitte versuche es noch einmal.";
    history.pop();
    console.error(error);
    showToast(error.message || "API-Fehler");
  } finally {
    busy = false;
    send.disabled = false;
    prompt.focus();
  }
}

document.getElementById("new-chat").addEventListener("click", startNewChat);
for (const button of document.querySelectorAll(".sidebar-nav button")) {
  button.addEventListener("click", () => showToast(button.dataset.info));
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

updateMeter();
resizeStars();
animateStars();
addMessage("assistant", "Willkommen bei KaiserChat. Was möchtest du wissen?");
prompt.focus();
