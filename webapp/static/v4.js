(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  let state, selectedTask;
  const el = (tag, text, cls = "") => {
    const node = document.createElement(tag); node.textContent = text; node.className = cls; return node;
  };
  const notify = message => {
    const toast = $("toast"); toast.textContent = message; toast.classList.add("show");
    setTimeout(() => toast.classList.remove("show"), 4000);
  };
  async function api(path, method = "GET", payload) {
    const response = await fetch(`/api/v4/${path}`, {method,
      headers: {"X-Telegram-Init-Data": window.Telegram?.WebApp?.initData || "", "Content-Type": "application/json"},
      body: payload === undefined ? undefined : JSON.stringify(payload)});
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }
  function button(label, action) {
    const node = el("button", label, "secondary small"); node.type = "button";
    node.addEventListener("click", async () => {
      node.disabled = true;
      try { await action(); } catch (error) { notify(error.message); }
      finally { node.disabled = false; }
    });
    return node;
  }
  function renderTasks() {
    const target = $("v4Tasks"); target.replaceChildren();
    for (const task of state.tasks.filter(t => (!$("taskFilter").value || t.status === $("taskFilter").value)
      && (!$("sourceFilter").value || t.source === $("sourceFilter").value)
      && (t.course || "").toLowerCase().includes($("courseFilter").value.toLowerCase()))) {
      const row = el("article", "", "list-item");
      row.append(el("h3", `#${task.id} ${task.title}`), el("p", `${task.course || "—"} · ${task.source} · ${task.status} · ${task.progress}%`),
        el("p", task.due_at || "Deadline unknown", "muted"));
      row.append(button("Edit", () => {
        const form = $("taskForm"); form.reset();
        selectedTask = task;
        for (const [key, value] of Object.entries(task)) if (form.elements.namedItem(key)) {
          form.elements.namedItem(key).value = value ?? "";
        }
        form.elements.due_at.value = task.due_at ? task.due_at.replace(" ", "T").slice(0, 16) : "";
        form.elements.teacher_override.checked = false;
        $("taskEditorTitle").textContent = `Edit #${task.id}`;
        form.scrollIntoView({behavior: "smooth"});
      }));
      if (task.status !== "done") row.append(button("Done", async () => {
        await fetchDone(task.id); await load(); $("refreshBtn").click();
      }));
      target.append(row);
    }
    if (!target.children.length) target.append(el("p", "No matching tasks."));
  }
  async function fetchDone(id) {
    const response = await fetch(`/api/assignments/${id}/done`, {method: "POST", headers: {"X-Telegram-Init-Data": window.Telegram?.WebApp?.initData || ""}});
    if (!response.ok) throw new Error(await response.text());
  }
  function renderPlan() {
    const target = $("v4Plan"); target.replaceChildren();
    for (const [id, risk] of Object.entries(state.plan.risks)) {
      const box = el("article", "", "list-item");
      box.append(el("h3", `#${id} · risk ${risk.score}/100`), el("p", Object.entries(risk.components).map(([k, v]) => `${k}: ${v}`).join(" · ")),
        el("p", risk.uncertainties.join(" · "), "muted")); target.append(box);
    }
    if (state.plan.overloaded) target.append(el("p", "Overloaded: reduce scope or negotiate deadlines. Rest is protected."));
    for (const b of state.plan.blocks) target.append(el("p", `${b.start} → ${b.end} · #${b.task_id} ${b.title}`));
    for (const t of state.plan.unscheduled) target.append(el("p", `#${t.task_id ?? t.id} ${t.title}: ${t.reason}`));
  }
  function renderWeek() {
    const target = $("v4Week"); target.replaceChildren();
    for (const day of state.week) {
      const box = el("details", "", "list-item"); box.append(el("summary", day.date));
      for (const b of day.blocks) {
        const line = el("div", `${b.start}–${b.end} ${b.title}${b.duration_estimated ? " (end estimated)" : ""} · ${b.provenance || "schedule"}`);
        if (typeof b.id === "number") line.append(button("Change this date", () => {
          const f = $("overrideForm"); f.elements.date.value = day.date; f.elements.entry_id.value = b.id;
          f.elements.start.value = b.start; f.elements.end.value = b.end; f.elements.cancelled.checked = false;
          f.scrollIntoView({behavior: "smooth"});
        }));
        box.append(line);
      }
      target.append(box);
    }
  }
  function renderMemory() {
    const target = $("v4Memory"); target.replaceChildren();
    for (const fact of state.memory) {
      const box = el("article", "", "list-item");
      const field = document.createElement("textarea"); field.value = fact.value; field.maxLength = 8000;
      field.setAttribute("aria-label", `Value for ${fact.key}`);
      box.append(el("h3", fact.key), el("p", `${fact.provenance} · confidence ${fact.confidence} · expires ${fact.expires_at || "never"}`, "muted"), field,
        button("Save correction", async () => { await api(`memory/${fact.id}`, "PATCH", {value: field.value}); await load(); notify("Memory corrected"); }),
        button("Delete", async () => { if (window.confirm(`Delete ${fact.key}?`)) { await api(`memory/${fact.id}`, "DELETE"); await load(); } }));
      target.append(box);
    }
  }
  async function load() {
    state = await api("state"); renderTasks(); renderPlan(); renderWeek(); renderMemory();
    const form = $("preferencesForm");
    for (const [key, value] of Object.entries(state.preferences)) {
      const field = form.elements.namedItem(key); if (!field) continue;
      if (field.type === "checkbox") field.checked = value;
      else field.value = Array.isArray(value) ? value.join(", ") : value;
    }
  }
  function submit(id, action) {
    $(id).addEventListener("submit", async event => {
      event.preventDefault(); event.submitter.disabled = true;
      try { await action(event.target); await load(); $("refreshBtn").click(); notify("Saved"); }
      catch (error) { notify(error.message); }
      finally { event.submitter.disabled = false; }
    });
  }
  submit("taskForm", async form => {
    const payload = Object.fromEntries(new FormData(form)); const id = payload.id; delete payload.id;
    for (const key of ["estimated_minutes", "progress", "importance", "consequence"]) payload[key] = payload[key] === "" ? null : Number(payload[key]);
    payload.due_at = payload.due_at || null; payload.course = payload.course || null;
    payload.teacher_override = form.elements.teacher_override.checked;
    // Fields outside this compact editor retain their values on edits.
    for (const key of ["url", "preparation_minutes", "testing_buffer_minutes"]) if (id && selectedTask) payload[key] = selectedTask[key];
    await api(id ? `tasks/${id}` : "tasks", id ? "PATCH" : "POST", payload);
  });
  $("taskForm").addEventListener("reset", () => { selectedTask = null; $("taskEditorTitle").textContent = "New task"; });
  submit("overrideForm", form => api("overrides", "POST", {date: form.elements.date.value, entry_id: Number(form.elements.entry_id.value),
    cancelled: form.elements.cancelled.checked, changes: form.elements.cancelled.checked ? {} : {start: form.elements.start.value, end: form.elements.end.value}}));
  submit("preferencesForm", form => {
    const payload = Object.fromEntries(new FormData(form));
    for (const key of ["sleep_hours", "default_commute_minutes", "max_work_minutes"]) payload[key] = Number(payload[key]);
    for (const key of ["morning_brief", "evening_brief", "quiz_open_notices"]) payload[key] = form.elements.namedItem(key).checked;
    payload.muted_kinds = payload.muted_kinds.split(",").map(x => x.trim()).filter(Boolean);
    return api("preferences", "POST", payload);
  });
  for (const id of ["taskFilter", "sourceFilter", "courseFilter"]) $(id).addEventListener("input", () => state && renderTasks());
  $("diagnosticsBtn").addEventListener("click", async () => {
    try { $("v4Diagnostics").textContent = JSON.stringify(await api("diagnostics"), null, 2); }
    catch (error) { notify(error.message); }
  });
  $("refreshBtn").addEventListener("click", () => load().catch(error => notify(error.message)));
  load().catch(error => notify(`Auth/API: ${error.message}`));
})();
