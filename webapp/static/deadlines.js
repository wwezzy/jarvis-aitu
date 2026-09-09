(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;
  const $ = (id) => document.getElementById(id);

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("X-Telegram-Init-Data", tg?.initData || "");
    if (options.body) headers.set("Content-Type", "application/json");
    const response = await fetch(path, { ...options, headers });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `HTTP ${response.status}`);
    }
    return response.json();
  }

  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function dueLabel(value) {
    if (!value) return "deadline unknown";
    const date = new Date(value.replace(" ", "T"));
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString([], { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  async function markDone(item) {
    const button = document.querySelector(`[data-assignment-done="${item.id}"]`);
    if (button) button.disabled = true;
    try {
      const data = await api(`/api/assignments/${item.id}/done`, { method: "POST", body: "{}" });
      renderAssignments(data.assignments || []);
      tg?.HapticFeedback?.impactOccurred?.("medium");
    } catch (error) {
      if (button) button.textContent = "Retry";
      console.error("Assignment complete failed", error);
    } finally {
      if (button) button.disabled = false;
    }
  }

  function renderAssignments(items) {
    const target = $("assignmentList");
    if (!target) return;
    target.replaceChildren();
    if (!items.length) {
      target.append(node("div", "muted", "Активных дедлайнов пока нет."));
      return;
    }
    for (const item of items.slice(0, 20)) {
      const box = node("div", "list-item");
      const course = item.course ? `[${item.course}] ` : "";
      box.append(node("div", "list-title", `${dueLabel(item.due_at)} · ${course}${item.title}`));
      box.append(node("div", "list-meta", `${item.source === "lms_ical" ? "LMS" : "Jarvis"} · #${item.id}`));
      const done = node("button", "schedule-action", "Done");
      done.type = "button";
      done.dataset.assignmentDone = String(item.id);
      done.addEventListener("click", () => markDone(item));
      box.append(done);
      target.append(box);
    }
  }

  function renderLms(status) {
    const target = $("lmsStatusText");
    const syncButton = $("syncLmsBtn");
    if (!target || !syncButton) return;
    if (!status?.configured) {
      target.textContent = "LMS not connected · set LMS_ICAL_URL in Render Environment";
      syncButton.disabled = true;
      syncButton.dataset.permanentDisabled = "1";
      return;
    }
    syncButton.dataset.permanentDisabled = "0";
    syncButton.disabled = false;
    if (status.last_success_at) {
      const date = new Date(status.last_success_at.replace(" ", "T"));
      const text = Number.isNaN(date.getTime()) ? status.last_success_at : date.toLocaleString();
      target.textContent = `LMS connected · last sync ${text}`;
    } else {
      target.textContent = "LMS connected · waiting for first sync";
    }
    if (status.last_error) target.textContent += " · last sync error";
  }

  async function loadDeadlines() {
    if (!$("assignmentList")) return;
    try {
      const data = await api("/api/dashboard");
      renderAssignments(data.assignments || []);
      renderLms(data.lms || {});
    } catch (error) {
      const target = $("lmsStatusText");
      if (target) target.textContent = `Deadline API error: ${error.message}`;
    }
  }

  $("syncLmsBtn")?.addEventListener("click", async () => {
    const button = $("syncLmsBtn");
    button.disabled = true;
    button.textContent = "Sync…";
    try {
      const data = await api("/api/lms/sync", { method: "POST", body: "{}" });
      renderAssignments(data.assignments || []);
      renderLms(data.lms || {});
      tg?.HapticFeedback?.impactOccurred?.("medium");
    } catch (error) {
      const target = $("lmsStatusText");
      if (target) target.textContent = `LMS sync error: ${error.message}`;
    } finally {
      button.textContent = "Sync LMS";
      if (button.dataset.permanentDisabled !== "1") button.disabled = false;
    }
  });

  loadDeadlines();
  setInterval(loadDeadlines, 5 * 60 * 1000);
})();
