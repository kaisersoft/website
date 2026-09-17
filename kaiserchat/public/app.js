const form = document.getElementById("chat-form");
const prompt = document.getElementById("prompt");
const send = document.getElementById("send");
const messagesEl = document.getElementById("messages");
const canvas = document.getElementById("stars");
const ctx = canvas.getContext("2d");
const chatList = document.getElementById("chat-list");
const toast = document.getElementById("toast");
const remainingEl = document.getElementById("remaining");
const budgetLabelEl = document.getElementById("budget-label");
const usedPercentEl = document.getElementById("used-percent");
const tachoBarEl = document.getElementById("tacho-bar");
const requestCountEl = document.getElementById("request-count");
const usedCostEl = document.getElementById("used-cost");
const avgCostEl = document.getElementById("avg-cost");
const tachoNoteEl = document.getElementById("tacho-note");

const history = [];
let stars = [];
let busy = false;
let currentTitle = "Neuer Chat";

function formatUsd(value, decimals = 2) {
  return `$${Number(value || 0).toFixed(decimals)}`;
}

function updateCreditMeter(data) {
  if (!data?.configured) {
    remainingEl.textContent = "—";
    budgetLabelEl.textContent = "/ — remaining";
    usedPercentEl.textContent = "—";
    tachoBarEl.style.width = "0%";
    requestCountEl.textContent = "—";
    usedCostEl.textContent = "—";
    avgCostEl.textContent = "—";
    tachoNoteEl.textContent = "";
    return;
  }

  const remaining = Number(data.remainingUsd) || 0;
  const budget = Number(data.budgetUsd) || 0;
  const used = Number(data.usedUsd) || 0;
  const percent = Number(data.usedPercent) || 0;

  remainingEl.textContent = formatUsd(remaining);
  budgetLabelEl.textContent = `/ ${formatUsd(budget)} remaining`;
  usedPercentEl.textContent = `${percent.toFixed(1)}% used`;
  tachoBarEl.style.width = `${Math.min(100, Math.max(0, percent))}%`;
  requestCountEl.textContent = String(Number(data.requests) || 0);
  usedCostEl.textContent = formatUsd(used);
  avgCostEl.textContent = formatUsd(Number(data.avgUsd) || 0, 4);
  tachoNoteEl.textContent = "";
}

async function refreshCreditMeter() {
  try {
    const response = await fetch(`/api/credits?ts=${Date.now()}`, { cache: "no-store" });
    const data = await response.json();
    updateCreditMeter(data);
  } catch (error) {
    console.error("Could not load credit meter", error);
    updateCreditMeter({ configured: false });
  }
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
    await refreshCreditMeter();
  } catch (error) {
    bubble.classList.remove("typing");
    bubble.textContent = "KaiserChat ist gerade nicht erreichbar. Bitte versuche es noch einmal.";
    history.pop();
    console.error(error);
    showToast("KaiserChat ist gerade nicht erreichbar.");
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

refreshCreditMeter();
setInterval(refreshCreditMeter, 60_000);
resizeStars();
animateStars();
addMessage("assistant", "Willkommen bei KaiserChat. Was möchtest du wissen?");
prompt.focus();
