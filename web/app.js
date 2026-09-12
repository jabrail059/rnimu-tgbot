"use strict";
const tg = window.Telegram?.WebApp;
const initData = tg?.initData || "";
const byId = id => document.getElementById(id);
const visible = (id, value = true) => { byId(id).hidden = !value; };
const views = ["catalog", "materials", "article", "editor", "category-editor", "paywall"];
const state = { session: null, managing: false, view: null, category: null, material: null,
  categoryEditId: null, photoIndex: 0, busy: false, locked: false, dirty: false };
let photoRequest = null;
let photoVersion = 0;
let expirationTimer = null;
let privacyVersion = 0;

if (tg) { tg.ready(); tg.expand(); }

async function api(path, {method = "GET", data, file, signal, binary = false} = {}) {
  const headers = {Authorization: `tma ${initData}`};
  if (data !== undefined) headers["Content-Type"] = "application/json";
  if (file) headers["Content-Type"] = "application/octet-stream";
  const response = await fetch(path, {method, headers, body: file || (data === undefined ? undefined : JSON.stringify(data)),
    cache: "no-store", credentials: "omit", signal});
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const messages = {401: "Сессия истекла. Закройте приложение и откройте его заново из бота.",
      403: "Доступ закрыт. Проверьте подписку или права администратора.",
      413: "Фото слишком большое. Максимальный размер — 10 МБ.",
      422: "Проверьте название, текст и формат фотографии.", 429: "Слишком много запросов. Подождите минуту."};
    const error = new Error(messages[response.status] || (response.status < 500 && detail.detail) || "Не удалось загрузить данные. Попробуйте ещё раз.");
    error.status = response.status;
    throw error;
  }
  return binary ? response.blob() : response.json();
}

function clearPhoto() {
  photoVersion += 1;
  photoRequest?.abort(); photoRequest = null;
  const canvas = byId("photo");
  canvas.width = 1; canvas.height = 1;
  visible("photo", false); visible("photo-loading", false);
}

function clearReader() {
  clearPhoto();
  byId("article-title").textContent = "";
  byId("article-content").textContent = "";
  byId("reader-mark").textContent = "";
}

function setDirty(value) {
  state.dirty = value;
  if (tg?.isVersionAtLeast?.("6.2")) {
    value ? tg.enableClosingConfirmation() : tg.disableClosingConfirmation();
  }
}

function allowLeave() {
  if (state.dirty && !window.confirm("Изменения не сохранены. Выйти без сохранения?")) return false;
  setDirty(false); return true;
}

function view(id) {
  if (id !== "article") clearReader();
  views.forEach(name => visible(name, name === id));
  state.view = id;
  visible("loading", false);
  const canGoBack = !["catalog", "paywall"].includes(id);
  visible("back", canGoBack);
  if (tg?.BackButton) canGoBack ? tg.BackButton.show() : tg.BackButton.hide();
  window.scrollTo(0, 0);
}

function notice(message) { byId("notice").textContent = message; visible("notice"); }

function purgeContent() {
  clearReader();
  byId("category-list").replaceChildren(); byId("material-list").replaceChildren();
  byId("editor-image-list").replaceChildren();
  byId("category-title").textContent = "";
  byId("material-form").reset(); byId("category-form").reset();
  state.category = null; state.material = null;
  setDirty(false);
}

function showError(error) {
  if (error.name === "AbortError") return;
  if (error.status === 401 || error.status === 403) {
    purgeContent(); state.managing = false; visible("admin-toggle", false);
    view("paywall");
  }
  byId("error").textContent = error.message || "Ошибка соединения. Попробуйте ещё раз.";
  visible("error"); visible("loading", false);
}

async function run(task) {
  if (state.busy) return;
  state.busy = true;
  byId("app").setAttribute("aria-busy", "true");
  visible("error", false); visible("notice", false);
  try { await task(); } catch (error) { showError(error); }
  finally { state.busy = false; byId("app").removeAttribute("aria-busy"); }
}

function card(title, subtitle, action) {
  const button = document.createElement("button"); button.className = "card";
  const content = document.createElement("span");
  const heading = document.createElement("span"); heading.className = "card-title"; heading.textContent = title;
  const small = document.createElement("small"); small.textContent = subtitle;
  const arrow = document.createElement("span"); arrow.className = "card-arrow"; arrow.textContent = "→"; arrow.setAttribute("aria-hidden", "true");
  content.append(heading, small); button.append(content, arrow);
  button.onclick = () => run(action); return button;
}

function empty(list, message) {
  if (!list.childElementCount) { const p = document.createElement("p"); p.className = "empty"; p.textContent = message; list.append(p); }
}

async function refreshAccess() {
  const session = await api("/api/session", {method: "POST", data: {init_data: initData}});
  state.session = session;
  if (!session.is_admin) state.managing = false;
  visible("admin-toggle", session.is_admin);
  byId("price").textContent = `Доступ ко всем материалам — ${Number(session.price).toLocaleString("ru-RU")} ₽ на ${session.days} дней.`;
  byId("subscription").textContent = session.is_active
    ? `Подписка активна\nДействует до: ${new Date(session.subscription_end).toLocaleString("ru-RU", {timeZone: "Europe/Moscow", dateStyle: "long", timeStyle: "short"})} (МСК)`
    : session.is_admin ? "Доступ администратора" : "Подписка неактивна";
  visible("subscription");
  clearTimeout(expirationTimer);
  if (session.is_active && !session.is_admin) {
    const delay = Math.max(0, new Date(session.subscription_end).getTime() - Date.now());
    expirationTimer = setTimeout(checkExpiration, Math.min(delay + 100, 2147483647));
  }
  if (!session.is_active && !session.is_admin) { purgeContent(); view("paywall"); return false; }
  return true;
}

function checkExpiration() {
  // Hide immediately even if a slow request is still running. Recheck access
  // after that request finishes rather than losing the timer to the busy guard.
  lockContent();
  if (state.busy) expirationTimer = setTimeout(checkExpiration, 250);
  else resumeContent();
}

async function checkSession() { if (await refreshAccess()) await loadCategories(); }

async function loadCategories() {
  const data = await api("/api/categories");
  state.category = null; state.material = null;
  byId("admin-toggle").textContent = state.managing ? "К чтению" : "Управление";
  byId("catalog-title").textContent = state.managing ? "Управление курсом" : "Разделы";
  byId("catalog-hint").textContent = state.managing ? "Создавайте разделы и наполняйте их материалами." : "Выберите раздел, чтобы перейти к материалам.";
  visible("add-category", state.managing);
  const list = byId("category-list"); list.replaceChildren();
  data.categories.forEach(item => list.append(card(item.title, `Материалов: ${item.material_count}`, () => loadMaterials(item.id))));
  empty(list, state.managing ? "Создайте первый раздел — здесь начнётся ваш курс." : "Материалы скоро появятся. Автор уже готовит курс.");
  view("catalog");
}

async function loadMaterials(categoryId) {
  const data = await api(`/api/categories/${categoryId}/materials`);
  state.category = data.category; state.material = null;
  byId("category-title").textContent = data.category.title;
  visible("category-actions", state.managing);
  const list = byId("material-list"); list.replaceChildren();
  data.materials.forEach(item => list.append(card(item.title, `Фотографий: ${item.image_count}`, () => state.managing ? editMaterial(item.id) : openArticle(item.id))));
  empty(list, state.managing ? "Добавьте первый материал в этот раздел." : "В этом разделе пока нет материалов.");
  view("materials");
}

async function openArticle(id, photoIndex = 0) {
  const data = await api(`/api/materials/${id}`);
  state.material = {id: data.id, images: data.images};
  state.photoIndex = Math.max(0, Math.min(photoIndex, data.images.length - 1));
  view("article");
  if (state.locked) return;
  byId("article-title").textContent = data.title;
  byId("article-content").textContent = data.text || "К этому материалу пока не добавлен текст.";
  byId("reader-mark").textContent = `Личный доступ · ID ${state.session.user_id}`;
  visible("gallery", data.images.length > 0);
  if (data.images.length) await loadPhoto(state.photoIndex);
}

async function loadPhoto(index) {
  const photos = state.material?.images || [];
  if (index < 0 || index >= photos.length || state.locked) return;
  clearPhoto(); state.photoIndex = index;
  const version = photoVersion;
  photoRequest = new AbortController();
  byId("photo-counter").textContent = `${index + 1} / ${photos.length}`;
  byId("prev-photo").disabled = index === 0;
  byId("next-photo").disabled = index === photos.length - 1;
  visible("photo-loading");
  let url;
  try {
    const blob = await api(`/api/images/${photos[index].id}`, {binary: true, signal: photoRequest.signal});
    url = URL.createObjectURL(blob);
    const img = new Image(); img.src = url; await img.decode();
    if (version !== photoVersion || state.locked || state.view !== "article") return;
    const canvas = byId("photo"); canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
    canvas.getContext("2d").drawImage(img, 0, 0);
    canvas.setAttribute("aria-label", `Фото препарата ${index + 1} из ${photos.length}`);
    visible("photo");
  } catch (error) { if (version === photoVersion) showError(error); }
  finally { if (url) URL.revokeObjectURL(url); if (version === photoVersion) visible("photo-loading", false); }
}

function editCategory(rename = false) {
  state.categoryEditId = rename ? state.category.id : null;
  byId("category-editor-title").textContent = rename ? "Название раздела" : "Новый раздел";
  byId("category-name").value = rename ? state.category.title : "";
  view("category-editor"); byId("category-name").focus();
}

async function editMaterial(id = null) {
  const data = id ? await api(`/api/materials/${id}`) : null;
  state.material = data;
  byId("editor-heading").textContent = id ? "Редактирование материала" : "Новый материал";
  byId("material-title").value = data?.title || "";
  byId("material-text").value = data?.text || "";
  setDirty(false); renderEditorImages(); view("editor");
}

function renderEditorImages() {
  visible("editor-images", Boolean(state.material?.id)); visible("save-first", !state.material?.id);
  const list = byId("editor-image-list"); list.replaceChildren();
  (state.material?.images || []).forEach((item, index) => {
    const row = document.createElement("div"); row.className = "image-row";
    const label = document.createElement("span"); label.textContent = `Фото ${index + 1}`;
    const preview = document.createElement("button"); preview.className = "secondary"; preview.textContent = "Посмотреть";
    preview.onclick = () => run(async () => { if (allowLeave()) await openArticle(state.material.id, index); });
    const remove = document.createElement("button"); remove.className = "danger"; remove.textContent = "Удалить";
    remove.onclick = () => run(async () => {
      if (!window.confirm(`Удалить фото ${index + 1}?`)) return;
      await api(`/api/admin/images/${item.id}`, {method: "DELETE"});
      state.material.images = state.material.images.filter(image => image.id !== item.id); renderEditorImages();
    });
    row.append(label, preview, remove); list.append(row);
  });
  empty(list, "Пока нет фотографий. Можно выбрать сразу несколько файлов.");
}

async function goBack() {
  if (!allowLeave()) return;
  if (state.view === "article" && state.managing) return editMaterial(state.material.id);
  if (["article", "editor"].includes(state.view)) return loadMaterials(state.category.id);
  if (state.view === "category-editor" && state.categoryEditId) return loadMaterials(state.category.id);
  return loadCategories();
}

byId("back").onclick = () => run(goBack);
tg?.BackButton?.onClick(() => run(goBack));
byId("open-bot").onclick = () => tg?.close();
byId("refresh-session").onclick = () => run(checkSession);
byId("admin-toggle").onclick = () => run(async () => { if (allowLeave()) { state.managing = !state.managing; await loadCategories(); } });
byId("add-category").onclick = () => run(() => editCategory());
byId("rename-category").onclick = () => run(() => editCategory(true));
byId("cancel-category").onclick = () => run(goBack);
byId("add-material").onclick = () => run(() => editMaterial());
byId("cancel-material").onclick = () => run(goBack);
byId("preview-material").onclick = () => run(async () => { if (allowLeave()) await openArticle(state.material.id); });

byId("category-form").onsubmit = event => { event.preventDefault(); run(async () => {
  const title = byId("category-name").value.trim();
  if (!title) throw new Error("Введите название раздела.");
  const id = state.categoryEditId;
  const result = await api(id ? `/api/admin/categories/${id}` : "/api/admin/categories", {method: id ? "PATCH" : "POST", data: {title}});
  setDirty(false); await loadMaterials(result.id); notice("Раздел сохранён.");
}); };
byId("material-form").onsubmit = event => { event.preventDefault(); run(async () => {
  const title = byId("material-title").value.trim();
  if (!title) throw new Error("Введите название материала.");
  const id = state.material?.id;
  const result = await api(id ? `/api/admin/materials/${id}` : "/api/admin/materials", {method: id ? "PATCH" : "POST",
    data: {category_id: state.category.id, title, text: byId("material-text").value}});
  setDirty(false); await editMaterial(result.id); notice("Материал сохранён и доступен подписчикам.");
}); };
for (const id of ["category-name", "material-title", "material-text"]) byId(id).addEventListener("input", () => setDirty(true));

byId("delete-category").onclick = () => run(async () => {
  if (!window.confirm("Удалить раздел вместе со всеми материалами и фотографиями? Отменить это действие нельзя.")) return;
  await api(`/api/admin/categories/${state.category.id}`, {method: "DELETE"}); await loadCategories(); notice("Раздел удалён.");
});
byId("delete-material").onclick = () => run(async () => {
  if (!window.confirm("Удалить материал и все его фотографии? Отменить это действие нельзя.")) return;
  await api(`/api/admin/materials/${state.material.id}`, {method: "DELETE"}); setDirty(false);
  await loadMaterials(state.category.id); notice("Материал удалён.");
});
byId("image-files").onchange = event => {
  const files = Array.from(event.target.files || []); const materialId = state.material?.id;
  run(async () => {
    if (!materialId || !files.length) return;
    visible("upload-status");
    const failures = []; let uploaded = 0;
    try {
      for (const [index, file] of files.entries()) {
        byId("upload-status").textContent = `Загружаем ${index + 1} из ${files.length}…`;
        if (file.size > state.session.max_image_bytes) { failures.push(`${file.name}: больше 10 МБ`); continue; }
        try { await api(`/api/admin/materials/${materialId}/images`, {method: "POST", file}); uploaded += 1; }
        catch (error) { if ([401, 403, 404].includes(error.status)) throw error; failures.push(`${file.name}: ${error.message}`); }
      }
      const material = await api(`/api/materials/${materialId}`);
      state.material.images = material.images; renderEditorImages();
      notice(`Добавлено фотографий: ${uploaded} из ${files.length}.`);
      if (failures.length) throw new Error(failures.join("\n"));
    } finally { event.target.value = ""; visible("upload-status", false); }
  });
};
byId("prev-photo").onclick = () => loadPhoto(state.photoIndex - 1);
byId("next-photo").onclick = () => loadPhoto(state.photoIndex + 1);
let swipe = null;
byId("photo-stage").addEventListener("pointerdown", event => { if (event.isPrimary) swipe = {x: event.clientX, y: event.clientY}; });
byId("photo-stage").addEventListener("pointerup", event => {
  if (!swipe) return;
  const dx = event.clientX - swipe.x; const dy = event.clientY - swipe.y; swipe = null;
  if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.4) loadPhoto(state.photoIndex + (dx < 0 ? 1 : -1));
});
byId("photo-stage").addEventListener("pointercancel", () => { swipe = null; });
byId("photo-stage").addEventListener("keydown", event => {
  if (["ArrowLeft", "ArrowRight"].includes(event.key)) { event.preventDefault(); loadPhoto(state.photoIndex + (event.key === "ArrowLeft" ? -1 : 1)); }
});

function lockContent() {
  if (!state.session || state.view === "paywall") return;
  privacyVersion += 1;
  state.locked = true; document.body.classList.add("privacy-hidden"); visible("privacy-shield"); clearReader();
}
async function resumeContent() {
  if (!state.locked || document.hidden || tg?.isActive === false || state.busy) return;
  state.busy = true;
  const version = privacyVersion;
  try {
    const allowed = await refreshAccess();
    if (version !== privacyVersion || document.hidden || tg?.isActive === false) return;
    state.locked = false;
    if (allowed && state.view === "article" && state.material) await openArticle(state.material.id, state.photoIndex);
    if (state.locked || version !== privacyVersion || document.hidden || tg?.isActive === false) return;
    document.body.classList.remove("privacy-hidden"); visible("privacy-shield", false);
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      showError(error); state.locked = false; document.body.classList.remove("privacy-hidden"); visible("privacy-shield", false);
    } else {
      state.locked = true;
      clearReader();
      byId("privacy-shield").querySelector("p").textContent = "Не удалось проверить доступ. Нажмите «Продолжить», чтобы повторить.";
    }
  } finally { state.busy = false; }
}
window.addEventListener("blur", lockContent);
window.addEventListener("focus", resumeContent);
window.addEventListener("pagehide", lockContent);
window.addEventListener("pageshow", resumeContent);
document.addEventListener("visibilitychange", () => document.hidden ? lockContent() : resumeContent());
if (tg?.isVersionAtLeast?.("8.0")) { tg.onEvent("deactivated", lockContent); tg.onEvent("activated", resumeContent); }
byId("resume").onclick = resumeContent;
for (const eventName of ["copy", "cut", "contextmenu", "dragstart"]) {
  document.addEventListener(eventName, event => { if (event.target.closest?.(".protected")) event.preventDefault(); });
}
document.addEventListener("keydown", event => {
  if (state.view !== "article") return;
  if ((event.ctrlKey || event.metaKey) && ["s", "p", "c"].includes(event.key.toLowerCase())) event.preventDefault();
  if (event.key === "PrintScreen") { event.preventDefault(); lockContent(); }
});
window.addEventListener("beforeunload", event => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
run(async () => { if (!initData) throw new Error("Откройте это приложение из Telegram через кнопку в боте."); await checkSession(); });
