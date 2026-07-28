/* Workshop behaviour.
 *
 * No framework, no build step: this has to keep working for as long as the camp does
 * without anyone running npm. Native <dialog> does the modals, so focus trapping, Escape
 * and the backdrop come from the platform rather than from code I would have to get right.
 *
 * Nothing here is fetched from the network except this pod's own root-relative routes.
 * The pod has no egress; an absolute URL is a hang, not a request.
 *
 * The shape of the thing: the terminal is full width at rest. One 1 Hz poll of /api/pulse
 * feeds one snapshot, and one render pass derives the Ribbon phrase, the drawer, the share
 * button and the badge from it. If /api/pulse is not there, every one of those falls back
 * to /api/state and the page still works — it just stops being clever.
 */

const $ = (id) => document.getElementById(id);
const api = {
  async get(p) { const r = await fetch(p, { cache: "no-store" }); return r.json(); },
  async post(p, b) {
    const r = await fetch(p, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(b || {}),
    });
    return { ok: r.ok, data: await r.json().catch(() => ({})) };
  },
};

const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");

/* Whole prompts, not vague ones. A vague ask gets a vague program and the child concludes
 * the tool is bad — the phrasing here is doing real work. Every one ends in
 * "One index.html." because the preview cannot reveal anything else, and this is the one
 * place that guarantee can be made. */
const IDEAS = [
  "Make a game where a unicorn runs and jumps over rocks. Put it all in one index.html.",
  "Make a drawing app with rainbow paint and a button to clear it. One index.html.",
  "Make a memory card game with animal emojis that I can actually play. One index.html.",
  "Make a quiz that asks me questions and tells me which dinosaur I am. One index.html.",
  "Make a maze I can walk through with the arrow keys. One index.html.",
  "Make a music toy where every key plays a different sound. One index.html.",
];

let STATE = { projects: [], project: "", models: [], has_index: false };

/* The whole client machine. One object so that "what does the UI think is true" is one
 * thing to read, not fifteen module-level lets. */
const M = {
  snap: null,           // last good /api/pulse payload
  snapAt: 0,            // when that payload arrived — every duration in it is measured from here
  havePulse: null,      // null = not asked yet, false = endpoint absent, true = live
  frozen: false,        // a poll failed: hold the last snapshot and stop acting on it
  fails: 0,
  degradedSince: 0,
  backoff: 1000,
  firstFailAt: 0,

  lastRevAt: 0,
  seenRev: -1,
  everBusy: false,
  everBuilt: false,
  busyTrueAt: 0,
  busyFalseAt: 0,

  armed: "none",        // "none" | "open"
  drawer: "closed",     // "closed" | "open" | "full"
  beforeFull: "open",
  dirty: false,
  revealedOnce: false,
  previewRetried: false,

  transient: null,      // { text, tone, until }
  phrase: "",
};

/* ------------------------------------------------------------------ storage
 *
 * Exactly three keys. Preferences persist; intents do not — "not right now" is a mood,
 * so it survives a reload in the same tab and dies with the tab. A drawer that flies open
 * thirty minutes later is indistinguishable from a bug. */

const K_REVEAL = "ws.workshop.reveal.v1";   // sessionStorage
const K_CURTAIN = "ws.workshop.curtain.v1"; // localStorage
const K_FILL = "ws.workshop.fill.v1";       // sessionStorage

function store(kind) {
  // Storage throws in a sandboxed or partitioned context. Losing a preference is fine;
  // taking the whole page down with it is not.
  try { return kind === "local" ? window.localStorage : window.sessionStorage; }
  catch { return null; }
}
function sGet(kind, k) { try { return store(kind)?.getItem(k) ?? null; } catch { return null; } }
function sSet(kind, k, v) { try { store(kind)?.setItem(k, v); } catch { /* ignore */ } }

function revealKey(project) { return `${project}|static`; }   // "static" is the only kind in v2
function readReveal() { try { return JSON.parse(sGet("session", K_REVEAL) || "{}"); } catch { return {}; } }
function isDismissed(project) { return readReveal()[revealKey(project)] === "dismissed"; }
function setIntent(project, value) {
  const o = readReveal();
  if (value) o[revealKey(project)] = value; else delete o[revealKey(project)];
  sSet("session", K_REVEAL, JSON.stringify(o));
}

/* ------------------------------------------------------------------ toast */

let toastTimer;
function toast(msg, ok = true) {
  const el = $("toast");
  el.textContent = msg;              // textContent, not innerHTML: nothing here is markup
  el.classList.toggle("bad", !ok);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ok ? 6000 : 10000);
}

/* ------------------------------------------------------------------ clipboard
 *
 * The workshop is served over plain HTTP at a bare IP, so window.isSecureContext is false
 * and navigator.clipboard is undefined. Step 2 is what actually runs at camp. Step 3 is
 * the guarantee that there is never a silent dead end. */

function copyDialog(text) {
  const d = $("dlg-copy"), ta = $("copy-text");
  ta.value = text;
  d.showModal();
  requestAnimationFrame(() => { ta.focus(); ta.select(); });
}

async function copyText(s) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(s);
      return true;
    }
  } catch { /* fall through */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = s;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:0;left:-9999px;opacity:0";
    // A modal <dialog> puts everything outside it in the inert top-layer backdrop, so a
    // textarea parented to <body> cannot take focus and .select() silently selects nothing
    // — execCommand then copies whatever was selected inside the dialog. Verified in
    // Chrome. Parent it to the topmost open dialog so the selection is real. This is the
    // path #share-copy takes, and on plain HTTP it is the ONLY path.
    const dialogs = document.querySelectorAll("dialog[open]");
    (dialogs[dialogs.length - 1] || document.body).appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, s.length);
    const ok = document.execCommand("copy") && ta.selectionEnd === s.length;
    ta.remove();
    if (ok) return true;
  } catch { /* fall through */ }
  copyDialog(s);
  return false;
}

const IS_MAC = /Mac|iP(hone|ad|od)/.test(navigator.platform || navigator.userAgent);
function pasteHint() {
  return IS_MAC
    ? "Copied. Click the dark box, then hold ⌘ and press V."
    : "Copied. Click the dark box, then hold Ctrl and press V.";
}

/* ------------------------------------------------------------------ terminal delivery
 *
 * Best effort only. ttyd is an opaque frame we do not control: if a build of it happens to
 * expose its input websocket on the frame's window, we can hand the sentence straight over
 * (ttyd's wire format is the byte '0' followed by the input). If anything at all is
 * missing, different, or throws — a cross-origin frame, a renamed field, a socket that is
 * not open — this returns false and the caller copies to the clipboard instead. It must
 * never throw and must never leave the child with nothing. */

function typeIntoTerminal(text) {
  try {
    const w = $("terminal-frame")?.contentWindow;
    if (!w) return false;
    if (!w.document) return false;                    // throws if the frame is cross-origin

    for (const name of Object.getOwnPropertyNames(w)) {
      let v;
      try { v = w[name]; } catch { continue; }
      const sock = pickSocket(w, v);
      if (sock) { sock.send("0" + text); return true; }
    }
  } catch { /* fall through to the clipboard */ }
  return false;
}

function pickSocket(w, v) {
  try {
    const WS = w.WebSocket;
    if (WS && v instanceof WS && v.readyState === 1 && /\/ws\b|token=/.test(String(v.url || ""))) return v;
    if (v && typeof v === "object" && !Array.isArray(v)) {
      for (const k of ["ws", "socket", "sock"]) {
        const c = v[k];
        if (WS && c instanceof WS && c.readyState === 1) return c;
      }
    }
  } catch { /* not it */ }
  return null;
}

async function sendIdea(text) {
  if (typeIntoTerminal(text)) { toast("Sent it to the agent — press Enter to go."); return; }
  if (await copyText(text)) toast(pasteHint());
}

/* ------------------------------------------------------------------ dialogs (unchanged) */

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

/* ------------------------------------------------------------------ facts */

function hasIndex() { return M.snap ? !!M.snap.has_index : !!STATE.has_index; }
function activeProject() { return (M.snap && M.snap.project) || STATE.project || ""; }

/* ------------------------------------------------------------------ preview */

let previewLoading = false;

function loadPreview() {
  // Cache-bust rather than reload(): a plain reload can come from cache, and a child who
  // sees no change concludes their edit did nothing.
  const u = new URL("/preview/", location.origin);
  u.searchParams.set("_", Date.now());
  previewLoading = true;
  $("drawer-progress").hidden = false;
  $("preview").src = u.toString();
}

$("preview").addEventListener("load", () => {
  if (!previewLoading) return;         // about:blank fires load too
  previewLoading = false;
  $("drawer-progress").hidden = true;
  syncEmpty();
});

function syncEmpty() {
  if (M.drawer === "closed") return;
  const empty = !hasIndex();
  $("preview-empty").hidden = !empty;
  // Never read contentDocument to decide this — the preview is sandboxed without
  // allow-same-origin and touching it throws. /api/pulse is the only source of truth.
  if (empty && !M.previewRetried && $("preview").src.includes("/preview/")) {
    M.previewRetried = true;
    setTimeout(() => { if (M.drawer !== "closed" && hasIndex()) loadPreview(); }, 1500);
  }
  if (!empty) M.previewRetried = false;
}

/* ------------------------------------------------------------------ drawer */

const stage = $("stage"), drawerEl = $("stage-preview");
drawerEl.setAttribute("inert", "");

function animDur(first) {
  if (REDUCED.matches) return 90;
  return first ? 420 : 220;
}

function openDrawer({ first = false } = {}) {
  if (M.drawer !== "closed") { syncChrome(); return; }
  M.drawer = sGet("session", K_FILL) === "1" ? "full" : "open";
  stage.dataset.drawer = M.drawer;
  drawerEl.removeAttribute("inert");
  drawerEl.classList.add("anim-in");
  if (first) drawerEl.classList.add("first");
  setTimeout(() => drawerEl.classList.remove("anim-in", "first"), animDur(first) + 60);
  M.previewRetried = false;
  loadPreview();
  syncChrome();
}

function closeDrawer() {
  if (M.drawer === "closed") return;
  drawerEl.classList.add("anim-out");
  const done = () => {
    drawerEl.classList.remove("anim-out");
    M.drawer = "closed";
    stage.dataset.drawer = "closed";
    drawerEl.setAttribute("inert", "");
    // Thirty children in one room, and a game that keeps playing its audio from behind a
    // closed drawer is a real problem.
    $("preview").src = "about:blank";
    syncChrome();
  };
  // A timer rather than animationend: if the animation is suppressed (reduced motion, a
  // background tab) animationend never fires and the drawer would be stuck half-shut.
  setTimeout(done, REDUCED.matches ? 0 : 140);
}

function syncChrome() {
  const open = M.drawer !== "closed";
  $("btn-look").setAttribute("aria-expanded", String(open));
  $("btn-fill").hidden = !open;
  $("btn-fill").querySelector(".label").textContent =
    M.drawer === "full" ? "Back to building" : "Fill the screen";
  const badge = M.snap && M.drawer === "closed" && M.everBuilt &&
                (M.armed === "open" || M.snap.rev > M.seenRev);
  $("look-badge").hidden = !badge;
  if (open) syncEmpty();
}

$("btn-look").addEventListener("click", () => {
  if (M.drawer !== "closed") {
    // Same as Hide: shutting the drawer ends "Fill the screen". Without this the flag
    // survives, and openDrawer() reads it on the NEXT reveal edge — so an auto-open the
    // child did not ask for arrives full screen with the terminal at zero width, mid
    // sentence. §4 says a reveal edge opens to "open", never "full".
    sSet("session", K_FILL, "0");
    closeDrawer();
    return;
  }
  setIntent(activeProject(), null);        // opening manually clears any dismissal
  M.armed = "none";
  if (M.snap) M.seenRev = M.snap.rev;
  openDrawer({ first: !M.revealedOnce });
  M.revealedOnce = true;
});

$("btn-hide").addEventListener("click", () => {
  setIntent(activeProject(), "dismissed");  // no TTL, no timer: only the four listed things clear it
  if (M.snap) M.seenRev = M.snap.rev;
  M.armed = "none";
  sSet("session", K_FILL, "0");
  closeDrawer();
});

$("btn-fill").addEventListener("click", () => {
  if (M.drawer === "full") {
    M.drawer = M.beforeFull === "closed" ? "open" : M.beforeFull;
    sSet("session", K_FILL, "0");
  } else {
    M.beforeFull = M.drawer;
    M.drawer = "full";
    sSet("session", K_FILL, "1");
  }
  stage.dataset.drawer = M.drawer;
  syncChrome();
});

// User intent always overrides the machine: this reloads now, whatever the settle gate says.
$("btn-reload").addEventListener("click", () => { M.dirty = false; loadPreview(); });
$("btn-popout").addEventListener("click", () => window.open("/preview/", "_blank", "noopener"));
$("empty-ideas").addEventListener("click", () => openStuck());

/* ------------------------------------------------------------------ settle gate */

function settled(now) {
  const s = M.snap;
  if (!s) return false;
  if (now - M.lastRevAt < 1200) return false;
  if (s.busy === true) return false;
  if (s.busy === false && now - M.busyFalseAt < 600) return false;
  return true;
}

// Keyed on revision quiescence, not elapsed time, so it can only fire once writing has
// demonstrably stopped. A "stale ceiling" that reloads mid-write was rejected: a broken
// page reads as "the tool is broken", a not-yet-updated page does not.
function watchdog(now) {
  const s = M.snap;
  return !!s && s.busy === true && (now - M.busyTrueAt >= 90000) && (now - M.lastRevAt >= 10000);
}

function go(now) { return settled(now) || watchdog(now); }

function evaluateGate() {
  if (!M.snap || M.frozen) return;
  const now = Date.now();
  if (!go(now)) return;

  if (M.armed === "open") {
    // Focus never moves on auto-open; the child is mid-sentence. Queue it while a modal is
    // up or the tab is hidden, and run it on release.
    if (document.querySelector("dialog[open]") || document.hidden) return;
    M.armed = "none";
    M.seenRev = M.snap.rev;
    openDrawer({ first: !M.revealedOnce });
    M.revealedOnce = true;
    return;
  }
  if (M.dirty && M.drawer !== "closed") {
    if (document.visibilityState !== "visible") return;   // exactly one reload on return
    M.dirty = false;
    loadPreview();
  }
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) evaluateGate(); });

/* ------------------------------------------------------------------ ribbon */

function fmtElapsed(ms) {
  const t = Math.max(0, Math.floor(ms / 1000));
  return t < 60 ? `${t}s` : `${Math.floor(t / 60)}m ${t % 60}s`;
}

/* Every duration in a pulse was measured when the server sampled it, and the snapshot is
 * up to a second old before it lands plus a second before the next one does. Read raw, the
 * 4 s "Writing…" window never closes and the elapsed counter ticks in lumps. Age them
 * against arrival time instead — and while the transport is degraded this keeps counting,
 * which is the honest reading: the file really is getting older. */
function aged(ms, now) { return (typeof ms === "number" ? ms : 0) + (now - M.snapAt); }

function transient(text, tone) { M.transient = { text, tone, until: Date.now() + 6000 }; renderRibbon(); }

// Top to bottom, first match wins. The Ribbon never invents activity it cannot observe:
// when busy is null there is no fabricated percentage and no fabricated "thinking".
function derivePhrase(now) {
  if (M.degradedSince && now - M.degradedSince >= 15000) return ["Reconnecting…", "warn"];
  if (M.transient && now < M.transient.until) return [M.transient.text, M.transient.tone];

  const s = M.snap;
  if (!s) {
    return STATE.has_index
      ? ["Your turn — type what you want next.", "grey"]
      : ["Click the dark box and type what you want to make.", "grey"];
  }

  const changed = s.changed || [];
  const writing = changed.length > 0 && aged(s.last_change_ms, now) < 4000;
  const busyMs = aged(s.busy_ms, now);

  if (s.busy === true) {
    if (writing && changed.length === 1) return [`Writing ${changed[0]}…`, "amber"];
    if (writing) return [`Writing ${changed.length} files…`, "amber"];
    if (busyMs >= 120000) return ["This is a big one. It's still going.", "amber"];
    if (busyMs >= 45000) return ["Still thinking. Good ones take a minute.", "amber"];
    return ["Thinking about it…", "amber"];
  }
  if (s.busy === null && writing) {
    return changed.length === 1
      ? [`Writing ${changed[0]}…`, "amber"]
      : [`Writing ${changed.length} files…`, "amber"];
  }
  if (s.offline_refs > 0 && s.has_index)
    return ["That page needs the internet, and this room has none. Press Stuck?", "warn"];
  if (M.drawer === "closed" && s.rev > M.seenRev && M.everBuilt)
    return ["There's a new version — press Look.", "green"];
  if (s.has_index && now - M.lastRevAt < 10000) return ["Made it — take a look.", "green"];
  if (s.has_index) return ["Your turn — type what you want next.", "grey"];
  if (M.everBusy) return ["It finished. There's nothing to look at yet.", "grey"];
  return ["Click the dark box and type what you want to make.", "grey"];
}

function renderRibbon() {
  const now = Date.now();
  const [text, tone] = derivePhrase(now);
  // Only write when it changes: #ribbon-phrase is the one live region on the page, and
  // rewriting it every second would make a screen reader unusable.
  if (text !== M.phrase) {
    M.phrase = text;
    $("ribbon-phrase").textContent = text;
  }
  $("ribbon-dot").dataset.tone = tone;

  const s = M.snap;
  // aria-hidden on this span, so the elapsed counter is never announced.
  $("ribbon-elapsed").textContent = (s && s.busy === true) ? fmtElapsed(aged(s.busy_ms, now)) : "";
}

/* ------------------------------------------------------------------ pulse */

function projectSwitchReset(s, firstEver) {
  M.armed = "none";
  M.lastRevAt = Date.now();
  M.seenRev = s.rev;
  M.dirty = false;
  M.everBuilt = !!s.has_index;
  M.revealedOnce = false;

  if (firstEver) {
    // Initial page load with something already built counts as a reveal edge: arm it and
    // let the settle gate decide when, rather than opening onto a half-written file.
    if (s.has_index && !isDismissed(s.project)) M.armed = "open";
    return;
  }
  if (s.has_index && !isDismissed(s.project)) {
    if (M.drawer === "closed") openDrawer({ first: true }); else loadPreview();
  } else {
    closeDrawer();
  }
}

function applyPulse(s) {
  const now = Date.now();
  const prev = M.snap;
  M.snapAt = now;

  if (prev && s.project === prev.project) {
    if (s.rev !== prev.rev) {
      if (s.rev < prev.rev) showStuckBar();     // the server restarted under us
      M.lastRevAt = now;
      if (M.drawer !== "closed") M.dirty = true;
    }
    if (s.busy === true && prev.busy !== true) M.busyTrueAt = now;
    if (s.busy === false && prev.busy !== false) M.busyFalseAt = now;
    if (s.has_index) M.everBuilt = true;

    // Reveal edge. Fires once per key: making index.html opens the drawer, forty rewrites
    // of it do not.
    if (s.has_index === true && prev.has_index === false && !isDismissed(s.project)) {
      M.armed = "open";
    }
    // They emptied the project — that resets the intent, so the next build reveals again.
    if (s.has_index === false && prev.has_index === true) setIntent(s.project, null);
  } else {
    M.snap = s;                       // projectSwitchReset reads the new snapshot
    projectSwitchReset(s, !prev);
    if (s.busy === true) M.busyTrueAt = now;
    if (s.busy === false) M.busyFalseAt = now;
  }

  if (s.busy === true) M.everBusy = true;
  M.snap = s;
  M.frozen = false;

  if (!$("curtain").hidden && s.busy === true) hideCurtain();  // they started without reading
  syncShare();
  syncChrome();
  renderRibbon();
  evaluateGate();
}

function showStuckBar() { $("bar-stuck").hidden = false; }

/* Ten consecutive failures OR forty seconds of silence, whichever lands first. Both halves
 * are needed: at the capped 8 s backoff the tenth failure alone is nearly a minute away,
 * and this must be evaluated on the ticker rather than only when a poll fails — otherwise
 * the deadline is only ever checked every 8 s and the bar shows up late. */
function syncFailBars() {
  if (!M.degradedSince) return;
  if (M.fails >= 10 || Date.now() - M.degradedSince >= 40000) $("bar-lost").hidden = false;
}

function onPollFailure() {
  M.fails += 1;
  M.frozen = true;                                    // acting on stale state is worse than not acting
  if (!M.degradedSince) M.degradedSince = Date.now();
  syncFailBars();
  const wait = M.backoff;
  M.backoff = Math.min(8000, M.backoff * 2);
  renderRibbon();
  return wait;
}

async function pollPulse() {
  let scheduled = M.backoff;
  try {
    const r = await fetch("/api/pulse", { cache: "no-store" });
    if (r.status === 404 && M.havePulse === null) {
      // The endpoint is not there at all. Degrade permanently and silently: Ribbon from
      // /api/state only, no auto-reveal, drawer by button. The UI must not break because
      // pulse is missing.
      M.havePulse = false;
      renderRibbon();
      return;                                          // and stop polling
    }
    const ct = r.headers.get("content-type") || "";
    if (!r.ok || !ct.includes("json")) throw new Error("bad pulse");
    const data = await r.json();
    if (!data || typeof data !== "object" || typeof data.rev !== "number") throw new Error("bad pulse");

    const wasDownFor = M.degradedSince ? Date.now() - M.degradedSince : 0;
    if (wasDownFor >= 5000) showStuckBar();
    M.havePulse = true;
    M.fails = 0;
    M.degradedSince = 0;
    M.backoff = 1000;
    $("bar-lost").hidden = true;
    applyPulse(data);
    scheduled = 1000;
  } catch {
    scheduled = onPollFailure();
  }
  setTimeout(pollPulse, scheduled);
}

// One 500 ms ticker: it drives the settle gate while armed or dirty, the elapsed counter,
// and the degraded phrase. Cheap enough to just always run.
setInterval(() => { renderRibbon(); syncFailBars(); evaluateGate(); }, 500);

$("btn-reload-page").addEventListener("click", () => location.reload());
$("btn-restart-term").addEventListener("click", restartAgent);

/* ------------------------------------------------------------------ curtain */

function hideCurtain() {
  if ($("curtain").hidden) return;
  $("curtain").hidden = true;
  sSet("local", K_CURTAIN, "seen");
}
$("curtain-go").addEventListener("click", hideCurtain);
$("curtain").addEventListener("click", (e) => { if (e.target === $("curtain")) hideCurtain(); });

for (const idea of IDEAS.slice(0, 3)) {
  const li = document.createElement("li");
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = idea;
  b.addEventListener("click", async () => { hideCurtain(); await sendIdea(idea); });
  li.appendChild(b);
  $("curtain-ideas").appendChild(li);
}

if (sGet("local", K_CURTAIN) !== "seen") $("curtain").hidden = false;

/* ------------------------------------------------------------------ Stuck? */

for (const idea of IDEAS) {
  const li = document.createElement("li");
  const t = document.createElement("span");
  t.className = "idea-text";
  t.textContent = idea;
  const b = document.createElement("button");
  b.type = "button";
  b.className = "btn small";
  b.textContent = "Copy";
  b.addEventListener("click", async () => { $("dlg-stuck").close(); await sendIdea(idea); });
  li.append(t, b);
  $("stuck-ideas").appendChild(li);
}

for (const b of document.querySelectorAll("#dlg-stuck [data-copy]")) {
  b.addEventListener("click", async () => {
    const text = b.dataset.copy;
    $("dlg-stuck").close();
    await sendIdea(text);
  });
}

function openStuck(reason) {
  const s = M.snap;
  let pick = 8;
  if (s && s.offline_refs > 0) pick = 3;
  else if (!hasIndex() && M.everBusy) pick = 1;
  else if (s && s.busy === true && s.busy_ms > 120000) pick = 6;
  else if (reason === "share-failed") pick = 1;

  $("stuck-lead").hidden = reason !== "share-failed";
  for (const d of document.querySelectorAll("#dlg-stuck details")) {
    d.open = d.dataset.stuck === String(pick);
  }
  $("dlg-stuck").showModal();
}
$("btn-stuck").addEventListener("click", () => openStuck());

async function restartAgent() {
  $("dlg-stuck").close();
  if (!await confirmDialog({
    title: "Restart the agent?",
    body: "Your files stay exactly as they are. The conversation you have had so far is forgotten, so you may need to say what you want again.",
    danger: "Restart it",
  })) return;
  // ttyd spawns a fresh shell per websocket, so reloading the frame IS the restart.
  $("terminal-frame").src = "/terminal/";
  $("bar-stuck").hidden = true;
  transient("Fresh start. Type what you want to make.", "grey");
}
$("stuck-restart").addEventListener("click", restartAgent);

/* ------------------------------------------------------------------ projects */

/* Switching only writes one file and reloads two frames: ttyd spawns a fresh shell per
 * websocket, so reloading the terminal lands in the new directory by itself. */
function reloadPanes() {
  $("terminal-frame").src = "/terminal/";
  $("preview").src = "about:blank";
  if (M.havePulse !== true) {
    // No pulse to resolve the switch for us, so decide from /api/state right now.
    if (STATE.has_index && !isDismissed(STATE.project)) openDrawer({ first: true });
    else closeDrawer();
  }
}

function renderMenu() {
  const m = $("project-menu");
  m.innerHTML = "";
  for (const p of STATE.projects) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "option");
    b.setAttribute("aria-selected", String(p.name === STATE.project));
    const dot = document.createElement("span");
    dot.className = "chip-dot" + (p.published ? " shared" : "");
    const nm = document.createElement("span");
    nm.textContent = p.name;
    b.append(dot, nm);
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
  nb.type = "button";
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
  $("dlg-settings").close();
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
  $("dlg-settings").close();
  if (!await confirmDialog({
    title: `Delete "${name}"?`,
    body: "The project and its share link both go, for good. Anyone holding the link will see nothing.",
    danger: "Delete it",
  })) return;
  const { ok, data } = await api.post("/api/delete", { name });
  if (ok) { await load(); reloadPanes(); }
  toast(data.message, ok);
});

/* ------------------------------------------------------------------ settings */

$("btn-settings").addEventListener("click", () => $("dlg-settings").showModal());

/* ------------------------------------------------------------------ state */

async function load() {
  let s;
  try { s = await api.get("/api/state"); }
  catch { $("bar-lost").hidden = false; return; }
  STATE = s;

  $("project-name").textContent = s.project;
  $("project-dot").className = "chip-dot" + (s.published ? " shared" : "");
  renderMenu();

  const radios = $("model-radios");
  radios.innerHTML = "";
  for (const m of s.models) {
    const [name, sub] = m.label.split("—").map((x) => x.trim());
    const l = document.createElement("label");
    const input = document.createElement("input");
    input.type = "radio"; input.name = "model"; input.value = m.id; input.checked = m.id === s.model;
    const span = document.createElement("span");
    span.textContent = name;
    const subEl = document.createElement("span");
    subEl.className = "sub";
    subEl.textContent = sub || "";
    span.appendChild(subEl);
    l.append(input, span);
    input.addEventListener("change", async (e) => {
      const { ok, data } = await api.post("/api/model", { model: e.target.value });
      toast(data.message || "Saved.", ok);
      if (ok) await load();
    });
    radios.appendChild(l);
  }

  $("project-note").textContent = s.has_index
    ? `"${s.project}" has an index.html, so it is ready to share.`
    : `"${s.project}" has no index.html yet — ask the agent for one.`;

  syncShare();
  renderRibbon();
}

/* ------------------------------------------------------------------ share */

function shareUrl() { return STATE.published_url || ""; }

function syncShare() {
  const btn = $("btn-share"), label = btn.querySelector(".label");
  const s = M.snap;
  const published = s ? !!s.published : !!STATE.published;
  let text = "Show someone", live = false;

  if (published) {
    // Without pulse there is no fingerprint to compare, so offer the action that is always
    // safe: republishing a current project is a no-op, showing a stale link is not.
    const current = !!(s && s.fp && s.published_fp && s.fp === s.published_fp);
    text = current ? "Live" : "Update the link";
    live = current;
  }
  if (label.textContent !== text) label.textContent = text;
  btn.classList.toggle("live", live);
  btn.classList.toggle("primary", !live);
}

function showShare(url) {
  $("share-ok").hidden = false;
  $("share-err").hidden = true;
  $("share-url").value = url;
  const a = $("share-link");
  a.href = url; a.textContent = url;
  $("dlg-share").showModal();
}

function showShareError(message) {
  $("share-ok").hidden = true;
  $("share-err").hidden = false;
  $("share-err-out").textContent = message || "Could not share.";
  $("dlg-share").showModal();
}

$("share-done").addEventListener("click", () => $("dlg-share").close());
$("share-err-close").addEventListener("click", () => $("dlg-share").close());
$("share-open").addEventListener("click", () => {
  const v = $("share-url").value;
  if (v.startsWith("http")) window.open(v, "_blank", "noopener");
});
$("share-copy").addEventListener("click", async () => {
  const btn = $("share-copy"), label = btn.querySelector(".label");
  if (await copyText($("share-url").value)) {
    label.textContent = "Copied ✓";
    setTimeout(() => { label.textContent = "Copy link"; }, 1600);
  }
});

$("btn-share").addEventListener("click", async () => {
  const btn = $("btn-share"), label = btn.querySelector(".label");

  // Live and current: this opens the link, it does not republish.
  if (btn.classList.contains("live")) {
    const v = shareUrl();
    if (v.startsWith("http")) { window.open(v, "_blank", "noopener"); return; }
  }
  if (!hasIndex()) { openStuck("share-failed"); return; }

  const restore = label.textContent;
  btn.disabled = true;
  label.textContent = "Sharing…";
  try {
    const { data } = await api.post("/api/publish");
    if (data.ok && data.url) {
      await load();
      showShare(data.url);
      transient("It's live. Anyone with the link can see it.", "green");
    } else {
      // publish explains its own refusals (no index.html, too big) better than a second
      // copy of that logic here would.
      showShareError(data.message);
    }
  } catch {
    showShareError("Could not reach the workshop.");
  } finally {
    btn.disabled = false;
    label.textContent = restore;
    syncShare();
  }
});

/* ------------------------------------------------------------------ keys
 *
 * Exactly two global keys, and both have a visible button. Everything else belongs to the
 * terminal: opencode is a TUI that uses Escape to interrupt, Ctrl+C, Ctrl+L and every
 * letter, so a single-letter global next to it is a defect class, not a shortcut. */

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (document.querySelector("dialog[open]")) return;   // the platform closes its own
    let closed = false;
    if (!$("project-menu").hidden) { toggleMenu(false); closed = true; }
    else if (!$("curtain").hidden) { hideCurtain(); closed = true; }
    if (closed) e.preventDefault();       // Escape never closes the drawer
    return;
  }

  // Key events raised inside the ttyd document never cross the boundary, but this covers
  // the window where the iframe ELEMENT holds focus before its inner document does. It is
  // what keeps the terminal usable. Do not build more on top of it.
  const el = document.activeElement;
  if (e.isComposing || e.keyCode === 229) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (["INPUT", "TEXTAREA", "SELECT", "IFRAME"].includes(el?.tagName)) return;
  if (el?.isContentEditable) return;
  if (document.querySelector("dialog[open]")) return;

  if (e.key === "?") { openStuck(); e.preventDefault(); }
});

/* ------------------------------------------------------------------ boot */

syncChrome();
renderRibbon();
load();
pollPulse();
