(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
  }

  const state = { dashboard: null };
  const $ = (id) => document.getElementById(id);
  const statusDot = $("statusDot");
  const toast = $("toast");

  function showToast(message, isError = false) {
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    toast.classList.add("show");
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => toast.classList.remove("show"), 2200);
  }

  function haptic(type = "light") {
    try { tg?.HapticFeedback?.impactOccurred(type); } catch (_) {}
  }

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

  function setView(name) {
    document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === name));
    document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
    history.replaceState(null, "", `#${name}`);
  }

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => setView(tab.dataset.view)));

  function renderSchedule(schedule) {
    const target = $("scheduleList");
    target.replaceChildren();
    for (const block of schedule.blocks) {
      const row = node("div", `timeline-item ${block.status}`);
      row.append(node("div", "timeline-time", block.start));
      const detail = node("div");
      detail.append(node("div", "timeline-title", block.title));
      detail.append(node("div", "list-meta", `${block.end} · ${block.category}`));
      row.append(detail);
      target.append(row);
    }
  }

  function renderGTG(gtg) {
    $("gtgToday").textContent = gtg.today.reps;
    $("gtgTodaySets").textContent = `${gtg.today.sets} sets`;
    $("gtgWeek").textContent = gtg.week.reps;
    $("gtgWeekSets").textContent = `${gtg.week.sets} sets`;
  }

  function renderProgression(items) {
    const target = $("progressionList");
    target.replaceChildren();
    if (!items.length) {
      target.append(node("div", "muted", "Нужна хотя бы одна структурированная тренировка."));
      return;
    }
    for (const item of items) {
      const box = node("div", "list-item");
      box.append(node("div", "list-title", item.exercise_name));
      box.append(node("div", `list-meta signal-${item.action}`, `${item.action} — ${item.reason}`));
      target.append(box);
    }
  }

  function formatSet(item) {
    const kg = item.weight_kg === null ? "BW" : `${item.weight_kg}kg`;
    const reps = item.reps === null ? "?" : item.reps;
    const rir = item.rir === null ? "RIR ?" : `RIR ${item.rir}`;
    return `${item.exercise_name}: ${kg} × ${reps} · ${rir}`;
  }

  function renderWorkouts(workouts) {
    const target = $("workoutHistory");
    target.replaceChildren();
    if (!workouts.length) {
      target.append(node("div", "muted", "Журнал пока пуст."));
      return;
    }
    for (const workout of workouts) {
      const box = node("div", "list-item");
      box.append(node("div", "list-title", `${workout.started_at.slice(0, 10)} · ${workout.title}`));
      box.append(node("div", "list-meta", `${workout.sets.length} sets · volume ${Math.round(workout.volume_kg)} kg`));
      for (const set of workout.sets.slice(0, 8)) box.append(node("div", "list-meta", formatSet(set)));
      target.append(box);
    }
  }

  function renderMemory(memory) {
    const target = $("memoryList");
    target.replaceChildren();
    if (!memory.length) {
      target.append(node("div", "muted", "Durable facts ещё не созданы."));
      return;
    }
    for (const item of memory) {
      const box = node("div", "list-item");
      box.append(node("div", "list-title", `[${item.category}] ${item.key}`));
      box.append(node("div", "list-meta", item.value));
      target.append(box);
    }
  }

  function fillReflection(reflection) {
    if (!reflection) return;
    $("deepWorkHours").value = reflection.deep_work_hours ?? "";
    $("proteinHit").checked = reflection.protein_hit === true;
    $("caloriesHit").checked = reflection.calories_hit === true;
    $("rirRespected").checked = reflection.rir_respected === true;
    $("mood").value = reflection.mood ?? "";
    $("energy").value = reflection.energy ?? "";
    $("reflectionNotes").value = reflection.notes ?? "";
  }

  async function loadDashboard() {
    statusDot.className = "status-dot";
    try {
      const data = await api("/api/dashboard");
      state.dashboard = data;
      statusDot.className = "status-dot online";
      const kind = data.schedule.kind === "FLEX" ? "FLEX DAY" : `DAY ${data.schedule.kind}`;
      $("dayKind").textContent = kind;
      $("serverTime").textContent = new Date(data.server_time).toLocaleString();
      renderSchedule(data.schedule);
      renderGTG(data.gtg);
      renderProgression(data.progression || []);
      renderWorkouts(data.workouts || []);
      renderMemory(data.memory || []);
      fillReflection(data.reflection);
    } catch (error) {
      statusDot.className = "status-dot error";
      showToast(`Auth/API: ${error.message}`, true);
    }
  }

  document.querySelectorAll(".gtg-btn").forEach((button) => {
    button.addEventListener("click", async () => {
      const reps = Number(button.dataset.reps);
      button.disabled = true;
      try {
        const data = await api("/api/gtg", { method: "POST", body: JSON.stringify({ reps }) });
        renderGTG(data.gtg);
        haptic("medium");
        showToast(`+${reps} GTG committed`);
      } catch (error) {
        showToast(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  });

  function addSetRow(prefill = {}) {
    const fragment = $("setRowTemplate").content.cloneNode(true);
    const row = fragment.querySelector(".set-row");
    row.querySelector(".exercise").value = prefill.exercise_name || "";
    row.querySelector(".weight").value = prefill.weight_kg ?? "";
    row.querySelector(".reps").value = prefill.reps ?? "";
    row.querySelector(".rir").value = prefill.rir ?? "";
    row.querySelector(".technique-ok").checked = prefill.technique_ok !== false;
    row.querySelector(".remove-set").addEventListener("click", () => row.remove());
    $("setRows").append(row);
  }

  $("addSetBtn").addEventListener("click", () => addSetRow());
  addSetRow();
  addSetRow();
  addSetRow();

  $("workoutForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const rows = [...document.querySelectorAll(".set-row")];
    const counters = {};
    const sets = rows.map((row) => {
      const exercise = row.querySelector(".exercise").value.trim();
      counters[exercise] = (counters[exercise] || 0) + 1;
      return {
        exercise_name: exercise,
        set_number: counters[exercise],
        weight_kg: row.querySelector(".weight").value || null,
        reps: row.querySelector(".reps").value || null,
        rir: row.querySelector(".rir").value || null,
        technique_ok: row.querySelector(".technique-ok").checked,
      };
    }).filter((item) => item.exercise_name);

    if (!sets.length) {
      showToast("Добавьте хотя бы один подход.", true);
      return;
    }

    const submit = event.submitter;
    submit.disabled = true;
    try {
      await api("/api/workouts", {
        method: "POST",
        body: JSON.stringify({
          title: $("workoutTitle").value.trim(),
          sets,
          notes: $("workoutNotes").value.trim(),
        }),
      });
      haptic("heavy");
      showToast("Workout committed to SQL");
      $("workoutNotes").value = "";
      await loadDashboard();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  $("reflectionForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = event.submitter;
    submit.disabled = true;
    try {
      await api("/api/reflection", {
        method: "POST",
        body: JSON.stringify({
          deep_work_hours: $("deepWorkHours").value || null,
          protein_hit: $("proteinHit").checked,
          calories_hit: $("caloriesHit").checked,
          rir_respected: $("rirRespected").checked,
          mood: $("mood").value || null,
          energy: $("energy").value || null,
          notes: $("reflectionNotes").value.trim(),
        }),
      });
      haptic("medium");
      showToast("Reflection committed");
      await loadDashboard();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  $("refreshBtn").addEventListener("click", loadDashboard);

  const initial = location.hash.replace("#", "");
  if (["dashboard", "workout", "reflection", "memory"].includes(initial)) setView(initial);
  loadDashboard();
})();
