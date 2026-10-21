"use strict";

(() => {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  let ticket = fragment.get("ticket");
  if (window.location.hash) history.replaceState(null, "", window.location.pathname + window.location.search);
  const $ = (id) => document.getElementById(id);
  const ns = "http://www.w3.org/2000/svg";
  const format = new Intl.NumberFormat("ru-RU");
  const dateFormat = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  const dayFormat = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" });
  const labels = {
    youtube: "YouTube", tiktok: "TikTok", instagram: "Instagram", twitter: "Twitter / X", x: "Twitter / X",
    rule34: "Rule34", rule34video: "Rule34Video", vk: "ВКонтакте", reddit: "Reddit", pinterest: "Pinterest",
    success: "Успешно", done: "Успешно", completed: "Завершено", failed: "Ошибка", error: "Ошибка",
    cancelled: "Отменено", canceled: "Отменено", started: "Начато", pending: "В ожидании", processing: "В работе",
    video: "Видео", audio: "Аудио", photo: "Фото", album: "Альбом", document: "Файл", live_photo: "Live Photo",
    download: "Загрузка", conversion: "Загрузка", start: "Старт", cancel: "Отмена", restart: "Перезапуск",
    bot_start: "Запуск бота", bot_restart: "Перезапуск бота", service_start: "Запуск сервиса", service_stop: "Остановка сервиса",
    description: "Описание", file: "Получить файл", quality: "Выбор качества", timing: "Выбор интервала",
    command: "Команда", callback: "Нажатие кнопки", message: "Сообщение", shutdown: "Остановка", upload: "Отправка",
    unknown: "Не указано", delivery_unknown: "Доставка не подтверждена",
  };
  const callbackLabels = {
    desc: "Описание", ttfile: "Исходный файл TikTok", qh: "Выбор качества", ytv: "Видео YouTube", yta: "Аудио YouTube",
    audio: "Аудио", cancel: "Отмена", help: "Помощь",
  };
  const systemLabels = {
    startup: "Запуск сервиса", shutdown: "Остановка сервиса", stats_access_issued: "Выдан доступ к статистике",
    stats_login: "Вход в статистику", stats_access_closed: "Доступ к статистике закрыт",
  };
  let state = { period: "30d", page: 1, pageSize: 25, tab: "events", data: null, controller: null, applied: {}, expiry: null, expiryTimer: null };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function translated(value) { return value == null || value === "" ? "—" : labels[value] || String(value); }
  function actionLabel(value) {
    if (!value) return "—";
    const text = String(value), separator = text.indexOf(":");
    if (separator === -1) return translated(text);
    const prefix = text.slice(0, separator), detail = text.slice(separator + 1);
    if (prefix === "command") return `Команда /${detail}`;
    if (prefix === "callback") return `Кнопка: ${callbackLabels[detail] || translated(detail)}`;
    if (prefix === "message") return { text: "Текстовое сообщение", media: "Сообщение с медиа" }[detail] || `Сообщение: ${translated(detail)}`;
    if (prefix === "system") return systemLabels[detail] || `Сервис: ${translated(detail)}`;
    return translated(text);
  }
  function num(value) { const n = Number(value); return Number.isFinite(n) ? n : 0; }
  function count(value) { return format.format(num(value)); }
  function date(value) { const d = new Date(value); return value && !Number.isNaN(d.valueOf()) ? dateFormat.format(d) : "—"; }
  function bytes(value) {
    const n = num(value);
    if (!n) return "—";
    const scale = Math.min(Math.floor(Math.log(Math.max(1, n)) / Math.log(1024)), 4);
    return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(n / (1024 ** scale))} ${["Б", "КБ", "МБ", "ГБ", "ТБ"][scale]}`;
  }
  function notice(message, info = false) {
    $("notice").textContent = message || "";
    $("notice").classList.toggle("info", info);
    $("notice").hidden = !message;
  }
  function gate(message) {
    $("dashboard").hidden = true;
    $("access-gate").hidden = false;
    if (message) $("gate-message").textContent = message;
  }
  function endSession(message) {
    if (state.controller) state.controller.abort();
    if (state.expiryTimer) window.clearTimeout(state.expiryTimer);
    state.expiryTimer = null;
    state.expiry = null;
    state.data = null;
    $("kpis").replaceChildren();
    $("activity-chart").replaceChildren();
    $("platforms").replaceChildren();
    $("table-body").replaceChildren();
    gate(message);
  }
  function checkExpiry() {
    if (state.expiry == null) return false;
    if (Date.now() < state.expiry) return false;
    endSession("Срок доступа истёк. Для новой сессии отправьте /dashboard боту.");
    return true;
  }
  function scheduleExpiry(value) {
    if (state.expiryTimer) window.clearTimeout(state.expiryTimer);
    const timestamp = new Date(value).valueOf();
    state.expiry = Number.isFinite(timestamp) ? timestamp : null;
    if (state.expiry == null) return false;
    if (checkExpiry()) return true;
    state.expiryTimer = window.setTimeout(checkExpiry, Math.max(0, state.expiry - Date.now()) + 10);
    return false;
  }
  async function api(path, options = {}) {
    const response = await fetch(`/stats/api/${path}`, { credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(15000), ...options });
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) {
      const error = new Error(data.error || `Запрос не выполнен (${response.status}). Попробуйте ещё раз.`);
      error.status = response.status;
      throw error;
    }
    return data;
  }
  async function authenticate(loginTicket, password) {
    $("retry-auth").disabled = true;
    gate("Проверяем доступ…");
    try {
      if (loginTicket) {
        await api("login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ticket: loginTicket, password }) });
        ticket = null;
        $("access-password").value = "";
        $("login-form").hidden = true;
      }
      const session = await api("session");
      if (scheduleExpiry(session.expires_at)) return;
      $("session-expiry").textContent = `Доступ до ${date(session.expires_at)}`;
      $("access-mode").textContent = session.public_available ? "Защищённый удалённый доступ" : "Доступ в локальной сети";
      $("dashboard").hidden = false;
      $("access-gate").hidden = true;
      state.applied = readFilters();
      await load();
    } catch (error) {
      gate(error.status === 401 || error.status === 403
        ? (loginTicket ? error.message : "Нет действующей сессии. Отправьте /dashboard боту и откройте новую ссылку с паролем. Срок доступа — 10 минут с момента запроса.")
        : error.message);
    } finally { $("retry-auth").disabled = false; }
  }
  function readFilters() {
    const data = new FormData($("filters"));
    const result = { period: state.period, exclude_owner: $("exclude-owner").checked ? "true" : "false" };
    for (const name of ["platform", "status", "media_type", "action", "outcome"]) {
      if (data.get(name)) result[name] = String(data.get(name));
    }
    const q = $("filter-user").value.trim();
    if (q) result.q = q;
    if (state.period === "custom") {
      const from = $("date-from").value;
      const to = $("date-to").value;
      if (!from || !to) throw new Error("Укажите начало и конец периода.");
      if (from > to) throw new Error("Начало периода должно быть раньше его окончания.");
      result.start = new Date(`${from}T00:00:00`).toISOString();
      const end = new Date(`${to}T00:00:00`);
      end.setDate(end.getDate() + 1);
      result.end = end.toISOString();
    }
    return result;
  }
  async function apply() {
    try { state.applied = readFilters(); } catch (error) { notice(error.message); return; }
    state.page = 1;
    await load();
  }
  async function load() {
    if (checkExpiry()) return;
    if (state.controller) state.controller.abort();
    const controller = new AbortController();
    state.controller = controller;
    $("dashboard").classList.add("is-loading");
    $("table-region").setAttribute("aria-busy", "true");
    $("refresh").disabled = true;
    notice("");
    try {
      const params = new URLSearchParams({ ...state.applied, page: String(state.page), page_size: String(state.pageSize) });
      const data = await api(`data?${params}`, { signal: controller.signal });
      if (controller.signal.aborted || checkExpiry()) return;
      state.data = data;
      render(data);
      $("last-updated").textContent = `Обновлено ${new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date())}`;
    } catch (error) {
      if (error.name === "AbortError") return;
      if (error.status === 401 || error.status === 403) endSession("Сессия завершена. Получите новую ссылку командой /dashboard в боте.");
      else notice(`${error.message}${state.data ? " На экране остаются данные предыдущего запроса." : ""}`);
    } finally {
      if (state.controller === controller) {
        $("dashboard").classList.remove("is-loading");
        $("table-region").setAttribute("aria-busy", "false");
        $("refresh").disabled = false;
      }
    }
  }
  function render(data) {
    renderKpis(data.summary || {});
    renderChart(data.series || []);
    renderPlatforms(data.platforms || []);
    updateOptions(data.options || {});
    const since = data.history && data.history.events_since;
    $("history-note").textContent = since
      ? `Журнал хранится до 90 дней. Доступные события — с ${date(since)}. Старые действия до начала сбора журнала могут отсутствовать.`
      : "Журнал хранится до 90 дней. Действия до начала сбора журнала могут отсутствовать.";
    renderTable();
  }
  function renderKpis(s) {
    const attempts = num(s.done) + num(s.failed) + num(s.cancelled) + num(s.delivery_unknown);
    const success = attempts ? `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(num(s.done) / attempts * 100)}%` : "—";
    const cards = [
      ["Зарегистрировано", count(s.registered), `К концу периода · активных: ${count(s.active)}`, "◉"],
      ["Сделали больше /start", count(s.nonstart), "За период: любое действие, включая /help", "↗"],
      ["Пользователи с загрузками", count(s.converters), "Хотя бы одна отправленная ссылка", "♧"],
      ["Загрузки", count(s.conversions), `Получено данных: ${bytes(s.bytes)}`, "↓"],
      ["Успешное завершение", success, `Ошибок: ${count(s.failed)} · отмен: ${count(s.cancelled)} · без подтверждения: ${count(s.delivery_unknown)}`, "✓"],
      ["События", count(s.events), "Действия и события сервиса за период", "≋"],
    ];
    const fragment = document.createDocumentFragment();
    for (const [title, value, hint, icon] of cards) {
      const card = el("article", "kpi-card");
      const titleNode = el("p", "kpi-label", title);
      titleNode.append(el("span", "kpi-icon", icon));
      card.append(titleNode, el("div", "kpi-number", value), el("p", "kpi-hint", hint));
      fragment.append(card);
    }
    $("kpis").replaceChildren(fragment);
  }
  function svgEl(name, attributes = {}, text) {
    const node = document.createElementNS(ns, name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function renderChart(series) {
    const container = $("activity-chart");
    if (!series.length) { container.replaceChildren(el("span", "", "Нет событий за выбранный период")); $("chart-caption").textContent = ""; return; }
    const width = 660, height = 190, left = 38, right = 10, top = 12, bottom = 28;
    const max = Math.max(1, ...series.map((p) => num(p.events)));
    const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `Активность по дням: ${count(series.reduce((sum, p) => sum + num(p.events), 0))} событий` });
    for (let i = 0; i <= 3; i++) {
      const y = top + (height - top - bottom) * i / 3;
      svg.append(svgEl("line", { x1: left, x2: width - right, y1: y, y2: y, class: "grid-line" }));
      svg.append(svgEl("text", { x: left - 9, y: y + 3, "text-anchor": "end" }, count(Math.round(max * (1 - i / 3)))));
    }
    const points = series.map((p, i) => ({ x: series.length === 1 ? (left + width - right) / 2 : left + i / (series.length - 1) * (width - left - right), y: height - bottom - num(p.events) / max * (height - top - bottom), source: p }));
    const path = points.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join(" ");
    svg.append(svgEl("path", { d: `${path} L${points[points.length - 1].x},${height - bottom} L${points[0].x},${height - bottom} Z`, class: "area" }));
    svg.append(svgEl("path", { d: path, class: "trend" }));
    for (const p of points) {
      const dot = svgEl("circle", { cx: p.x, cy: p.y, r: series.length === 1 ? 4 : 2.5, fill: "#70c9b3" });
      dot.append(svgEl("title", {}, `${p.source.day}: ${count(p.source.events)} событий, ${count(p.source.conversions)} загрузок`));
      svg.append(dot);
    }
    const indices = [...new Set([0, Math.floor((series.length - 1) / 2), series.length - 1])];
    for (const index of indices) {
      const parsed = new Date(series[index].day);
      svg.append(svgEl("text", { x: points[index].x, y: height - 5, "text-anchor": index === 0 ? "start" : index === series.length - 1 ? "end" : "middle" }, Number.isNaN(parsed.valueOf()) ? series[index].day : dayFormat.format(parsed)));
    }
    container.replaceChildren(svg);
    $("chart-caption").textContent = "Даты графика — UTC. Наведите на точку, чтобы увидеть значения.";
  }
  function renderPlatforms(platforms) {
    const root = $("platforms");
    if (!platforms.length) { root.replaceChildren(el("p", "muted", "Загрузок за этот период нет")); return; }
    const max = Math.max(1, ...platforms.map((p) => num(p.total)));
    const fragment = document.createDocumentFragment();
    for (const entry of platforms) {
      const row = el("div", "platform-row");
      const name = entry.platform ? translated(entry.platform) : "Не указано";
      const detail = el("div", "platform-detail");
      const track = el("div", "platform-track");
      const fill = el("div", "platform-fill");
      fill.style.width = `${Math.max(0, Math.min(100, num(entry.total) / max * 100))}%`;
      track.append(fill);
      detail.append(el("span", "platform-name", name), track);
      row.append(el("span", "platform-symbol", name.slice(0, 1)), detail, el("span", "platform-count", count(entry.total)));
      row.title = `Успешно: ${count(entry.done)} · ошибок: ${count(entry.failed)} · ${bytes(entry.bytes)}`;
      fragment.append(row);
    }
    root.replaceChildren(fragment);
  }
  function updateOptions(options) {
    for (const [key, id] of [["platforms", "filter-platform"], ["statuses", "filter-status"], ["media_types", "filter-media"], ["actions", "filter-action"], ["outcomes", "filter-outcome"]]) {
      if (!Array.isArray(options[key])) continue;
      const select = $(id), selected = select.value;
      const first = select.options[0].cloneNode(true);
      const values = options[key].map((entry) => typeof entry === "object" && entry ? entry.value || entry.id : entry).filter((value) => value != null && value !== "");
      if (selected && !values.includes(selected)) values.push(selected);
      select.replaceChildren(first);
      for (const value of values) { const option = el("option", "", key === "actions" ? actionLabel(value) : translated(value)); option.value = value; select.append(option); }
      select.value = selected;
    }
  }
  function cell(text, className = "") { return el("td", className, text); }
  function userCell(item) {
    const td = cell("");
    if (item.telegram_id == null) { td.textContent = "Система"; return td; }
    const button = el("button", "user-link", item.username ? `@${String(item.username).replace(/^@/, "")}` : String(item.telegram_id));
    button.type = "button";
    button.title = "Показать события пользователя";
    button.addEventListener("click", () => { $("filter-user").value = String(item.telegram_id); setTab("events", false); apply(); });
    td.append(button);
    if (item.username) td.append(el("span", "cell-secondary", item.telegram_id));
    return td;
  }
  function statusCell(value) {
    const td = cell("");
    const category = ["error", "failed"].includes(value) ? "error" : ["cancelled", "canceled"].includes(value) ? "cancelled" : ["success", "done", "completed"].includes(value) ? "" : "other";
    td.append(el("span", `status-pill ${category}`, translated(value)));
    return td;
  }
  function renderTable() {
    const data = state.data || {}, users = state.tab === "users";
    const rows = users ? data.users || [] : data.activity || [];
    const headers = users ? ["Пользователь", "Первый запуск", "Загрузки", "Успешно", "Ошибки", "Отмены", "События", "Последняя активность"] : ["Время", "Пользователь", "Действие", "Платформа", "Тип", "Результат", "Размер", "Время обработки"];
    const tr = el("tr");
    for (const title of headers) { const th = el("th", "", title); th.scope = "col"; tr.append(th); }
    $("table-head").replaceChildren(tr);
    const fragment = document.createDocumentFragment();
    for (const item of rows) {
      const row = el("tr");
      if (users) row.append(userCell(item), cell(date(item.registration_date)), cell(count(item.conversions), "cell-primary"), cell(count(item.done)), cell(count(item.failed)), cell(count(item.cancelled)), cell(count(item.events)), cell(date(item.last_activity)));
      else row.append(cell(date(item.created_at)), userCell(item), cell(actionLabel(item.action || item.kind), "cell-primary"), cell(item.platform ? translated(item.platform) : "Не указано"), cell(translated(item.media_type)), statusCell(item.outcome), cell(bytes(item.file_size)), cell(item.duration_ms == null ? "—" : `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(num(item.duration_ms) / 1000)} с`));
      fragment.append(row);
    }
    $("table-body").replaceChildren(fragment);
    $("table-empty").hidden = rows.length > 0;
    const pagination = data.pagination || {};
    const total = num(users ? pagination.users_total : pagination.activity_total);
    const pages = Math.max(1, Math.ceil(total / state.pageSize));
    $("result-count").textContent = `${count(total)} записей`;
    $("page-label").textContent = `${state.page} / ${pages}`;
    $("page-prev").disabled = state.page <= 1;
    $("page-next").disabled = state.page >= pages;
  }
  function setTab(tab, reload = true) {
    state.tab = tab;
    document.querySelectorAll("[data-tab]").forEach((button) => button.setAttribute("aria-selected", String(button.dataset.tab === tab)));
    $("table-region").setAttribute("aria-labelledby", `tab-${tab}`);
    state.page = 1;
    if (reload) load();
  }
  $("filters").addEventListener("submit", (event) => { event.preventDefault(); apply(); });
  document.querySelectorAll("[data-period]").forEach((button) => button.addEventListener("click", () => {
    state.period = button.dataset.period;
    document.querySelectorAll("[data-period]").forEach((other) => { const selected = other === button; other.classList.toggle("selected", selected); other.setAttribute("aria-pressed", String(selected)); });
    $("custom-dates").hidden = state.period !== "custom";
    if (state.period !== "custom") apply();
  }));
  document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => setTab(button.dataset.tab)));
  $("reset").addEventListener("click", () => {
    $("filters").reset();
    $("exclude-owner").checked = true;
    document.querySelector('[data-period="30d"]').click();
  });
  $("refresh").addEventListener("click", load);
  $("page-size").addEventListener("change", () => { state.pageSize = num($("page-size").value); state.page = 1; load(); });
  $("page-prev").addEventListener("click", () => { if (state.page > 1) { state.page--; load(); } });
  $("page-next").addEventListener("click", () => { state.page++; load(); });
  $("logout").addEventListener("click", async () => {
    $("logout").disabled = true;
    try {
      await api("logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      endSession("Доступ завершён для всех ссылок и сессий. Для новой сессии отправьте /dashboard боту.");
    } catch (error) { notice(error.message); }
    finally { $("logout").disabled = false; }
  });
  $("retry-auth").addEventListener("click", () => authenticate());
  document.addEventListener("visibilitychange", () => { if (!document.hidden) checkExpiry(); });
  $("exclude-owner").checked = true;
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("login-submit").disabled = true;
    try { await authenticate(ticket, $("access-password").value.trim()); }
    finally { $("login-submit").disabled = false; }
  });
  if (ticket) {
    $("login-form").hidden = false;
    $("retry-auth").hidden = true;
    gate("Введите одноразовый пароль из того же сообщения бота. Доступ — на 10 минут с момента запроса.");
    $("access-password").focus();
  } else {
    gate("Проверяем доступ к статистике…");
    authenticate();
  }
})();
