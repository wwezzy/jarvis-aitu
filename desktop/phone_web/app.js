"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const tokenKey = "jarvis.phone.token";
  const controlKeyName = "jarvis.phone.control";
  let token = "", snapshot = null, dayKey = "today", busy = false, gymDirty = false;
  let controlKey = "", controlSnapshot = null, controlBusy = false, challenge = null;
  let sessionGeneration = 0, controlGeneration = 0;
  const pending = new Map();
  const errors = {authorization_required: "Ключ не подходит. Подключись заново.", rate_limit: "Слишком много запросов. Подожди минуту.",
    result_uncertain: "Результат пока не подтверждён. Сначала обнови и проверь план, не создавай повтор вслепую.",
    operation_failed_inspect_plan: "Не удалось подтвердить изменение. Обнови план перед повтором.",
    idempotency_conflict: "Этот запрос уже использован для другого действия. Проверь план.",
    plan_item_invalid: "Проверь дело, дату и выбранные дни. Оно могло быть изменено на ноутбуке.",
    request_store_full: "Хранилище подтверждений заполнено. Нужна проверка на ноутбуке.",
    invalid_request: "Проверь заполненные поля.", plan_unavailable: "План временно недоступен. Проверь Jarvis на ноутбуке.",
    control_authorization_required: "Ключ управления или разрешение владельца не подтверждены. Проверь настройку на ноутбуке.",
    control_unavailable: "Удалённое управление не подключено на ноутбуке.",
    control_request_invalid: "Эта команда не разрешена. Выбери действие из списка.",
    control_result_uncertain: "Результат команды не подтверждён. Проверь ноутбук перед новым запросом; повтор автоматически не отправляется."};
  function notice(text, bad = false) { $("notice").textContent = text; $("notice").classList.toggle("error", bad); }
  function savedToken(value) { try { if (value) sessionStorage.setItem(tokenKey, value); else sessionStorage.removeItem(tokenKey); } catch (_) { /* Private browsing can deny storage; the current tab still works. */ } }
  try {
    const fragment = new URLSearchParams(location.hash.slice(1));
    token = fragment.get("token") || sessionStorage.getItem(tokenKey) || "";
    controlKey = sessionStorage.getItem(controlKeyName) || "";
  } catch (_) { token = ""; }
  if (location.hash) history.replaceState(null, "", location.pathname);
  function newKey() { return Array.from(crypto.getRandomValues(new Uint8Array(24)), (n) => n.toString(16).padStart(2, "0")).join(""); }
  function session(control = false) { return {generation: sessionGeneration, token, control, controlGeneration, controlKey}; }
  function current(context) {
    return context.generation === sessionGeneration && context.token === token &&
      (!context.control || context.controlGeneration === controlGeneration && context.controlKey === controlKey);
  }
  function staleSession() { const error = new Error("Session changed"); error.name = "StaleSessionError"; return error; }
  function requireCurrent(context) { if (!current(context)) throw staleSession(); }
  function syncButtons() {
    document.querySelectorAll("button").forEach((item) => { item.disabled = busy || controlBusy; });
    // Disconnect closes this tab's access; it cannot cancel a command that the
    // laptop has already received. It remains available during pending I/O.
    $("disconnect").disabled = false; $("control-disconnect").disabled = false;
    updateChallenge();
  }
  async function request(path, payload, control = false, context = session(control)) {
    requireCurrent(context);
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
    const headers = {Authorization: "Bearer " + context.token};
    if (control) headers["X-Jarvis-Control"] = context.controlKey;
    const options = {method: payload ? "POST" : "GET", headers, credentials: "omit", cache: "no-store", signal: controller.signal};
    let identity;
    if (payload) {
      identity = path + "\n" + JSON.stringify(payload);
      if (!pending.has(identity)) pending.set(identity, newKey());
      headers["Content-Type"] = "application/json"; headers["Idempotency-Key"] = pending.get(identity);
      options.body = JSON.stringify(payload);
    }
    try {
      const response = await fetch(path, options);
      requireCurrent(context);
      const data = await response.json();
      requireCurrent(context);
      if (!response.ok) {
        if (response.status === 401) { clearSession(); $("connection").textContent = "Связь с ноутбуком не подтверждена"; notice(errors.authorization_required, true); throw staleSession(); }
        if (control && (response.status === 403 || data.error === "control_unavailable")) {
          clearControl(); controlNotice(errors[data.error] || errors.control_authorization_required, true); throw staleSession();
        }
        if (response.status === 400 && identity) pending.delete(identity);
        if (control && data.error === "result_uncertain") throw new Error("Исход этой команды неизвестен. Проверь ноутбук перед новым действием; сохранённый запрос повторно не выполняется.");
        if (control && data.error === "idempotency_conflict") throw new Error("Этот запрос уже использован для другой команды. Проверь состояние ноутбука перед новым действием.");
        throw new Error(errors[data.error] || "Изменение не подтверждено. Обнови план и проверь соединение.");
      }
      if (identity) pending.delete(identity);
      return data;
    } catch (error) {
      requireCurrent(context);
      if (error.name === "AbortError" || error instanceof TypeError) throw new Error(control ? "Ноутбук не отвечает. Результат команды неизвестен; проверь его перед повтором." : "Ноутбук не отвечает. Проверь, включён ли Jarvis и личное защищённое соединение. Изменение не подтверждено.");
      throw error;
    } finally { clearTimeout(timer); }
  }
  function element(tag, text, className) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; }
  function controlNotice(text, bad = false) { $("control-notice").textContent = text; $("control-notice").classList.toggle("error", bad); }
  function saveControlKey(value) { try { if (value) sessionStorage.setItem(controlKeyName, value); else sessionStorage.removeItem(controlKeyName); } catch (_) {} }
  function clearChallenge() { challenge = null; $("control-confirmation").hidden = true; $("control-confirm-text").textContent = ""; $("control-countdown").textContent = ""; }
  function clearControl() {
    controlGeneration += 1;
    controlKey = ""; controlSnapshot = null; controlBusy = false; saveControlKey(""); clearChallenge(); controlNotice("");
    $("control-key").value = ""; $("control-workspace").hidden = true; $("control-connect-form").hidden = false;
    $("control-status").textContent = "Удалённое управление не подключено. Нужны отдельный ключ и разрешение на ноутбуке.";
    for (const id of ["control-actions", "control-projects", "control-apps"]) $(id).replaceChildren();
    for (const identity of pending.keys()) if (identity.startsWith("/api/control")) pending.delete(identity);
    syncButtons();
  }
  function clearSession() {
    sessionGeneration += 1;
    token = ""; snapshot = null; gymDirty = false; busy = false; dayKey = "today"; pending.clear(); savedToken(""); clearControl();
    $("items").replaceChildren();
    for (const id of ["plan-title", "plan-date", "gym-time", "token"]) $(id).value = "";
    document.querySelectorAll('input[name="weekday"]').forEach((input) => { input.checked = false; });
    $("day-title").textContent = "Твой план"; $("delivery").textContent = "Push и звонки не подключены.";
    $("workspace").hidden = true; $("pairing").hidden = false; $("connection").textContent = "Эта вкладка отключена";
  }
  function renderControl() {
    const available = controlSnapshot && controlSnapshot.available === true;
    $("control-workspace").hidden = !available; $("control-connect-form").hidden = available;
    $("control-status").textContent = available ? "Доступ к разрешённым командам подтверждён ноутбуком." : "Управление не подключено или требует разрешения владельца на ноутбуке.";
    for (const [key, kind] of [["actions", "action"], ["projects", "project"], ["apps", "app"]]) {
      const holder = $("control-" + key); holder.replaceChildren();
      const rows = available && Array.isArray(controlSnapshot[key]) ? controlSnapshot[key] : [];
      for (const row of rows) {
        if (!row || typeof row.name !== "string") continue;
        const text = (typeof row.title === "string" ? row.title : row.name) + (row.requires_confirmation ? " · подтверждение" : "");
        const item = element("button", text, "secondary"); item.type = "button";
        item.addEventListener("click", () => controlMutate("/api/control", {kind, name: row.name}));
        holder.append(item);
      }
      if (!rows.length) holder.append(element("p", "Нет настроенных команд.", "muted"));
    }
  }
  async function refreshControl(quiet = false, context = session(true)) {
    if (!context.token || !context.controlKey || controlBusy || !current(context)) return false;
    try {
      const result = await request("/api/control", undefined, true, context);
      if (!current(context)) return false;
      controlSnapshot = result; renderControl();
      if (controlSnapshot.available === true) { saveControlKey(context.controlKey); $("control-key").value = ""; }
      else { clearControl(); controlNotice(errors.control_unavailable, true); return false; }
      if (!quiet) controlNotice("Доступ проверен. Действие выполняется только после нажатия своей кнопки.");
      return true;
    } catch (error) { if (current(context)) controlNotice(error.message, true); return false; }
  }
  function updateChallenge() {
    if (!challenge) return;
    const remaining = Math.max(0, Math.ceil((challenge.expiresAt - Date.now()) / 1000));
    $("control-countdown").textContent = "Подтверждение: до " + remaining + " с. Автоматического выполнения нет.";
    $("control-confirm").disabled = controlBusy || remaining === 0;
    $("control-cancel").disabled = controlBusy || remaining === 0;
    if (!remaining) { clearChallenge(); controlNotice("Срок подтверждения истёк. Запроси действие заново, если оно ещё нужно.", true); }
  }
  async function controlMutate(path, payload) {
    if (busy || controlBusy || !controlKey || !controlSnapshot || controlSnapshot.available !== true) return;
    const context = session(true);
    controlBusy = true; clearChallenge();
    controlNotice("Проверяю результат запроса…");
    syncButtons();
    try {
      const result = await request(path, payload, true, context);
      if (!current(context)) return;
      controlNotice(typeof result.text === "string" ? result.text : "Результат команды не подтверждён.", result.ok !== true);
      if (typeof result.confirmation_id === "string" && /^[a-f0-9]{32}$/.test(result.confirmation_id) && Number.isInteger(result.expires_in) && result.expires_in > 0 && result.expires_in <= 60) {
        challenge = {token: result.confirmation_id, expiresAt: Date.now() + result.expires_in * 1000};
        $("control-confirm-text").textContent = result.text; $("control-confirmation").hidden = false;
      }
    } catch (error) { if (current(context)) controlNotice(error.message, true); }
    finally { if (current(context)) { controlBusy = false; syncButtons(); } }
  }
  function render() {
    if (!snapshot) return;
    const day = snapshot[dayKey];
    const date = new Date(day.date + "T12:00:00Z");
    $("day-title").textContent = date.toLocaleDateString("ru-RU", {weekday: "long", day: "numeric", month: "long", timeZone: "UTC"});
    $("items").replaceChildren();
    if (!day.items.length) $("items").append(element("p", "Пока пусто. Добавь одно конкретное дело.", "empty"));
    for (const item of day.items) {
      const card = element("article", undefined, "plan-item");
      card.append(element("p", item.time || "Без точного времени", "item-time"), element("h3", item.title));
      const status = {done: "Выполнено · с твоих слов", missed: "Пропущено · с твоих слов", unknown: "Пока неизвестно"}[item.status] || "Пока неизвестно";
      card.append(element("p", status, "item-status"));
      const controls = element("div", undefined, "reports");
      for (const [value, label] of [["done", "Готово"], ["missed", "Пропустил"], ["unknown", "Не знаю"]]) {
        const button = element("button", label, "secondary"); button.type = "button";
        button.setAttribute("aria-pressed", String(item.status === value));
        button.addEventListener("click", () => mutate("/api/report", {item_id: item.id, status: value}, "Отметка сохранена с твоих слов."));
        controls.append(button);
      }
      card.append(controls); $("items").append(card);
    }
    if (!gymDirty) {
      for (const input of document.querySelectorAll('input[name="weekday"]')) input.checked = snapshot.gym.enabled && snapshot.gym.days.includes(Number(input.value));
      $("gym-time").value = snapshot.gym.time || "";
    }
    if (!$("plan-date").value) $("plan-date").value = snapshot.tomorrow.date;
    $("delivery").textContent = snapshot.delivery.message;
    $("today-tab").classList.toggle("selected", dayKey === "today"); $("tomorrow-tab").classList.toggle("selected", dayKey === "tomorrow");
    $("today-tab").setAttribute("aria-pressed", String(dayKey === "today")); $("tomorrow-tab").setAttribute("aria-pressed", String(dayKey === "tomorrow"));
  }
  async function refresh(quiet = false, context = session()) {
    if (!context.token || !current(context)) return false;
    try {
      const result = await request("/api/plan", undefined, false, context);
      if (!current(context)) return false;
      snapshot = result; render(); savedToken(context.token);
      $("workspace").hidden = false; $("pairing").hidden = true; $("token").value = "";
      $("connection").textContent = "Подключено к твоему ноутбуку";
      if (controlKey && !controlSnapshot && !controlBusy) refreshControl(true);
      if (!quiet) notice("План обновлён.");
      return true;
    } catch (error) { if (current(context)) { $("connection").textContent = "Связь с ноутбуком не подтверждена"; notice(error.message, true); } return false; }
  }
  async function mutate(path, payload, success) {
    if (!token || busy || controlBusy) return;
    const context = session();
    busy = true; syncButtons();
    try {
      await request(path, payload, false, context);
      if (!current(context)) return;
      if (path === "/api/plan") $("plan-title").value = "";
      if (path === "/api/gym") gymDirty = false;
      const updated = await refresh(true, context);
      if (current(context)) notice(updated ? success : success + " Свежий план пока не получен.", !updated);
    } catch (error) { if (current(context)) notice(error.message, true); }
    finally { if (current(context)) { busy = false; syncButtons(); } }
  }
  $("control-connect-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const candidate = $("control-key").value.trim();
    if (!/^[A-Za-z0-9_-]{32,256}$/.test(candidate)) { controlNotice("Нужен отдельный полный ключ управления с ноутбука.", true); return; }
    clearControl(); controlKey = candidate;
    refreshControl();
  });
  $("control-refresh").addEventListener("click", () => refreshControl());
  $("control-disconnect").addEventListener("click", () => {
    const inFlight = controlBusy; clearControl();
    controlNotice(inFlight ? "Управление закрыто в этой вкладке. Уже отправленная команда могла выполниться; проверь ноутбук перед повтором." : "Управление закрыто в этой вкладке. План остаётся доступен.");
  });
  for (const [id, path] of [["control-confirm", "/api/control/confirm"], ["control-cancel", "/api/control/cancel"]]) {
    $(id).addEventListener("click", () => {
      if (!challenge || Date.now() >= challenge.expiresAt) { updateChallenge(); return; }
      const pendingToken = challenge.token; controlMutate(path, {token: pendingToken});
    });
  }
  $("connect-form").addEventListener("submit", (event) => {
    event.preventDefault(); const candidate = $("token").value.trim();
    if (!/^[A-Za-z0-9_-]{32,256}$/.test(candidate)) { notice("Нужен полный личный ключ из настройки на ноутбуке.", true); return; }
    clearSession(); token = candidate; $("connection").textContent = "Проверяю связь с ноутбуком…"; refresh();
  });
  $("plan-form").addEventListener("submit", (event) => { event.preventDefault(); mutate("/api/plan", {date_key: $("plan-date").value, title: $("plan-title").value.trim()}, "Новое дело добавлено."); });
  $("gym-form").addEventListener("submit", (event) => { event.preventDefault(); const weekdays = Array.from(document.querySelectorAll('input[name="weekday"]:checked'), (input) => Number(input.value)); mutate("/api/gym", {weekdays, time: $("gym-time").value || null}, weekdays.length ? "Дни зала сохранены." : "Недельное правило зала отключено."); });
  $("gym-form").addEventListener("input", () => { gymDirty = true; });
  $("today-tab").addEventListener("click", () => { dayKey = "today"; render(); });
  $("tomorrow-tab").addEventListener("click", () => { dayKey = "tomorrow"; render(); });
  $("refresh").addEventListener("click", () => refresh());
  $("disconnect").addEventListener("click", () => {
    const inFlight = busy || controlBusy; clearSession();
    notice(inFlight ? "Ключи удалены из этой вкладки. Уже отправленный запрос мог выполниться; проверь ноутбук перед повтором." : "Ключи удалены из этой вкладки.");
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !busy && !controlBusy) refresh(true); });
  setInterval(() => { if (token && !document.hidden && !busy && !controlBusy) refresh(true); }, 30000);
  setInterval(updateChallenge, 250);
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  if (token) refresh();
})();
