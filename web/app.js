"use strict";
const tg = window.Telegram?.WebApp;
const initData = tg?.initData || "";
const byId = id => document.getElementById(id);
const visible = (id, value = true) => { byId(id).hidden = !value; };
const views = ["catalog", "materials", "article", "editor", "category-editor", "paywall"];
const state = { session: null, managing: false, view: null, category: null, material: null,
  categoryEditId: null, busy: false, locked: false, dirty: false };
let photoVersion = 0;
let photoObserver = null;
let photoPages = [];
let photoActive = 0;
let photoPriority = null;
let photoCacheBytes = 0;
const PHOTO_CACHE_LIMIT = 64 * 1024 * 1024;
let expirationTimer = null;
let privacyVersion = 0;
let documentVersion = 0;
let pdfObserver = null;
let pdfPages = [];
let pdfActive = 0;
let pdfPriority = null;
let readerScroll = 0;
let heartbeatTimer = null;
let pdfUploadRunning = false;
let pdfCacheBytes = 0;
const PDF_CACHE_LIMIT = 96 * 1024 * 1024;
const THEME_KEY = "rnimu-theme";
const SIZE_KEY = "rnimu-material-scale";
let backToTopIdleTimer = null;

function savedPreference(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function savePreference(key, value) {
  try { localStorage.setItem(key, value); } catch { /* Private browsing may disable storage. */ }
}

function setTheme(value) {
  const theme = ["light", "dark"].includes(value) ? value : "auto";
  const root = document.documentElement;
  if (theme === "auto") root.removeAttribute("data-theme");
  else root.dataset.theme = theme;
  const current = theme === "auto"
    ? (tg?.colorScheme || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"))
    : theme;
  root.style.colorScheme = current;
  const toggle = byId("theme-toggle");
  toggle.dataset.activeTheme = current;
  toggle.setAttribute("aria-checked", String(current === "dark"));
  const label = current === "dark" ? "Включить светлую тему" : "Включить тёмную тему";
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
  savePreference(THEME_KEY, theme);
}

function setMaterialScale(value) {
  const scale = Math.max(75, Math.min(200, Math.round(Number(value) / 5) * 5 || 100));
  byId("article").style.setProperty("--material-width", `${scale}%`);
  byId("material-scale").value = String(scale);
  byId("material-scale-value").textContent = `${scale}%`;
  byId("scale-down").disabled = scale <= 75;
  byId("scale-up").disabled = scale >= 200;
  byId("scale-reset").disabled = scale === 100;
  savePreference(SIZE_KEY, String(scale));
}

function setBackToTopVisible(show) {
  const button = byId("back-to-top");
  button.classList.toggle("is-visible", show);
  button.setAttribute("aria-hidden", String(!show));
  button.tabIndex = show ? 0 : -1;
  if (!show && document.activeElement === button) button.blur();
}

function updateBackToTop() {
  window.clearTimeout(backToTopIdleTimer);
  backToTopIdleTimer = null;
  const show = window.scrollY > 0 && !state.locked && !document.hidden;
  setBackToTopVisible(show);
  if (show) backToTopIdleTimer = window.setTimeout(() => {
    backToTopIdleTimer = null;
    setBackToTopVisible(false);
  }, 10_000);
}

if (tg) { tg.ready(); tg.expand(); }
setTheme(savedPreference(THEME_KEY));
setMaterialScale(savedPreference(SIZE_KEY) || 100);
tg?.onEvent?.("themeChanged", () => {
  if (savedPreference(THEME_KEY) === "auto") setTheme("auto");
});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
  if (savedPreference(THEME_KEY) === "auto" && !tg?.colorScheme) setTheme("auto");
});

async function api(path, {method = "GET", data, file, signal, binary = false, extraHeaders = {}, keepalive = false} = {}) {
  const headers = {...await window.courseAuth.headers(path, method), ...extraHeaders};
  if (data !== undefined) headers["Content-Type"] = "application/json";
  if (file) headers["Content-Type"] = "application/octet-stream";
  const response = await fetch(path, {method, headers, body: file || (data === undefined ? undefined : JSON.stringify(data)),
    cache: "no-store", credentials: "omit", signal, keepalive});
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const messages = {401: "Сессия истекла. Закройте приложение и откройте его заново из бота.",
      403: "Доступ закрыт. Проверьте подписку или права администратора.",
      413: detail.detail || "Файл превышает допустимый размер.",
      422: detail.detail && detail.detail !== "Invalid request" ? detail.detail : "Проверьте название, текст и формат файла.", 429: "Слишком много запросов. Подождите минуту."};
    const error = new Error((typeof detail.detail === "string" && detail.detail !== "Invalid request" && detail.detail) || messages[response.status] || "Не удалось загрузить данные. Попробуйте ещё раз.");
    error.status = response.status;
    error.readingLimited = response.headers.get("X-Reading-Limited") === "1";
    throw error;
  }
  return binary ? response.blob() : response.json();
}

function clearPhotos() {
  photoVersion += 1;
  photoObserver?.disconnect(); photoObserver = null;
  for (const page of photoPages) {
    page.request?.abort();
    page.canvas.width = 1; page.canvas.height = 1;
  }
  photoPages = []; photoCacheBytes = 0; photoPriority = null;
  byId("gallery").querySelectorAll(".photo-jump").forEach(control => control.remove());
  byId("photo-pages").replaceChildren(); visible("gallery", false);
}

function clearReader() {
  clearInterval(heartbeatTimer); heartbeatTimer = null;
  const view = state.material?.view_token;
  if (view && window.courseAuth.hasSession()) {
    state.material.view_token = null;
    api("/api/reader/close", {method: "POST", data: {view}, keepalive: true}).catch(() => {});
  }
  clearPhotos();
  clearDocuments();
  byId("article-nav").replaceChildren(); visible("reader-tools", false);
  byId("article-title").textContent = "";
  byId("article-content").textContent = "";
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
  document.body.classList.toggle("reading", id === "article");
  visible("fullscreen", Boolean(id === "article" && tg?.isVersionAtLeast?.("8.0") && tg?.requestFullscreen));
  visible("loading", false);
  const canGoBack = !["catalog", "paywall"].includes(id);
  visible("back", canGoBack);
  if (tg?.BackButton) canGoBack ? tg.BackButton.show() : tg.BackButton.hide();
  window.scrollTo(0, 0);
  updateBackToTop();
}

function notice(message) { byId("notice").textContent = message; visible("notice"); }

function purgeContent() {
  clearReader();
  byId("category-list").replaceChildren(); byId("material-list").replaceChildren();
  byId("editor-image-list").replaceChildren();
  byId("editor-document-list").replaceChildren();
  byId("security-events").replaceChildren(); visible("security-panel", false);
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
    state.locked = false; document.body.classList.remove("privacy-hidden"); visible("privacy-shield", false);
  } else if (error.status === 409 || error.readingLimited) {
    lockContent();
    byId("privacy-shield").querySelector("p").textContent = error.message;
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
  const arrow = document.createElement("span"); arrow.className = "card-arrow"; arrow.setAttribute("aria-hidden", "true");
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("viewBox", "0 0 24 24"); icon.setAttribute("fill", "none");
  icon.setAttribute("stroke", "currentColor"); icon.setAttribute("stroke-width", "1.8");
  icon.setAttribute("stroke-linecap", "round"); icon.setAttribute("stroke-linejoin", "round");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M4 12h16m-6-6 6 6-6 6"); icon.append(path); arrow.append(icon);
  content.append(heading, small); button.append(content, arrow);
  button.onclick = () => run(action); return button;
}

function empty(list, message) {
  if (!list.childElementCount) { const p = document.createElement("p"); p.className = "empty"; p.textContent = message; list.append(p); }
}

async function refreshAccess() {
  const session = window.courseAuth.hasSession() ? await api("/api/session") : await window.courseAuth.open(initData);
  state.session = session;
  byId("image-limit").textContent = `JPEG, PNG или WebP, до ${Math.round(session.max_image_bytes / 1024 / 1024)} МБ и 50 мегапикселей. Фото появятся в порядке загрузки.`;
  byId("pdf-limit").textContent = `До ${Math.round(session.max_pdf_bytes / 1024 / 1024)} МБ на файл. Выберите PDF без пароля — страницы будут доступны для чтения с прокруткой.`;
  if (!session.is_admin) state.managing = false;
  visible("admin-toggle", session.is_admin);
  byId("price").textContent = `Доступ ко всем материалам — ${Number(session.price).toLocaleString("ru-RU")} ₽ на ${session.days} дней.`;
  byId("subscription").textContent = session.is_active
    ? `Подписка активна\nДействует до: ${new Date(session.subscription_end).toLocaleString("ru-RU", {timeZone: "Europe/Moscow", dateStyle: "long", timeStyle: "short"})} (МСК)`
    : session.is_admin ? "Доступ администратора" : "Подписка неактивна";
  visible("subscription");
  clearTimeout(expirationTimer);
  const sessionDelay = (session.session_expires - session.server_time) * 1000;
  const subscriptionDelay = session.is_active && !session.is_admin ? new Date(session.subscription_end).getTime() - session.server_time * 1000 : Infinity;
  expirationTimer = setTimeout(checkExpiration, Math.max(0, Math.min(sessionDelay, subscriptionDelay, 2147483647)) + 100);
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
  visible("security-panel", state.managing);
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
  data.materials.forEach(item => list.append(card(item.title, `Фотографий: ${item.image_count} · PDF: ${item.document_count}`, () => state.managing ? editMaterial(item.id) : openArticle(item.id))));
  empty(list, state.managing ? "Добавьте первый материал в этот раздел." : "В этом разделе пока нет материалов.");
  view("materials");
}

async function openArticle(id) {
  const knownPhotoSizes = new Map(
    (state.material?.id === id ? state.material.images : [])
      .filter(photo => photo.width && photo.height)
      .map(photo => [photo.id, [photo.width, photo.height]])
  );
  const data = await api(`/api/materials/${id}`);
  data.images.forEach(photo => {
    const size = knownPhotoSizes.get(photo.id);
    if (size) [photo.width, photo.height] = size;
  });
  clearReader();
  state.material = {id: data.id, images: data.images, documents: data.documents, view_token: data.view_token};
  view("article");
  if (state.locked) return;
  heartbeatTimer = setInterval(heartbeat, 20_000);
  byId("article-title").textContent = data.title;
  byId("article-content").textContent = data.text || "";
  renderPhotos(data.images);
  renderDocuments(data.documents);
  renderArticleNav(data);
}

function discardCachedPhoto(page) {
  if (!page.blob) return;
  photoCacheBytes = Math.max(0, photoCacheBytes - page.blob.size); page.blob = null;
}

function cachePhoto(page, blob) {
  discardCachedPhoto(page);
  page.blob = blob; page.lastUsed = performance.now(); photoCacheBytes += blob.size;
  while (photoCacheBytes > PHOTO_CACHE_LIMIT) {
    const oldest = photoPages
      .filter(item => item !== page && item.blob && !item.nearby && !item.request)
      .sort((left, right) => left.lastUsed - right.lastUsed)[0];
    if (!oldest) break;
    discardCachedPhoto(oldest);
  }
}

function pageJump(kind, title, count, targetAt, prioritize) {
  const jump = document.createElement("form"); jump.className = `${kind}-jump page-jump`;
  const jumpLabel = document.createElement("label"); jumpLabel.textContent = kind === "photo" ? "Фото" : "Страница";
  const pageNumber = document.createElement("input");
  pageNumber.type = "number"; pageNumber.inputMode = "numeric";
  pageNumber.min = "1"; pageNumber.max = String(count);
  pageNumber.step = "1"; pageNumber.required = true; pageNumber.value = "1";
  pageNumber.setAttribute("aria-label", `${kind === "photo" ? "Номер фотографии" : "Номер страницы PDF"} «${title}»`);
  jumpLabel.append(pageNumber);
  const total = document.createElement("span"); total.textContent = `из ${count}`;
  const go = document.createElement("button"); go.type = "submit"; go.textContent = "Перейти";
  jump.append(jumpLabel, total, go);
  jump.onsubmit = event => {
    event.preventDefault();
    if (!pageNumber.checkValidity()) { pageNumber.reportValidity(); return; }
    const target = targetAt(pageNumber.valueAsNumber - 1);
    if (target) {
      pageNumber.blur();
      prioritize(target);
      target.scrollIntoView({block: "start", behavior: "auto"});
    }
  };
  return jump;
}

function renderArticleNav(material) {
  const nav = byId("article-nav"); nav.replaceChildren();
  const hasMedia = material.images.length || material.documents.length;
  visible("reader-tools", Boolean(hasMedia)); visible("reader-zoom", Boolean(hasMedia));
  if (!hasMedia) return;
  byId("reader-zoom").querySelector("label").textContent = material.images.length && material.documents.length
    ? "Размер фото и PDF" : material.images.length ? "Размер фото" : "Размер PDF";
  const heading = document.createElement("span");
  heading.className = "article-nav-label"; heading.textContent = "Быстрый переход";
  nav.append(heading);
  const addJump = (label, target, prioritize) => {
    const button = document.createElement("button");
    button.type = "button"; button.className = "section-link"; button.textContent = label;
    button.title = label;
    button.onclick = () => {
      button.blur(); prioritize(); target.scrollIntoView({block: "start", behavior: "auto"});
    };
    nav.append(button);
  };
  if (material.images.length) addJump(`Фотографии · ${material.images.length}`, byId("gallery"), () => {
    photoPriority = photoPages[0];
    pdfPages.forEach(page => page.request?.abort());
    photoPriority.nearby = true; pumpPhotos();
  });
  material.documents.forEach(item => {
    const section = Array.from(byId("documents").querySelectorAll(".pdf-document"))
      .find(element => element.dataset.documentId === String(item.id));
    if (!section) return;
    addJump(`PDF · ${item.title}`, section, () => {
      pdfPriority = pdfPages.find(page => page.documentId === item.id);
      photoPages.forEach(page => page.request?.abort());
      pdfPages.forEach(page => { if (page !== pdfPriority) page.request?.abort(); });
      if (pdfPriority) { pdfPriority.nearby = true; pumpPDF(); }
    });
  });
}

function renderPhotos(photos) {
  clearPhotos();
  if (!photos.length || state.locked) return;
  const gallery = byId("gallery"); visible("gallery");
  const container = byId("photo-pages");
  gallery.insertBefore(pageJump("photo", byId("article-title").textContent, photos.length,
    index => container.children[index], target => {
      photoPriority = target.photoPage;
      photoPages.forEach(page => { if (page !== photoPriority) page.request?.abort(); });
      photoPriority.nearby = true;
      pumpPhotos();
    }), container);
  photoObserver = new IntersectionObserver(entries => {
    for (const entry of entries) {
      const page = entry.target.photoPage;
      page.nearby = entry.isIntersecting;
      if (!page.nearby) {
        page.request?.abort(); page.loaded = false;
        page.canvas.width = 1; page.canvas.height = 1; page.canvas.hidden = true;
        page.placeholder.hidden = false;
      }
    }
    pumpPhotos();
  }, {rootMargin: "800px 0px"});
  const fragment = document.createDocumentFragment();
  photos.forEach((photo, index) => {
    const wrapper = document.createElement("div"); wrapper.className = "photo-page";
    const label = document.createElement("p"); label.className = "photo-page-label";
    label.textContent = `Фотография ${index + 1} из ${photos.length}`;
    const sheet = document.createElement("div"); sheet.className = "photo-sheet";
    sheet.style.aspectRatio = photo.width && photo.height ? `${photo.width} / ${photo.height}` : "4 / 3";
    const canvas = document.createElement("canvas"); canvas.width = 1; canvas.height = 1; canvas.hidden = true;
    canvas.setAttribute("role", "img"); canvas.setAttribute("aria-label", label.textContent);
    const placeholder = document.createElement("div"); placeholder.className = "photo-placeholder";
    const status = document.createElement("span"); status.textContent = "Загружаем фотографию…"; status.setAttribute("role", "status");
    const retry = document.createElement("button"); retry.textContent = "Повторить"; retry.hidden = true;
    const page = {imageId: photo.id, photo, canvas, sheet, placeholder, status, retry, nearby: false,
      loaded: false, request: null, failed: false, version: photoVersion, blob: null, lastUsed: 0};
    retry.onclick = () => { page.failed = false; retry.hidden = true; pumpPhotos(); };
    placeholder.append(status, retry); sheet.append(canvas, placeholder); wrapper.append(label, sheet); fragment.append(wrapper);
    wrapper.photoPage = page; photoPages.push(page); photoObserver.observe(wrapper);
  });
  container.append(fragment);
}

function pumpPhotos() {
  if (state.locked || state.view !== "article") return;
  while (photoActive < 2) {
    const available = item => item.nearby && !item.loaded && !item.request && !item.failed;
    const page = (photoPriority && available(photoPriority) ? photoPriority : null) || photoPages.find(available);
    if (!page) return;
    page.request = new AbortController(); photoActive += 1; loadPhotoPage(page);
  }
}

async function loadPhotoPage(page) {
  let url; const cached = Boolean(page.blob);
  page.status.textContent = cached ? "Восстанавливаем фотографию…" : "Загружаем фотографию…";
  try {
    const blob = page.blob || await pageBlob("image", page.imageId, 1, page.request.signal);
    if (!cached) cachePhoto(page, blob); else page.lastUsed = performance.now();
    url = URL.createObjectURL(blob);
    const img = new Image(); img.src = url; await img.decode();
    if (page.version !== photoVersion || state.locked || !page.nearby || state.view !== "article") return;
    page.photo.width = img.naturalWidth; page.photo.height = img.naturalHeight;
    page.sheet.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
    page.canvas.width = img.naturalWidth; page.canvas.height = img.naturalHeight;
    page.canvas.getContext("2d").drawImage(img, 0, 0);
    page.canvas.hidden = false; page.placeholder.hidden = true; page.loaded = true;
    if (photoPriority === page) photoPriority = null;
  } catch (error) {
    if (error.name !== "AbortError" && page.version === photoVersion) {
      discardCachedPhoto(page); page.failed = true; page.status.textContent = error.message; page.retry.hidden = false;
      if ([401, 403, 409].includes(error.status) || error.readingLimited) showError(error);
    }
  } finally {
    if (url) URL.revokeObjectURL(url);
    page.request = null; photoActive -= 1; pumpPhotos();
  }
}

async function heartbeat() {
  const view = state.material?.view_token;
  if (!view || state.locked || document.hidden) return;
  try { await api("/api/reader/heartbeat", {method: "POST", data: {view}}); }
  catch (error) {
    if (state.material?.view_token !== view) return;
    lockContent();
    byId("privacy-shield").querySelector("p").textContent = "Не удалось подтвердить доступ. Нажмите «Продолжить», чтобы повторить.";
    showError(error);
  }
}

async function pageBlob(kind, resourceId, page, signal) {
  const view = state.material?.view_token;
  if (!view || state.locked || signal.aborted) throw new DOMException("Просмотр завершён", "AbortError");
  const {ticket} = await api("/api/reader/ticket", {method: "POST", data: {view, kind, resource_id: resourceId, page}, signal});
  const path = kind === "image" ? `/api/images/${resourceId}` : `/api/documents/${resourceId}/pages/${page}`;
  return api(path, {binary: true, signal, extraHeaders: {"X-Read-View": view, "X-Page-Ticket": ticket}});
}

function clearDocuments() {
  documentVersion += 1;
  pdfObserver?.disconnect(); pdfObserver = null;
  for (const page of pdfPages) {
    page.request?.abort();
    page.canvas.width = 1; page.canvas.height = 1;
  }
  pdfPages = []; pdfPriority = null;
  pdfCacheBytes = 0;
  byId("documents").replaceChildren(); visible("documents", false);
}

function discardCachedPage(page) {
  if (!page.blob) return;
  pdfCacheBytes = Math.max(0, pdfCacheBytes - page.blob.size);
  page.blob = null;
}

function cachePDFPage(page, blob) {
  discardCachedPage(page);
  page.blob = blob; page.lastUsed = performance.now(); pdfCacheBytes += blob.size;
  while (pdfCacheBytes > PDF_CACHE_LIMIT) {
    const oldest = pdfPages
      .filter(item => item !== page && item.blob && !item.nearby && !item.request)
      .sort((left, right) => left.lastUsed - right.lastUsed)[0];
    if (!oldest) break;
    discardCachedPage(oldest);
  }
}

function renderDocuments(documents) {
  clearDocuments();
  if (!documents.length || state.locked) return;
  const container = byId("documents"); visible("documents");
  // Only nearby pages keep expensive decoded pixels. Compressed pages stay in
  // bounded, memory-only cache so scrolling back does not contact the server.
  pdfObserver = new IntersectionObserver(entries => {
    for (const entry of entries) {
      const page = entry.target.pdfPage;
      page.nearby = entry.isIntersecting;
      if (!page.nearby) {
        page.request?.abort(); page.loaded = false;
        page.canvas.width = 1; page.canvas.height = 1; page.canvas.hidden = true;
        page.placeholder.hidden = false;
      }
    }
    pumpPDF();
  }, {rootMargin: "300px 0px"});
  for (const document of documents) {
    const section = window.document.createElement("section"); section.className = "pdf-document";
    section.dataset.documentId = String(document.id);
    const heading = window.document.createElement("h3"); heading.textContent = document.title;
    const hint = window.document.createElement("p"); hint.className = "muted";
    hint.textContent = `${document.page_sizes.length} стр. · Листайте вниз для чтения`;
    const pages = window.document.createElement("div"); pages.className = "pdf-pages";
    const jump = pageJump("pdf", document.title, document.page_sizes.length,
      index => pages.children[index], target => {
        pdfPriority = target.pdfPage;
        pdfPages.forEach(page => { if (page !== pdfPriority) page.request?.abort(); });
        pdfPriority.nearby = true;
        pumpPDF();
      });
    section.append(heading, hint, jump, pages);
    document.page_sizes.forEach(([width, height], index) => {
      const wrapper = window.document.createElement("div"); wrapper.className = "pdf-page";
      const label = window.document.createElement("p"); label.className = "pdf-page-label";
      label.textContent = `Страница ${index + 1} из ${document.page_sizes.length}`;
      const sheet = window.document.createElement("div"); sheet.className = "pdf-sheet";
      sheet.style.aspectRatio = `${width} / ${height}`;
      const canvas = window.document.createElement("canvas"); canvas.width = 1; canvas.height = 1; canvas.hidden = true;
      canvas.setAttribute("role", "img"); canvas.setAttribute("aria-label", label.textContent);
      const placeholder = window.document.createElement("div"); placeholder.className = "pdf-placeholder";
      const status = window.document.createElement("span"); status.textContent = "Загружаем страницу…"; status.setAttribute("role", "status");
      const retry = window.document.createElement("button"); retry.textContent = "Повторить"; retry.hidden = true;
      const page = {documentId: document.id, number: index + 1, canvas, placeholder, status, retry,
        nearby: false, loaded: false, request: null, failed: false, version: documentVersion,
        blob: null, lastUsed: 0};
      retry.onclick = () => { page.failed = false; retry.hidden = true; pumpPDF(); };
      placeholder.append(status, retry); sheet.append(canvas, placeholder); wrapper.append(label, sheet); pages.append(wrapper);
      wrapper.pdfPage = page; pdfPages.push(page); pdfObserver.observe(wrapper);
    });
    container.append(section);
  }
}

function pumpPDF() {
  if (state.locked || state.view !== "article") return;
  while (pdfActive < 2) {
    const available = item => item.nearby && !item.loaded && !item.request && !item.failed;
    const page = (pdfPriority && available(pdfPriority) ? pdfPriority : null) || pdfPages.find(available);
    if (!page) return;
    page.request = new AbortController(); pdfActive += 1;
    loadPDFPage(page);
  }
}

async function loadPDFPage(page) {
  let url; const cached = Boolean(page.blob);
  page.status.textContent = cached ? "Восстанавливаем страницу…" : "Загружаем страницу…";
  try {
    const blob = page.blob || await pageBlob("document", page.documentId, page.number, page.request.signal);
    if (!cached) cachePDFPage(page, blob);
    else page.lastUsed = performance.now();
    url = URL.createObjectURL(blob);
    const img = new Image(); img.src = url; await img.decode();
    if (page.version !== documentVersion || state.locked || !page.nearby || state.view !== "article") return;
    page.canvas.width = img.naturalWidth; page.canvas.height = img.naturalHeight;
    page.canvas.getContext("2d").drawImage(img, 0, 0);
    page.canvas.hidden = false; page.placeholder.hidden = true; page.loaded = true;
    if (pdfPriority === page) pdfPriority = null;
  } catch (error) {
    if (error.name !== "AbortError" && page.version === documentVersion) {
      discardCachedPage(page);
      page.failed = true; page.status.textContent = error.message; page.retry.hidden = false;
      if ([401, 403, 409].includes(error.status) || error.readingLimited) showError(error);
    }
  } finally {
    if (url) URL.revokeObjectURL(url);
    page.request = null; pdfActive -= 1; pumpPDF();
  }
}

async function uploadPDF(materialId, file, onProgress) {
  const path = `/api/admin/materials/${materialId}/documents?title=${encodeURIComponent(file.name.slice(0, 300))}`;
  const headers = await window.courseAuth.headers(path, "POST");
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", path);
    for (const [name, value] of Object.entries(headers)) request.setRequestHeader(name, value);
    request.setRequestHeader("Content-Type", "application/pdf");
    request.upload.onprogress = event => { if (event.lengthComputable) onProgress(event.loaded / event.total); };
    request.onload = () => {
      let result;
      try { result = JSON.parse(request.responseText); } catch { result = {}; }
      if (request.status >= 200 && request.status < 300) resolve(result);
      else {
        const error = new Error(typeof result.detail === "string" ? result.detail : "Не удалось загрузить PDF. Попробуйте ещё раз.");
        error.status = request.status; reject(error);
      }
    };
    request.onerror = () => reject(new Error("Соединение прервалось. Повторите загрузку PDF."));
    request.onabort = () => reject(new Error("Загрузка PDF отменена."));
    request.send(file);
  });
}

function editCategory(rename = false) {
  state.categoryEditId = rename ? state.category.id : null;
  byId("category-editor-title").textContent = rename ? "Название раздела" : "Новый раздел";
  byId("category-name").value = rename ? state.category.title : "";
  view("category-editor"); byId("category-name").focus();
}

async function editMaterial(id = null) {
  const data = id ? await api(`/api/materials/${id}`) : null;
  const {categories} = await api("/api/categories");
  const select = byId("material-category"); select.replaceChildren();
  categories.forEach(category => {
    const option = document.createElement("option");
    option.value = category.id; option.textContent = category.title;
    select.append(option);
  });
  select.value = data?.category_id || state.category.id;
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
  const documents = byId("editor-document-list"); documents.replaceChildren();
  (state.material?.documents || []).forEach(item => {
    const row = document.createElement("div"); row.className = "document-row";
    const label = document.createElement("span");
    const pageCount = Array.isArray(item.page_sizes) ? item.page_sizes.length : item.page_count || "?";
    const size = Number.isFinite(item.size_bytes) ? ` · ${(item.size_bytes / 1024 / 1024).toFixed(1)} МБ` : "";
    label.textContent = `${item.title || "PDF-документ"} · ${pageCount} стр.${size}`;
    const remove = document.createElement("button"); remove.className = "danger"; remove.textContent = "Удалить";
    remove.onclick = () => run(async () => {
      if (!window.confirm(`Удалить PDF «${item.title}»?`)) return;
      await api(`/api/admin/documents/${item.id}`, {method: "DELETE"});
      state.material.documents = state.material.documents.filter(document => document.id !== item.id); renderEditorImages();
    });
    row.append(label, remove); documents.append(row);
  });
  empty(documents, "Здесь появятся загруженные PDF.");
}

async function goBack() {
  if (!allowLeave()) return;
  if (state.view === "article" && state.managing) return editMaterial(state.material.id);
  if (["article", "editor"].includes(state.view)) return loadMaterials(state.category.id);
  if (state.view === "category-editor" && state.categoryEditId) return loadMaterials(state.category.id);
  return loadCategories();
}

byId("back").onclick = () => run(goBack);
byId("theme-toggle").onclick = () =>
  setTheme(byId("theme-toggle").dataset.activeTheme === "dark" ? "light" : "dark");
byId("material-scale").oninput = event => setMaterialScale(event.target.value);
byId("scale-down").onclick = () => setMaterialScale(Number(byId("material-scale").value) - 10);
byId("scale-up").onclick = () => setMaterialScale(Number(byId("material-scale").value) + 10);
byId("scale-reset").onclick = () => setMaterialScale(100);
byId("back-to-top").onclick = () => window.scrollTo({top: 0,
  behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"});
window.addEventListener("scroll", updateBackToTop, {passive: true});
byId("fullscreen").onclick = () => {
  try { tg.isFullscreen ? tg.exitFullscreen() : tg.requestFullscreen(); }
  catch { notice("Полноэкранный режим недоступен в этой версии Telegram."); }
};
tg?.BackButton?.onClick(() => run(goBack));
byId("open-bot").onclick = () => tg?.close();
byId("refresh-session").onclick = () => run(checkSession);
byId("admin-toggle").onclick = () => run(async () => { if (allowLeave()) { state.managing = !state.managing; await loadCategories(); } });
byId("add-category").onclick = () => run(() => editCategory());
byId("security-refresh").onclick = () => run(async () => {
  const {events} = await api("/api/admin/security/events");
  const list = byId("security-events"); list.replaceChildren();
  const labels = {session_opened: "Открыт сеанс", launch_replay: "Повторный вход с чужим ключом",
    reading_limit_60: "Превышен минутный лимит", reading_limit_3600: "Превышен часовой лимит",
    reading_limit_86400: "Превышен суточный лимит", admin_revoke: "Администратор завершил сеанс",
    admin_unblock: "Администратор снял ограничение"};
  for (const event of events) {
    const row = document.createElement("p"); row.className = "status";
    row.textContent = `ID ${event.user_id} · ${labels[event.event] || event.event}\n${new Date(event.at * 1000).toLocaleString("ru-RU", {timeZone: "Europe/Moscow"})} МСК`;
    list.append(row);
  }
  empty(list, "Событий пока нет.");
});
for (const action of ["revoke", "unblock"]) byId(`security-${action}`).onclick = () => run(async () => {
  const id = byId("security-user").value.trim();
  if (!/^[1-9][0-9]{0,15}$/.test(id) || !Number.isSafeInteger(Number(id))) throw new Error("Введите числовой Telegram ID пользователя.");
  await api(`/api/admin/security/users/${id}/${action}`, {method: "POST"});
  notice(action === "revoke" ? "Сеанс пользователя завершён." : "Ограничение чтения снято.");
});
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
  const categoryId = Number(byId("material-category").value);
  const result = await api(id ? `/api/admin/materials/${id}` : "/api/admin/materials", {method: id ? "PATCH" : "POST",
    data: {category_id: categoryId, title, text: byId("material-text").value}});
  state.category = {id: categoryId, title: byId("material-category").selectedOptions[0].textContent};
  setDirty(false); await editMaterial(result.id); notice("Материал сохранён и доступен подписчикам.");
}); };
for (const id of ["category-name", "material-category", "material-title", "material-text"]) byId(id).addEventListener("input", () => setDirty(true));

byId("delete-category").onclick = () => run(async () => {
  if (!window.confirm("Удалить раздел вместе со всеми материалами, фотографиями и PDF? Отменить это действие нельзя.")) return;
  await api(`/api/admin/categories/${state.category.id}`, {method: "DELETE"}); await loadCategories(); notice("Раздел удалён.");
});
byId("delete-material").onclick = () => run(async () => {
  if (!window.confirm("Удалить материал, его фотографии и PDF? Отменить это действие нельзя.")) return;
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
        if (file.size > state.session.max_image_bytes) {
          failures.push(`${file.name}: больше ${Math.round(state.session.max_image_bytes / 1024 / 1024)} МБ`); continue;
        }
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
async function waitUntilIdle() {
  while (state.busy) await new Promise(resolve => setTimeout(resolve, 50));
}
async function uploadSelectedPDFs(files, input) {
  files = Array.from(files || []); const materialId = state.material?.id;
  if (!materialId || !files.length || pdfUploadRunning) return;
  pdfUploadRunning = true;
  try {
    await waitUntilIdle();
    await run(async () => {
      if (state.material?.id !== materialId || !files.length) throw new Error("Материал изменился. Выберите PDF ещё раз.");
      visible("pdf-upload-status"); visible("pdf-upload-progress");
      const failures = []; const uploadedDocuments = [];
      const wasDirty = state.dirty; setDirty(true);
      try {
        for (const [index, file] of files.entries()) {
        if (file.size > state.session.max_pdf_bytes) {
          failures.push(`${file.name}: больше ${Math.round(state.session.max_pdf_bytes / 1024 / 1024)} МБ`); continue;
        }
        const progress = value => {
          byId("pdf-upload-progress").value = value * 100;
          byId("pdf-upload-status").textContent = value < 1
            ? `Загружаем PDF ${index + 1} из ${files.length}: ${Math.round(value * 100)}%`
            : `PDF ${index + 1} из ${files.length}: проверяем страницы…`;
        };
        progress(0);
        try { uploadedDocuments.push(await uploadPDF(materialId, file, progress)); }
        catch (error) { if ([401, 403, 404].includes(error.status)) throw error; failures.push(`${file.name}: ${error.message}`); }
        }
        let refreshedDocuments = null;
        if (uploadedDocuments.length) {
          try {
            const material = await api(`/api/materials/${materialId}`);
            refreshedDocuments = material.documents;
          } catch (error) {
            if ([401, 403, 404].includes(error.status)) throw error;
          }
        }
        state.material.documents = refreshedDocuments || [...(state.material.documents || []), ...uploadedDocuments];
        renderEditorImages();
        notice(`Добавлено PDF: ${uploadedDocuments.length} из ${files.length}.`);
        if (failures.length) throw new Error(failures.join("\n"));
      } finally {
        setDirty(wasDirty); input.value = "";
        if (!failures.length) {
          setTimeout(() => { visible("pdf-upload-status", false); visible("pdf-upload-progress", false); }, 2500);
        }
      }
    });
  } finally { pdfUploadRunning = false; }
}
byId("pdf-files").onchange = event => uploadSelectedPDFs(event.target.files, event.target);
byId("pdf-upload").onclick = () => uploadSelectedPDFs(byId("pdf-files").files, byId("pdf-files"));

function lockContent() {
  if (!state.session || state.view === "paywall") return;
  if (!state.locked) readerScroll = window.scrollY;
  privacyVersion += 1;
  state.locked = true; document.body.classList.add("privacy-hidden"); visible("privacy-shield"); clearReader();
  updateBackToTop();
}
async function resumeContent() {
  if (!state.locked || document.hidden || tg?.isActive === false || state.busy) return;
  state.busy = true;
  const version = privacyVersion;
  try {
    const allowed = await refreshAccess();
    if (version !== privacyVersion || document.hidden || tg?.isActive === false) return;
    state.locked = false;
    if (allowed && state.view === "article" && state.material) {
      await openArticle(state.material.id);
      window.scrollTo(0, readerScroll);
    }
    if (state.locked || version !== privacyVersion || document.hidden || tg?.isActive === false) return;
    document.body.classList.remove("privacy-hidden"); visible("privacy-shield", false);
    updateBackToTop();
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
if (tg?.isVersionAtLeast?.("8.0")) {
  tg.onEvent("deactivated", lockContent); tg.onEvent("activated", resumeContent);
  tg.onEvent("fullscreenChanged", () => {
    const label = tg.isFullscreen ? "Свернуть экран" : "На весь экран";
    byId("fullscreen").textContent = label;
    byId("fullscreen").setAttribute("aria-label", label);
    byId("fullscreen").title = label;
  });
  tg.onEvent("fullscreenFailed", () => notice("Полноэкранный режим недоступен на этом устройстве."));
}
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
// Keep an active editor/upload session alive without continuing a hidden reader.
setInterval(async () => {
  if (!window.courseAuth.hasSession() || !state.session || state.locked || document.hidden || state.view === "article") return;
  try { await api("/api/session"); } catch (error) { showError(error); }
}, 60_000);
run(async () => { if (!initData) throw new Error("Откройте это приложение из Telegram через кнопку в боте."); await checkSession(); });
