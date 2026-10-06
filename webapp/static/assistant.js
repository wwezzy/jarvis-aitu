(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  let snapshot, sequence = 0, notesSequence = 0;
  const node = (tag, text, cls = "") => {
    const value = document.createElement(tag); value.textContent = text; value.className = cls; return value;
  };
  function toast(text) {
    $("toast").textContent = text; $("toast").classList.add("show");
    setTimeout(() => $("toast").classList.remove("show"), 5000);
  }
  async function api(path, method = "GET", payload) {
    const response = await fetch(`/api/v4/${path}`, {method,
      headers: {"X-Telegram-Init-Data": window.Telegram?.WebApp?.initData || "", "Content-Type": "application/json"},
      body: payload === undefined ? undefined : JSON.stringify(payload)});
    if (!response.ok) throw new Error(await response.text());
    const result = await response.json();
    if (method !== "GET") window.dispatchEvent(new CustomEvent("jarvis:changed", {detail: {origin: "assistant"}}));
    return result;
  }
  function action(label, callback) {
    const value = node("button", label, "secondary small"); value.type = "button";
    value.addEventListener("click", async () => {
      value.disabled = true;
      try { await callback(); } catch (error) { toast(error.message); }
      finally { value.disabled = false; }
    }); return value;
  }
  async function load() {
    const request = ++sequence;
    const result = await api("assistant");
    if (request !== sequence) return;
    snapshot = result;
    $("assistantNext").textContent = result.next_action.text;
    $("startSuggestedFocus").disabled = !["goal", "task"].includes(result.next_action.kind);
    $("assistantFocus").textContent = result.focus ? `${result.focus.title} · до ${new Date(result.focus.end).toLocaleTimeString()} · результат ещё неизвестен` : "Фокус не запущен";
    $("finishFocusForm").hidden = !result.focus;
    const goals = $("assistantGoals"); goals.replaceChildren();
    for (const goal of result.goals) {
      const box = node("article", "", "list-item");
      box.append(node("h3", `#${goal.id} ${goal.title}`), node("p", `${goal.week_completed}/${goal.target_per_week} дней · ${goal.week_minutes} минут по отметкам · сегодня: ${goal.today}`), node("p", `Если ${goal.cue}, то ${goal.next_step} · минимум ${goal.minimum_minutes} мин`));
      box.append(action("Сделал минимум", async () => {
        if (!window.confirm(`Подтвердить, что выполнен шаг «${goal.next_step}» не меньше ${goal.minimum_minutes} мин?`)) return;
        await api("checkins", "POST", {goal_id: goal.id, outcome: "done", minutes: goal.minimum_minutes}); await load();
      }), action("Сегодня пропускаю", async () => { await api("checkins", "POST", {goal_id: goal.id, outcome: "skipped", minutes: 0}); await load(); }),
      action("Изменить", () => {
        const form = $("goalForm"); form.reset();
        for (const [key, value] of Object.entries(goal)) if (form.elements.namedItem(key)) form.elements.namedItem(key).value = value;
        form.scrollIntoView({behavior: "smooth"});
      }), action("Отключить цель", async () => { await api(`goals/${goal.id}`, "PATCH", {enabled: false}); await load(); }));
      goals.append(box);
    }
    if (!result.goals.length) goals.append(node("p", "Добавь один ориентир. Стартовый набор также доступен через /autopilot on."));
    const activity = $("assistantActivity"); activity.replaceChildren();
    const labels = {work: "Рабочие приложения", gaming: "Игры", media: "Медиа", idle: "Простой", other: "Другое", unknown: "Неизвестно"};
    for (const [key, value] of Object.entries(result.activity.seconds)) activity.append(node("p", `${labels[key]}: ${(value / 60).toFixed(1)} мин`));
    activity.append(node("p", `Покрытие наблюдения: ${result.activity.coverage_minutes} мин; весь день не подразумевается.`, "muted"));
  }
  async function notes() {
    const request = ++notesSequence;
    const query = $("noteQuery").value;
    const result = await api(query ? "notes?q=" + encodeURIComponent(query) : "notes?status=inbox");
    if (request !== notesSequence) return;
    const target = $("assistantNotes"); target.replaceChildren();
    for (const note of result) {
      const box = node("article", "", "list-item");
      box.append(node("h3", `#${note.id} ${note.title}`), node("p", `${note.status} · ${note.source}`, "muted"));
      const details = node("details", ""); details.append(node("summary", "Источник"), node("p", note.content)); box.append(details);
      for (const [status, label] of [["action", "Нужно действие"], ["reference", "Справка"], ["archived", "В архив"]]) box.append(action(label, async () => { await api(`notes/${note.id}`, "PATCH", {status}); await notes(); }));
      box.append(action("Удалить", async () => { if (window.confirm(`Удалить запись #${note.id}?`)) { await api(`notes/${note.id}`, "DELETE"); await notes(); } }));
      target.append(box);
    }
    if (!result.length) target.append(node("p", "Записей по запросу нет."));
  }
  function submit(id, callback) {
    $(id).addEventListener("submit", async event => {
      event.preventDefault(); event.submitter.disabled = true;
      try { await callback(event.target); await load(); }
      catch (error) { toast(error.message); }
      finally { event.submitter.disabled = false; }
    });
  }
  $("startSuggestedFocus").addEventListener("click", async () => {
    const button = $("startSuggestedFocus"); button.disabled = true;
    try {
      const next = snapshot.next_action;
      await api("focus", "POST", {action: "start", minutes: next.minutes,
        ...(next.kind === "goal" ? {goal_id: next.goal_id} : {task_id: next.task_id})}); await load();
    } catch (error) { toast(error.message); button.disabled = false; }
  });
  submit("finishFocusForm", async form => { const result = await api("focus", "POST", {action: "stop", minutes: Number(form.elements.minutes.value)}); toast(result.message); form.reset(); });
  $("cancelFocus").addEventListener("click", async () => { try { await api("focus", "POST", {action: "cancel"}); await load(); } catch (error) { toast(error.message); } });
  submit("goalForm", async form => {
    const payload = Object.fromEntries(new FormData(form)); const id = payload.id; delete payload.id;
    for (const key of ["minimum_minutes", "target_per_week"]) payload[key] = Number(payload[key]);
    await api(id ? `goals/${id}` : "goals", id ? "PATCH" : "POST", payload); form.reset(); toast("Цель сохранена");
  });
  submit("captureForm", async form => { await api("notes", "POST", Object.fromEntries(new FormData(form))); form.reset(); await notes(); toast("Источник сохранён"); });
  $("searchNotes").addEventListener("click", () => notes().catch(error => toast(error.message)));
  $("refreshAssistant").addEventListener("click", () => load().catch(error => toast(error.message)));
  $("refreshPcStatus").addEventListener("click", async () => {
    try { $("assistantPcStatus").textContent = JSON.stringify(await api("pc_status"), null, 2); }
    catch (error) { toast(error.message); }
  });
  window.addEventListener("jarvis:changed", () => load().catch(error => toast(error.message)));
  load().catch(error => toast(error.message)); notes().catch(error => toast(error.message));
})();
