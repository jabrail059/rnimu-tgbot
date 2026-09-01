const tg = window.Telegram?.WebApp;
const initData = tg?.initData || "";
const byId = (id) => document.getElementById(id);
const show = (id) => byId(id).hidden = false;
const hide = (id) => byId(id).hidden = true;

if (tg) { tg.ready(); tg.expand(); }

async function api(path) {
  const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({init_data: initData})});
  if (!response.ok) throw new Error(response.status === 403 ? "Подписка неактивна" : "Не удалось загрузить данные");
  return response.json();
}

function showPaywall() { hide("loading"); hide("topics"); hide("article"); hide("back"); show("paywall"); }
function showError(message) { hide("loading"); byId("error").textContent = message; show("error"); }

async function loadTopics() {
  const data = await api("/api/topics");
  const list = byId("topic-list"); list.replaceChildren();
  data.topics.forEach(topic => {
    const button = document.createElement("button"); button.className = "topic";
    button.innerHTML = `${escapeHtml(topic.title)}<small>${escapeHtml(topic.description)}</small>`;
    button.onclick = () => loadContent(topic.id); list.append(button);
  });
  hide("loading"); hide("paywall"); hide("article"); hide("back"); show("topics");
}

async function loadContent(topicId) {
  try {
    const data = await api(`/api/content/${encodeURIComponent(topicId)}`);
    byId("article-title").textContent = data.title;
    byId("article-content").textContent = data.content;
    hide("topics"); show("article"); show("back");
  } catch (error) { error.message === "Подписка неактивна" ? showPaywall() : showError(error.message); }
}

function escapeHtml(value) { const temp = document.createElement("span"); temp.textContent = value; return temp.innerHTML; }
byId("back").onclick = loadTopics;
byId("open-bot").onclick = () => tg?.close();
document.addEventListener("contextmenu", event => event.preventDefault());
document.addEventListener("copy", event => event.preventDefault());
document.addEventListener("keydown", event => { if ((event.ctrlKey || event.metaKey) && ["s", "p", "c"].includes(event.key.toLowerCase())) event.preventDefault(); });

(async () => {
  if (!initData) return showError("Откройте это приложение из Telegram.");
  try {
    const session = await api("/api/session");
    if (!session.is_active) return showPaywall();
    byId("subscription").textContent = `Доступ активен до ${new Date(session.subscription_end).toLocaleDateString("ru-RU")}`;
    await loadTopics();
  } catch (error) { showError(error.message); }
})();
