const tg = window.Telegram?.WebApp;
const initData = tg?.initData || "";
const byId = (id) => document.getElementById(id);
const show = (id) => byId(id).hidden = false;
const hide = (id) => byId(id).hidden = true;

if (tg) { tg.ready(); tg.expand(); }

let isAdmin = false;
let currentCategory = null;
let currentImages = [];
let currentImageIndex = 0;
let currentObjectUrl = null;
let adminCategories = [];
let adminMaterials = [];

function errorMessage(response) {
  return response.status === 403 ? "Доступ запрещён" : "Не удалось выполнить запрос";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: "POST",
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    body: options.body ?? JSON.stringify({init_data: initData}),
    cache: "no-store",
  });
  if (!response.ok) throw new Error(errorMessage(response));
  return response;
}

async function jsonApi(path, options = {}) {
  const response = await api(path, options);
  return response.json();
}

function showPaywall() {
  hide("loading"); hide("topics"); hide("article"); hide("back"); hide("admin");
  show("paywall");
}

function showError(message) {
  hide("loading");
  byId("error").textContent = message;
  show("error");
}

function clearError() {
  hide("error");
}

async function loadTopics() {
  clearError();
  const data = await jsonApi("/api/topics");
  const list = byId("topic-list");
  list.replaceChildren();
  if (!data.topics.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "Разделов пока нет.";
    list.append(empty);
  }
  data.topics.forEach(topic => {
    const button = document.createElement("button");
    button.className = "topic";
    button.innerHTML = escapeHtml(topic.title);
    button.onclick = () => loadMaterials(Number(topic.id));
    list.append(button);
  });
  currentCategory = null;
  hide("loading"); hide("paywall"); hide("article"); hide("admin"); hide("back"); show("topics");
}

async function loadMaterials(categoryId) {
  try {
    const data = await jsonApi(`/api/materials/${categoryId}`);
    currentCategory = categoryId;
    const list = byId("topic-list");
    list.replaceChildren();
    const title = document.createElement("h2");
    title.textContent = data.category.title;
    list.append(title);
    if (!data.materials.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "Материалов в этом разделе пока нет.";
      list.append(empty);
    }
    data.materials.forEach(material => {
      const button = document.createElement("button");
      button.className = "topic";
      button.innerHTML = `${escapeHtml(material.title)}<small>${escapeHtml(material.text || "")}</small>`;
      button.onclick = () => loadContent(material.id);
      list.append(button);
    });
    hide("article"); hide("admin"); hide("paywall"); show("topics"); show("back");
    byId("back").onclick = loadTopics;
  } catch (error) {
    error.message === "Доступ запрещён" ? showPaywall() : showError(error.message);
  }
}

async function loadContent(materialId) {
  try {
    clearError();
    const data = await jsonApi(`/api/content/${encodeURIComponent(materialId)}`);
    byId("article-title").textContent = data.title;
    byId("article-content").textContent = data.content || "";
    currentImages = data.images || [];
    currentImageIndex = 0;
    hide("topics"); hide("admin"); show("article"); show("back");
    await renderCurrentImage();
  } catch (error) {
    error.message === "Доступ запрещён" ? showPaywall() : showError(error.message);
  }
}

async function renderCurrentImage() {
  const gallery = byId("gallery");
  const canvas = byId("gallery-canvas");
  cleanupObjectUrl();
  if (!currentImages.length) {
    hide("gallery");
    canvas.width = 1; canvas.height = 1;
    return;
  }
  show("gallery");
  const imageId = currentImages[currentImageIndex].id;
  try {
    const response = await api(`/api/image/${imageId}`);
    const blob = await response.blob();
    currentObjectUrl = URL.createObjectURL(blob);
    const image = new Image();
    image.onload = () => {
      const maxWidth = Math.min(window.innerWidth - 52, 700);
      const ratio = image.naturalHeight / image.naturalWidth || 1;
      canvas.width = Math.max(1, Math.round(Math.min(image.naturalWidth, maxWidth)));
      canvas.height = Math.max(1, Math.round(canvas.width * ratio));
      const ctx = canvas.getContext("2d", {alpha: false});
      ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(currentObjectUrl);
      currentObjectUrl = null;
    };
    image.onerror = () => showError("Не удалось показать изображение");
    image.src = currentObjectUrl;
    byId("gallery-counter").textContent = `${currentImageIndex + 1} / ${currentImages.length}`;
    byId("gallery-prev").disabled = currentImageIndex === 0;
    byId("gallery-next").disabled = currentImageIndex === currentImages.length - 1;
  } catch (error) {
    showError(error.message);
  }
}

function cleanupObjectUrl() {
  if (currentObjectUrl) {
    URL.revokeObjectURL(currentObjectUrl);
    currentObjectUrl = null;
  }
}

byId("gallery-prev").onclick = async () => {
  if (currentImageIndex > 0) { currentImageIndex--; await renderCurrentImage(); }
};
byId("gallery-next").onclick = async () => {
  if (currentImageIndex < currentImages.length - 1) { currentImageIndex++; await renderCurrentImage(); }
};
let touchStartX = 0;
byId("gallery-canvas").addEventListener("touchstart", e => {
  touchStartX = e.changedTouches[0].clientX;
}, {passive: true});
byId("gallery-canvas").addEventListener("touchend", async e => {
  const dx = e.changedTouches[0].clientX - touchStartX;
  if (Math.abs(dx) < 40) return;
  if (dx < 0 && currentImageIndex < currentImages.length - 1) currentImageIndex++;
  if (dx > 0 && currentImageIndex > 0) currentImageIndex--;
  await renderCurrentImage();
}, {passive: true});

function escapeHtml(value) {
  const temp = document.createElement("span");
  temp.textContent = value;
  return temp.innerHTML;
}

byId("back").onclick = loadTopics;
byId("open-bot").onclick = () => tg?.close();
byId("admin-open").onclick = openAdmin;

async function openAdmin() {
  if (!isAdmin) return;
  clearError();
  hide("topics"); hide("article"); hide("paywall"); hide("back"); show("admin");
  await refreshAdmin();
}

async function refreshAdmin() {
  try {
    const [categoriesData, materialsData] = await Promise.all([
      jsonApi("/api/admin/categories"),
      jsonApi("/api/admin/materials"),
    ]);
    adminCategories = categoriesData.categories;
    adminMaterials = materialsData.materials;
    renderAdminCategories();
    renderAdminCategorySelects();
    renderAdminMaterials();
  } catch (error) {
    showError(error.message);
  }
}

function renderAdminCategorySelects() {
  const selects = [byId("material-category"), byId("edit-category")];
  selects.forEach(select => {
    const selected = select.value;
    select.replaceChildren();
    adminCategories.forEach(category => {
      const option = document.createElement("option");
      option.value = category.id;
      option.textContent = category.title;
      select.append(option);
    });
    if (selected && adminCategories.some(c => String(c.id) === selected)) select.value = selected;
  });
  byId("material-form").querySelector("button[type=submit]").disabled = !adminCategories.length;
}

function renderAdminCategories() {
  const box = byId("admin-categories");
  box.replaceChildren();
  if (!adminCategories.length) {
    const p = document.createElement("p"); p.className = "muted"; p.textContent = "Разделов нет."; box.append(p); return;
  }
  adminCategories.forEach(category => {
    const row = document.createElement("div");
    row.className = "admin-row";
    const title = document.createElement("span"); title.textContent = category.title;
    const del = document.createElement("button"); del.className = "danger small"; del.textContent = "Удалить";
    del.onclick = async () => {
      if (!confirm(`Удалить раздел «${category.title}» и все материалы внутри него?`)) return;
      try {
        await jsonApi(`/api/admin/categories/${category.id}/delete`);
        await refreshAdmin();
      } catch (error) { showError(error.message); }
    };
    row.append(title, del); box.append(row);
  });
}

function renderAdminMaterials() {
  const box = byId("admin-materials");
  box.replaceChildren();
  if (!adminMaterials.length) {
    const p = document.createElement("p"); p.className = "muted"; p.textContent = "Материалов нет."; box.append(p); return;
  }
  adminMaterials.forEach(material => {
    const row = document.createElement("div");
    row.className = "admin-row material-row";
    const info = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = material.title;
    const cat = document.createElement("small"); cat.textContent = material.category_title;
    info.append(title, cat);
    const actions = document.createElement("div"); actions.className = "admin-actions";
    const edit = document.createElement("button"); edit.className = "small"; edit.textContent = "Изменить";
    edit.onclick = () => openEditor(material);
    const del = document.createElement("button"); del.className = "danger small"; del.textContent = "Удалить";
    del.onclick = async () => {
      if (!confirm(`Удалить «${material.title}»?`)) return;
      try {
        await jsonApi(`/api/admin/materials/${material.id}/delete`);
        hide("admin-editor");
        await refreshAdmin();
      } catch (error) { showError(error.message); }
    };
    actions.append(edit, del); row.append(info, actions); box.append(row);
  });
}

byId("category-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    const title = byId("category-title").value.trim();
    if (!title) return;
    await jsonApi("/api/admin/categories/create", {body: JSON.stringify({title})});
    byId("category-title").value = "";
    await refreshAdmin();
  } catch (error) { showError(error.message); }
};

byId("material-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    const category_id = Number(byId("material-category").value);
    const title = byId("material-title").value.trim();
    const text = byId("material-text").value;
    await jsonApi("/api/admin/materials/create", {
      body: JSON.stringify({category_id, title, text})
    });
    e.target.reset();
    await refreshAdmin();
  } catch (error) { showError(error.message); }
};

function openEditor(material) {
  byId("edit-id").value = material.id;
  byId("edit-category").value = material.category_id;
  byId("edit-title").value = material.title;
  byId("edit-text").value = material.text;
  byId("edit-images").value = "";
  show("admin-editor");
  loadEditorImages(material.id).catch(error => showError(error.message));
}

byId("edit-cancel").onclick = () => hide("admin-editor");
byId("edit-save").onclick = async () => {
  try {
    const material_id = Number(byId("edit-id").value);
    await jsonApi(`/api/admin/materials/${material_id}/update`, {
      body: JSON.stringify({
        category_id: Number(byId("edit-category").value),
        title: byId("edit-title").value.trim(),
        text: byId("edit-text").value
      })
    });
    await refreshAdmin();
    const fresh = adminMaterials.find(m => m.id === material_id);
    if (fresh) await loadEditorImages(material_id);
  } catch (error) { showError(error.message); }
};

byId("edit-images").onchange = async (e) => {
  const materialId = Number(byId("edit-id").value);
  if (!materialId || !e.target.files.length) return;
  try {
    const form = new FormData();
    form.append("init_data", initData);
    [...e.target.files].forEach(file => form.append("files", file));
    await fetchAdminMultipart(`/api/admin/materials/${materialId}/images`, form);
    e.target.value = "";
    await loadEditorImages(materialId);
  } catch (error) { showError(error.message); }
};

async function fetchAdminMultipart(path, form) {
  const response = await fetch(path, {method: "POST", body: form, cache: "no-store"});
  if (!response.ok) throw new Error(errorMessage(response));
  return response.json();
}

async function loadEditorImages(materialId) {
  const material = await jsonApi(`/api/admin/materials/${materialId}`);
  const box = byId("edit-gallery");
  box.replaceChildren();
  for (let i = 0; i < material.images.length; i++) {
    const imageId = material.images[i].id;
    const row = document.createElement("div");
    row.className = "admin-image-row";
    const label = document.createElement("span");
    label.textContent = `Фото ${i + 1}`;
    const del = document.createElement("button");
    del.className = "danger small";
    del.textContent = "Удалить";
    del.onclick = async () => {
      try {
        await jsonApi(`/api/admin/images/${imageId}/delete`);
        await loadEditorImages(materialId);
      } catch (error) { showError(error.message); }
    };
    row.append(label, del); box.append(row);
  }
}

byId("admin-refresh").onclick = refreshAdmin;

// Reduce casual exposure when the Mini App loses focus.
document.addEventListener("visibilitychange", () => {
  document.body.classList.toggle("app-blurred", document.hidden);
});
window.addEventListener("blur", () => document.body.classList.add("app-blurred"));
window.addEventListener("focus", () => document.body.classList.remove("app-blurred"));

document.addEventListener("contextmenu", event => event.preventDefault());
document.addEventListener("copy", event => event.preventDefault());
document.addEventListener("cut", event => event.preventDefault());
document.addEventListener("dragstart", event => event.preventDefault());
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && ["s", "p", "c", "x", "u"].includes(event.key.toLowerCase())) {
    event.preventDefault();
  }
});

(async () => {
  if (!initData) return showError("Откройте это приложение из Telegram.");
  try {
    const session = await jsonApi("/api/session");
    isAdmin = Boolean(session.is_admin);
    if (isAdmin) show("admin-open");
    if (!session.is_active) return showPaywall();
    byId("subscription").textContent =
      `Доступ активен до ${new Date(session.subscription_end).toLocaleDateString("ru-RU")}`;
    await loadTopics();
  } catch (error) {
    showError(error.message);
  }
})();
