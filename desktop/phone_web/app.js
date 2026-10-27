"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const tokenKey = "jarvis.phone.token";
  let token = "", snapshot = null, dayKey = "today", busy = false, gymDirty = false;
  const pending = new Map();
  const errors = {authorization_required: "Ключ не подходит. Подключись заново.", rate_limit: "Слишком много запросов. Подожди минуту.",
    result_uncertain: "Результат пока не подтверждён. Сначала обнови и проверь план, не создавай повтор вслепую.",
    operation_failed_inspect_plan: "Не удалось подтвердить изменение. Обнови план перед повтором.",
    idempotency_conflict: "Этот запрос уже использован для другого действия. Проверь план.",
    plan_item_invalid: "Проверь дело, дату и выбранные дни. Оно могло быть изменено на ноутбуке.",
    request_store_full: "Хранилище подтверждений заполнено. Нужна проверка на ноутбуке.",
    invalid_request: "Проверь заполненные поля.", plan_unavailable: "План временно недоступен. Проверь Jarvis на ноутбуке."};
  function notice(text, bad = false) { $("notice").textContent = text; $("notice").classList.toggle("error", bad); }
  function savedToken(value) { try { if (value) sessionStorage.setItem(tokenKey, value); else sessionStorage.removeItem(tokenKey); } catch (_) { /* Private browsing can deny storage; the current tab still works. */ } }
  try {
    const fragment = new URLSearchParams(location.hash.slice(1));
    token = fragment.get("token") || sessionStorage.getItem(tokenKey) || "";
  } catch (_) { token = ""; }
  if (location.hash) history.replaceState(null, "", location.pathname);
  function newKey() { return Array.from(crypto.getRandomValues(new Uint8Array(24)), (n) => n.toString(16).padStart(2, "0")).join(""); }
  async function request(path, payload) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
    const headers = {Authorization: "Bearer " + token};
    const options = {method: payload ? "POST" : "GET", headers, credentials: "omit", cache: "no-store", signal: controller.signal};
    let identity;
    if (payload) {
      identity = path + "\n" + JSON.stringify(payload);
      if (!pending.has(identity)) pending.set(identity, newKey());
      headers["Content-Type"] = "application/json"; headers["Idempotency-Key"] = pending.get(identity);
      options.body = JSON.stringify(payload);
    }
    try {
      const response = await fetch(path, options), data = await response.json();
      if (!response.ok) {
        if (response.status === 401) { $("pairing").hidden = false; $("workspace").hidden = true; snapshot = null; $("items").replaceChildren(); token = ""; savedToken(""); }
        if (response.status === 400 && identity) pending.delete(identity);
        throw new Error(errors[data.error] || "Изменение не подтверждено. Обнови план и проверь соединение.");
      }
      if (identity) pending.delete(identity);
      return data;
    } catch (error) {
      if (error.name === "AbortError" || error instanceof TypeError) throw new Error("Ноутбук не отвечает. Проверь, включён ли Jarvis и личное защищённое соединение. Изменение не подтверждено.");
      throw error;
    } finally { clearTimeout(timer); }
  }
  function element(tag, text, className) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; }
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
  async function refresh(quiet = false) {
    if (!token) return;
    try {
      snapshot = await request("/api/plan"); render(); savedToken(token);
      $("workspace").hidden = false; $("pairing").hidden = true; $("token").value = "";
      $("connection").textContent = "Подключено к твоему ноутбуку";
      if (!quiet) notice("План обновлён.");
      return true;
    } catch (error) { $("connection").textContent = "Связь с ноутбуком не подтверждена"; notice(error.message, true); return false; }
  }
  async function mutate(path, payload, success) {
    if (busy) return;
    busy = true; document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    try { await request(path, payload); if (path === "/api/plan") $("plan-title").value = ""; if (path === "/api/gym") gymDirty = false; const updated = await refresh(true); notice(updated ? success : success + " Свежий план пока не получен.", !updated); }
    catch (error) { notice(error.message, true); }
    finally { busy = false; document.querySelectorAll("button").forEach((button) => { button.disabled = false; }); }
  }
  $("connect-form").addEventListener("submit", (event) => { event.preventDefault(); token = $("token").value.trim(); if (!/^[A-Za-z0-9_-]{32,256}$/.test(token)) { notice("Нужен полный личный ключ из настройки на ноутбуке.", true); return; } refresh(); });
  $("plan-form").addEventListener("submit", (event) => { event.preventDefault(); mutate("/api/plan", {date_key: $("plan-date").value, title: $("plan-title").value.trim()}, "Новое дело добавлено."); });
  $("gym-form").addEventListener("submit", (event) => { event.preventDefault(); const weekdays = Array.from(document.querySelectorAll('input[name="weekday"]:checked'), (input) => Number(input.value)); mutate("/api/gym", {weekdays, time: $("gym-time").value || null}, weekdays.length ? "Дни зала сохранены." : "Недельное правило зала отключено."); });
  $("gym-form").addEventListener("input", () => { gymDirty = true; });
  $("today-tab").addEventListener("click", () => { dayKey = "today"; render(); });
  $("tomorrow-tab").addEventListener("click", () => { dayKey = "tomorrow"; render(); });
  $("refresh").addEventListener("click", () => refresh());
  $("disconnect").addEventListener("click", () => { token = ""; snapshot = null; gymDirty = false; pending.clear(); savedToken(""); $("items").replaceChildren(); for (const id of ["plan-title", "plan-date", "gym-time", "token"]) $(id).value = ""; document.querySelectorAll('input[name="weekday"]').forEach((input) => { input.checked = false; }); $("workspace").hidden = true; $("pairing").hidden = false; $("connection").textContent = "Эта вкладка отключена"; notice("Ключ удалён из этой вкладки."); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !busy) refresh(true); });
  setInterval(() => { if (token && !document.hidden && !busy) refresh(true); }, 30000);
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  if (token) refresh();
})();
