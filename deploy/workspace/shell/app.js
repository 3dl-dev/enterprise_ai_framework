/* Workshop behaviour.
 *
 * No framework, no build step: this has to keep working for as long as the camp does
 * without anyone running npm. Native <dialog> does the modals, so focus trapping, Escape
 * and the backdrop come from the platform rather than from code I would have to get right.
 */

const $ = (id) => document.getElementById(id);
const api = {
  async get(p) { const r = await fetch(p); return r.json(); },
  async post(p, b) {
    const r = await fetch(p, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(b || {}),
    });
    return { ok: r.ok, data: await r.json().catch(() => ({})) };
  },
};

let STATE = { projects: [], project: "", models: [] };

/* Whole prompts, not vague ones. A vague ask gets a vague program and the child concludes
 * the tool is bad — the phrasing here is doing real work. */
const IDEAS = [
  "Make a game where a unicorn runs and jumps over rocks. Put it all in index.html.",
  "Make a drawing app where I paint with rainbow colours, and a button to clear it.",
  "Make a memory card game with animal emojis that I can actually play.",
  "Make a quiz that asks me questions and tells me which dinosaur I am.",
  "Make a maze I can walk through with the arrow keys.",
  "Make a music toy where every key plays a different sound.",
];

/* ------------------------------------------------------------------ toast */

let toastTimer;
function toast(msg, ok = true) {
  const el = $("toast");
  el.innerHTML = msg;
  el.classList.toggle("bad", !ok);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ok ? 8000 : 12000);
}

/* ------------------------------------------------------------------ dialogs */

function confirmDialog({ title, body, danger = "Yes, do it" }) {
  return new Promise((resolve) => {
    const d = $("dlg-confirm");
    $("confirm-title").textContent = title;
    $("confirm-body").textContent = body;
    $("confirm-go").textContent = danger;
    d.returnValue = "cancel";
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true });
    d.showModal();
  });
}

function newProjectDialog() {
  return new Promise((resolve) => {
    const d = $("dlg-new"), input = $("new-name"), err = $("new-err");
    input.value = ""; err.hidden = true;
    d.returnValue = "cancel";
    d.addEventListener("close", () => resolve(d.returnValue === "ok" ? input.value.trim() : null), { once: true });
    d.showModal();
    // Native autofocus fires before the dialog is laid out in some browsers.
    requestAnimationFrame(() => input.focus());
  });
}

/* ------------------------------------------------------------------ preview */

function refreshPreview() {
  // Cache-bust rather than reload(): a plain reload can come from cache, and a child who
  // sees no change concludes their edit did nothing.
  const u = new URL("/preview/", location.origin);
  u.searchParams.set("_", Date.now());
  $("preview").src = u.toString();
}

function setEmpty(isEmpty) {
  $("preview-empty").hidden = !isEmpty;
  $("preview").style.visibility = isEmpty ? "hidden" : "visible";
}

$("refresh").addEventListener("click", refreshPreview);
$("popout").addEventListener("click", () => window.open("/preview/", "_blank", "noopener"));
$("empty-ideas").addEventListener("click", () => openPanel("guide"));

let autoTimer;
$("autorefresh").addEventListener("change", (e) => {
  clearInterval(autoTimer);
  if (e.target.checked) {
    // 3s: quick enough to feel live while the agent writes, slow enough that catching a
    // half-written file is unlikely.
    autoTimer = setInterval(refreshPreview, 3000);
    toast("Reloading every few seconds while you build.");
  }
});

/* ------------------------------------------------------------------ splitter */

(function splitter() {
  const grid = $("grid"), bar = $("splitter");
  const vertical = () => window.matchMedia("(max-width: 900px)").matches;
  let pct = Number(localStorage.getItem("ws.split") || 50);

  const apply = () => {
    const p = Math.min(80, Math.max(20, pct));
    grid.style[vertical() ? "gridTemplateRows" : "gridTemplateColumns"] = `${p}fr 6px ${100 - p}fr`;
    grid.style[vertical() ? "gridTemplateColumns" : "gridTemplateRows"] = "";
  };
  apply();
  window.addEventListener("resize", apply);

  const move = (e) => {
    const r = grid.getBoundingClientRect();
    pct = vertical()
      ? ((e.clientY - r.top) / r.height) * 100
      : ((e.clientX - r.left) / r.width) * 100;
    apply();
  };
  const stop = () => {
    document.body.classList.remove("resizing");
    bar.classList.remove("dragging");
    localStorage.setItem("ws.split", String(Math.round(pct)));
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", stop);
  };
  bar.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    document.body.classList.add("resizing");
    bar.classList.add("dragging");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  });
  bar.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 10 : 3;
    if (e.key === "ArrowLeft" || e.key === "ArrowUp") { pct -= step; apply(); e.preventDefault(); }
    if (e.key === "ArrowRight" || e.key === "ArrowDown") { pct += step; apply(); e.preventDefault(); }
    if (e.key === "Enter") { pct = 50; apply(); }
    localStorage.setItem("ws.split", String(Math.round(pct)));
  });
})();

/* ------------------------------------------------------------------ panel */

function openPanel(which) {
  $("panel").hidden = false;
  $("panel").setAttribute("aria-hidden", "false");
  $("scrim").hidden = false;
  $("panel-guide").hidden = which !== "guide";
  $("panel-settings").hidden = which !== "settings";
  $("tab-guide").setAttribute("aria-pressed", String(which === "guide"));
  $("tab-settings").setAttribute("aria-pressed", String(which === "settings"));
}
function closePanel() {
  $("panel").hidden = true;
  $("panel").setAttribute("aria-hidden", "true");
  $("scrim").hidden = true;
  $("tab-guide").setAttribute("aria-pressed", "false");
  $("tab-settings").setAttribute("aria-pressed", "false");
}
$("tab-guide").addEventListener("click", () => $("panel").hidden ? openPanel("guide") : ($("panel-guide").hidden ? openPanel("guide") : closePanel()));
$("tab-settings").addEventListener("click", () => $("panel").hidden ? openPanel("settings") : ($("panel-settings").hidden ? openPanel("settings") : closePanel()));
$("panel-close").addEventListener("click", closePanel);
$("scrim").addEventListener("click", closePanel);

/* ------------------------------------------------------------------ ideas */

for (const idea of IDEAS) {
  const li = document.createElement("li");
  const b = document.createElement("button");
  b.textContent = idea;
  b.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(idea);
      toast("Copied — click the dark panel on the left and paste it in.");
      closePanel();
    } catch {
      toast("Could not copy. Select the text and copy it by hand.", false);
    }
  });
  li.appendChild(b);
  $("ideas").appendChild(li);
}

/* ------------------------------------------------------------------ projects */

/* Switching only writes one file and reloads two frames: ttyd spawns a fresh shell per
 * websocket, so reloading the terminal lands in the new directory by itself. */
function reloadPanes() {
  $("terminal").src = "/terminal/";
  refreshPreview();
}

function renderMenu() {
  const m = $("project-menu");
  m.innerHTML = "";
  for (const p of STATE.projects) {
    const b = document.createElement("button");
    b.setAttribute("role", "option");
    b.setAttribute("aria-selected", String(p.name === STATE.project));
    b.innerHTML = `<span class="chip-dot ${p.published ? "shared" : ""}"></span><span>${p.name}</span>`;
    b.addEventListener("click", async () => {
      toggleMenu(false);
      if (p.name === STATE.project) return;
      const { ok, data } = await api.post("/api/switch", { name: p.name });
      if (ok) { await load(); reloadPanes(); toast(data.message); } else toast(data.message, false);
    });
    m.appendChild(b);
  }
  m.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
  const nb = document.createElement("button");
  nb.className = "new";
  nb.textContent = "+  Start something new";
  nb.addEventListener("click", async () => { toggleMenu(false); await createProject(); });
  m.appendChild(nb);
}

function toggleMenu(show) {
  const open = show ?? $("project-menu").hidden;
  $("project-menu").hidden = !open;
  $("project-button").setAttribute("aria-expanded", String(open));
}
$("project-button").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(); });
document.addEventListener("click", () => toggleMenu(false));
$("project-menu").addEventListener("click", (e) => e.stopPropagation());

async function createProject() {
  const name = await newProjectDialog();
  if (!name) return;
  const { ok, data } = await api.post("/api/projects", { name });
  if (!ok) { toast(data.message || "Could not make it.", false); return; }
  await load(); reloadPanes(); toast(data.message);
}

$("reset-project").addEventListener("click", async () => {
  if (!await confirmDialog({
    title: `Start "${STATE.project}" over?`,
    body: "This empties the project. Every earlier version stays in its history, so nothing is truly lost.",
    danger: "Empty it",
  })) return;
  const { ok, data } = await api.post("/api/reset", {});
  if (ok) { await load(); reloadPanes(); }
  toast(data.message, ok);
});

$("delete-project").addEventListener("click", async () => {
  const name = STATE.project;
  if (!await confirmDialog({
    title: `Delete "${name}"?`,
    body: "The project and its share link both go, for good. Anyone holding the link will see nothing.",
    danger: "Delete it",
  })) return;
  const { ok, data } = await api.post("/api/delete", { name });
  if (ok) { await load(); reloadPanes(); }
  toast(data.message, ok);
});

/* ------------------------------------------------------------------ state */

async function load() {
  let s;
  try { s = await api.get("/api/state"); }
  catch { toast("Lost contact with the workspace. Reload the page.", false); return; }
  STATE = s;

  $("project-name").textContent = s.project;
  $("project-dot").className = "chip-dot" + (s.published ? " shared" : "");
  renderMenu();

  const model = s.models.find((m) => m.id === s.model);
  $("agent-state").textContent = model ? model.label.split("—")[0].trim() : s.model;

  const radios = $("model-radios");
  radios.innerHTML = "";
  for (const m of s.models) {
    const [name, sub] = m.label.split("—").map((x) => x.trim());
    const l = document.createElement("label");
    l.innerHTML = `<input type="radio" name="model" value="${m.id}" ${m.id === s.model ? "checked" : ""}>
                   <span>${name}<span class="sub">${sub || ""}</span></span>`;
    l.querySelector("input").addEventListener("change", async (e) => {
      const { ok, data } = await api.post("/api/model", { model: e.target.value });
      toast(data.message || "Saved.", ok);
      if (ok) await load();
    });
    radios.appendChild(l);
  }

  $("project-note").textContent = s.has_index
    ? `"${s.project}" has an index.html, so it is ready to share.`
    : `"${s.project}" has no index.html yet — ask the agent for one.`;

  $("published-url").value = s.published_url || "not shared yet";
  $("published-state").textContent = s.published
    ? "Live. Anyone with this link can open it — no login."
    : "Not shared yet. Press “Share it” and you get a link.";
  $("share-card").classList.toggle("live", !!s.published);
  $("open-url").disabled = !s.published;

  setEmpty(!s.has_index);
}

$("copy-url").addEventListener("click", async () => {
  const v = $("published-url").value;
  if (!v.startsWith("http")) { toast("Share it first.", false); return; }
  try { await navigator.clipboard.writeText(v); toast("Link copied."); }
  catch { toast("Could not copy — select it and copy by hand.", false); }
});
$("open-url").addEventListener("click", () => {
  const v = $("published-url").value;
  if (v.startsWith("http")) window.open(v, "_blank", "noopener");
});

/* ------------------------------------------------------------------ publish */

$("publish").addEventListener("click", async () => {
  const btn = $("publish");
  btn.disabled = true; btn.classList.add("busy");
  btn.querySelector(".label").textContent = "Sharing…";
  try {
    const { data } = await api.post("/api/publish");
    if (data.ok && data.url) {
      toast(`Live at <a href="${data.url}" target="_blank" rel="noopener">${data.url}</a> — send that to anyone.`);
      await load();
      openPanel("settings");
    } else {
      // publish explains its own refusals (no index.html, too big) better than a second
      // copy of that logic here would.
      toast(data.message || "Could not share.", false);
    }
  } catch {
    toast("Could not reach the workspace.", false);
  } finally {
    btn.disabled = false; btn.classList.remove("busy");
    btn.querySelector(".label").textContent = "Share it";
  }
});

/* ------------------------------------------------------------------ keys */

document.addEventListener("keydown", (e) => {
  // Never steal a key while the child is typing to the agent or naming a project.
  const typing = ["INPUT", "TEXTAREA", "IFRAME"].includes(document.activeElement?.tagName);
  if (e.key === "Escape") { toggleMenu(false); closePanel(); return; }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "?") { openPanel("guide"); e.preventDefault(); }
  if (e.key.toLowerCase() === "r") { refreshPreview(); }
});

load();
