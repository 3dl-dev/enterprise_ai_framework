/* The portal.
 *
 * No framework and no build step, same as the Workshop shell: this is the page a person
 * lands on to find everything else, and it must keep working with no toolchain alive.
 *
 * Every panel loads independently. One failing endpoint degrades its own card and leaves
 * the rest of the page usable — losing the spend query must never cost somebody the link
 * to their own work.
 */

const $ = (id) => document.getElementById(id);

async function get(path) {
  const r = await fetch(path, { cache: "no-store", headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return { ok: r.ok, data: await r.json().catch(() => ({})) };
}

let toastTimer;
function toast(msg, ok = true) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.toggle("bad", !ok);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ok ? 6000 : 10000);
}

/* Money, at the scale this actually operates. Sub-cent totals are the normal case for a
 * single user, and rounding them to "$0.00" makes a working meter look broken. */
function money(n) {
  const v = Number(n || 0);
  if (v === 0) return "$0.00";
  if (v < 0.01) return "$" + v.toFixed(5).replace(/0+$/, "").replace(/\.$/, ".0");
  if (v < 1) return "$" + v.toFixed(4);
  // Grouped above a thousand. A budget sentinel rendered "cap $1000000.00", which is
  // a number nobody can read at a glance and looks like a bug rather than a cap.
  return "$" + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function compact(n) {
  const v = Number(n || 0);
  if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "k";
  return String(v);
}

function sinceParam(id = "since") {
  const el = $(id);
  const v = el ? el.value : "";
  if (!v) return "";
  const now = Date.now();
  const ms = { "24h": 864e5, "7d": 6048e5, "30d": 2592e6 }[v];
  return "?since=" + new Date(now - ms).toISOString();
}

/* ---------------------------------------------------------------- identity + links */

async function loadMe() {
  const me = await get("/portal/api/me");
  $("username").textContent = me.username;
  $("email").textContent = me.email || "";
  $("avatar").textContent = (me.username || "?").slice(0, 1).toUpperCase();
  document.title = `${me.username} — Enterprise AI`;

  const L = me.links || {};
  wire("link-password", L.password || L.account);
  wire("link-account", L.account);
  wire("mi-account", L.password || L.account);
  wire("mi-published", L.published);
  if (L.signout) $("signout").href = L.signout;
  LINKS = L;
  IS_ADMIN = me.is_admin === true;
  // Operator-only entry to the behavioural-analytics report. Hidden for everyone else, who
  // gets a 404 from the endpoint anyway — no point advertising a door they can't open.
  if (IS_ADMIN) { const mi = $("mi-analytics"); if (mi) mi.hidden = false; }
  mountFrames();
  return me;
}

function wire(id, href) {
  const el = $(id);
  if (!el) return;
  if (href) el.href = href;
  else { el.classList.add("disabled"); el.removeAttribute("href"); }
}

/* ---------------------------------------------------------------- tabs

   The framed surfaces stay mounted once visited. Switching tabs only flips visibility,
   because reloading chat every time somebody glances at their code would throw away the
   conversation they were in the middle of — which is precisely the disjointedness this
   whole change exists to remove.

   Agents is the odd one out and deliberately so: it is not an iframe, so there is nothing
   to keep mounted, and its list is REFETCHED on every switch to it. Status is the point of
   that view — an agent's pod can go from starting to running without anybody touching the
   page — and a cached list would show a green dot next to something that has since
   crashed. */

let LINKS = {};
let IS_ADMIN = false;
const TAB_KEY = "eai.tab.v1";
const TABS = ["chat", "code", "agents"];

function mountFrames() {
  const last = localStorage.getItem(TAB_KEY);
  showTab(TABS.includes(last) ? last : "chat");
}

function loadFrame(which) {
  const frame = which === "code" ? $("frame-code") : $("frame-chat");
  if (frame.dataset.loaded) return;
  // The workshop is proxied on THIS origin at /workshop/; chat is the origin root. Both
  // same-origin, which is what makes them embeddable at all — the old workspace URL was
  // a plain-HTTP LAN address that a browser refuses to frame inside an HTTPS page.
  frame.src = which === "code" ? "/workshop/" : (LINKS.chat || "/");
  frame.dataset.loaded = "1";
}

const TAB_TITLE = { chat: "Chat", code: "Code", agents: "Agents" };

function showTab(which) {
  if (!TABS.includes(which)) which = "chat";
  for (const t of TABS) {
    $("view-" + t).hidden = t !== which;
    $("tab-" + t).setAttribute("aria-selected", String(t === which));
  }
  if (which === "agents") {
    loadAgents();
  } else {
    loadFrame(which);
    // Tell the frame to re-measure once it is actually on screen. A frame laid out while
    // its tab was hidden measures zero width, and ttyd sizes its terminal to whatever it
    // measured — which is how the prompt ends up off-screen. Same-origin, so this is a
    // plain event; wrapped because a frame that has not finished loading has no window yet.
    requestAnimationFrame(() => {
      const f = which === "code" ? $("frame-code") : $("frame-chat");
      try { f.contentWindow?.dispatchEvent(new Event("resize")); } catch {}
    });
  }
  localStorage.setItem(TAB_KEY, which);
  document.title = `${TAB_TITLE[which]} — Enterprise AI`;
}

$("tab-chat").addEventListener("click", () => showTab("chat"));
$("tab-code").addEventListener("click", () => showTab("code"));
$("tab-agents").addEventListener("click", () => showTab("agents"));
$("code-retry").addEventListener("click", () => {
  const f = $("frame-code");
  delete f.dataset.loaded;
  $("code-fallback").hidden = true;
  loadFrame("code");
});

// A dead workshop cannot be caught from `error` — an iframe whose navigation came back
// with an HTTP error status still fires `load`, never `error`, so listening for a failed
// load reports nothing and the raw 502 body renders straight at the user. What DOES tell
// the two apart, because workshop_proxy is same-origin, is the framed document itself:
// the proxy's own error responses come back `application/json` (FastAPI's HTTPException
// body); the real workshop page is `text/html`. Checked on every load, not only the
// first, so the panel also clears the moment a retry actually lands on a live pod, and
// re-appears if a retry lands on a pod that is still down.
$("frame-code").addEventListener("load", () => {
  const frame = $("frame-code");
  if (!frame.dataset.loaded) return; // the iframe's own initial about:blank, not a mount
  let broken;
  try {
    broken = frame.contentDocument?.contentType === "application/json";
  } catch {
    // Cross-origin would mean this is not our proxy's response at all; nothing to flag.
    broken = false;
  }
  $("code-fallback").hidden = !broken;
});

/* ---------------------------------------------------------------- user menu */

function toggleMenu(show) {
  const open = show ?? $("user-menu").hidden;
  $("user-menu").hidden = !open;
  $("avatar-btn").setAttribute("aria-expanded", String(open));
}
$("avatar-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(); });
document.addEventListener("click", () => toggleMenu(false));
$("user-menu").addEventListener("click", (e) => e.stopPropagation());

$("mi-settings").addEventListener("click", () => {
  toggleMenu(false);
  // Refresh on open rather than on page load: these numbers go stale while somebody is
  // working, and the settings sheet is the only place they are visible.
  loadSpend(); loadPublished(); loadKeys(); loadAdmin();
  $("dlg-settings").showModal();
});
$("settings-close").addEventListener("click", () => $("dlg-settings").close());

document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA", "IFRAME", "SELECT"].includes(document.activeElement?.tagName);
  if (e.key === "Escape") { toggleMenu(false); return; }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  // Digit shortcuts, the way a tabbed app is expected to behave.
  if (e.key === "1") showTab("chat");
  if (e.key === "2") showTab("code");
  if (e.key === "3") showTab("agents");
});

/* ---------------------------------------------------------------- spend */

async function loadSpend() {
  let d;
  try { d = await get("/portal/api/spend" + sinceParam()); }
  catch { $("spend-total").textContent = "—"; $("spend-empty").hidden = false;
          $("spend-empty").textContent = "Could not load spend just now."; return; }

  $("spend-total").textContent = money(d.total?.spend);
  const rows = d.by_surface || [];
  const tbody = $("spend-rows");
  tbody.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    const tokens = (r.prompt_tokens || 0) + (r.completion_tokens || 0);
    tr.innerHTML =
      `<td><span class="surface-tag"></span></td>` +
      `<td class="num">${compact(r.requests)}</td>` +
      `<td class="num">${compact(tokens)}</td>` +
      `<td class="num">${money(r.spend)}</td>`;
    // textContent, not innerHTML: the surface name comes from a key alias and is not
    // ours to trust as markup.
    tr.querySelector(".surface-tag").textContent = r.surface;
    tbody.appendChild(tr);
  }
  $("spend-table").hidden = rows.length === 0;
  $("spend-empty").hidden = rows.length !== 0;
}

$("since").addEventListener("change", loadSpend);

/* ---------------------------------------------------------------- published work */

async function loadPublished() {
  let d;
  try { d = await get("/portal/api/published"); } catch { return; }
  const list = $("worklist");
  list.innerHTML = "";
  const items = d.projects || [];
  for (const p of items) {
    const li = document.createElement("li");
    const dot = document.createElement("span"); dot.className = "dot";
    const name = document.createElement("span"); name.className = "name grow";
    name.textContent = p.name;
    li.append(dot, name);
    if (p.url) {
      const open = document.createElement("a");
      open.className = "btn small"; open.textContent = "Open";
      open.href = p.url; open.target = "_blank"; open.rel = "noopener";
      const copy = document.createElement("button");
      copy.className = "btn small ghost"; copy.textContent = "Copy link";
      copy.addEventListener("click", () => copyText(p.url, "Link copied."));
      li.append(copy, open);
    }
    list.appendChild(li);
  }
  $("work-empty").hidden = items.length !== 0;
}

/* Clipboard needs a secure context. The workspaces are reached over plain HTTP on the
 * LAN, where navigator.clipboard is simply undefined — so there is a fallback rather
 * than a button that silently does nothing. */
async function copyText(text, okMsg) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      toast(okMsg);
      return;
    }
    throw new Error("no clipboard");
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    let done = false;
    try { done = document.execCommand("copy"); } catch { done = false; }
    ta.remove();
    toast(done ? okMsg : "Could not copy — select the text and copy it by hand.", done);
  }
}

/* ---------------------------------------------------------------- keys */

const SURFACE_BLURB = {
  chat: "Used by the chat surface on your behalf.",
  ide: "Held by your Workshop pod.",
  terminal: "For a terminal agent on your own machine.",
};

async function loadKeys() {
  let d;
  try { d = await get("/portal/api/keys"); } catch { return; }
  const list = $("keylist");
  list.innerHTML = "";
  const keys = d.keys || [];
  for (const k of keys) {
    const li = document.createElement("li");
    const col = document.createElement("div"); col.className = "grow";
    const alias = document.createElement("div"); alias.className = "alias";
    alias.textContent = k.alias;
    const meta = document.createElement("div"); meta.className = "meta";
    const budget = k.max_budget == null ? "no limit set" : `limit ${money(k.max_budget)}`;
    const blurb = SURFACE_BLURB[k.surface] || "";
    meta.textContent = blurb ? `${blurb} · ${budget}` : budget;
    col.append(alias, meta);
    const rot = document.createElement("button");
    rot.className = "btn small";
    rot.textContent = "Rotate";
    rot.addEventListener("click", () => rotate(k.surface, k.alias));
    li.append(col, rot);
    list.appendChild(li);
  }
  $("keys-empty").hidden = keys.length !== 0;
}

function confirmDialog({ title, body, danger }) {
  return new Promise((resolve) => {
    const d = $("dlg-confirm");
    $("confirm-title").textContent = title;
    $("confirm-body").textContent = body;
    $("confirm-go").textContent = danger || "Yes";
    d.returnValue = "cancel";
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true });
    d.showModal();
  });
}

async function rotate(surface, alias) {
  const ok = await confirmDialog({
    title: `Rotate ${alias}?`,
    body: "The current key stops working immediately. Anything still using it — including "
        + "a running workspace — must be given the new one.",
    danger: "Rotate it",
  });
  if (!ok) return;
  const { ok: good, data } = await post("/portal/api/keys/rotate", { surface });
  if (!good) { toast(data.detail || "Could not rotate that key.", false); return; }
  $("dlg-key-title").textContent = `New key for ${data.alias}`;
  $("new-key").textContent = data.key;
  $("dlg-key").showModal();
  loadKeys();
}

$("copy-key").addEventListener("click", () => copyText($("new-key").textContent, "Key copied."));
$("close-key").addEventListener("click", () => {
  // Clear it out of the DOM on close; there is no reason for it to linger in the page.
  $("new-key").textContent = "";
  $("dlg-key").close();
});

/* ---------------------------------------------------------------- operator

   Only renders for a name in PORTAL_ADMINS. A non-operator gets 404 from the endpoint —
   not 403 — so the panel simply never appears rather than advertising that it exists. */

async function loadAdmin() {
  // Ask only if we are told we may. The endpoint still enforces it — this just avoids a
  // 404 in every camper's console for a panel they were never going to see.
  if (!IS_ADMIN) { $("panel-admin").hidden = true; return; }
  let d;
  try { d = await get("/portal/api/admin/overview" + sinceParam("admin-since")); }
  catch { $("panel-admin").hidden = true; return; }
  $("panel-admin").hidden = false;
  $("admin-total").textContent = money(d.totals?.spend);

  const body = $("admin-rows");
  body.innerHTML = "";
  for (const p of d.people || []) {
    const tr = document.createElement("tr");
    const tokens = (p.prompt_tokens || 0) + (p.completion_tokens || 0);
    const caps = Object.values(p.budgets || {})
      .map((b) => b.max_budget).filter((x) => x != null);
    const cap = caps.length ? money(Math.min(...caps)) : "—";
    tr.innerHTML =
      "<td></td><td class='surfaces'></td>" +
      `<td class="num">${compact(p.requests)}</td>` +
      `<td class="num">${compact(tokens)}</td>` +
      `<td class="num">${money(p.spend)}</td>` +
      `<td class="num">${cap}</td>`;
    // textContent throughout: these are usernames and key aliases, not markup.
    tr.children[0].textContent = p.username;
    tr.children[1].textContent = (p.surfaces || []).map((s) => s.surface).join(", ") || "—";
    body.appendChild(tr);
  }

  const unpriced = d.unpriced_models || [];
  $("admin-unpriced").hidden = unpriced.length === 0;
  if (unpriced.length) {
    $("admin-unpriced").textContent =
      `${unpriced.length} model(s) served traffic at $0, so this total under-reports and ` +
      `budgets cannot trip on them: ${unpriced.slice(0, 6).join(", ")}` +
      (unpriced.length > 6 ? "…" : "");
  }

  try {
    const a = await get("/portal/api/admin/audit?limit=1");
    const v = a.verified || {};
    const okay = v.ok === true;
    // Do not interpolate a count that may not be there — it rendered as
    // "Audit chain verifies. entries." which reads like a truncated sentence.
    $("admin-audit").textContent = okay
      ? "Audit chain verifies."
      : `Audit chain DOES NOT verify${v.broken_at ? ` (break at #${v.broken_at})` : ""} `
        + "— investigate before trusting these numbers.";
    $("admin-audit").classList.toggle("bad", !okay);
  } catch { $("admin-audit").textContent = ""; }
}

$("admin-since").addEventListener("change", loadAdmin);

/* ---------------------------------------------------------------- agents

   The third surface. Every control here is owner-scoped SERVER SIDE — the endpoints take
   the owner from the signed-in session and never from anything this file sends — so
   nothing below is a permission check. It is a rendering of what the caller owns, and if
   it ever showed somebody else's agent, the bug would be in the control plane, not here.

   Two dimensions per row, never added: `inference` is dollars off the gateway ledger,
   `usage` is quantities off the resident meter. Owned compute has no price
   (enterpriseaiframework-914), so putting a dollar sign on hours would be inventing a
   number nobody owes. */

const STATUS_TEXT = {
  running: "running",
  starting: "starting…",
  stopped: "stopped",
  unknown: "unknown",
};

async function del(path) {
  const r = await fetch(path, { method: "DELETE", headers: { Accept: "application/json" } });
  return { ok: r.ok, data: await r.json().catch(() => ({})) };
}

/* Hours, at the scale a camp session actually produces. Rounding 4 minutes to "0.1h"
 * reads as a broken meter in exactly the way sub-cent spend did. */
function hours(n) {
  const v = Number(n || 0);
  if (v === 0) return "0h";
  if (v < 1) return Math.round(v * 60) + "m";
  return v.toFixed(1) + "h";
}

let AGENTS_BUSY = false;

async function loadAgents() {
  let d;
  try { d = await get("/portal/api/agents"); }
  catch (e) {
    $("agents-error").hidden = false;
    $("agents-error").textContent =
      "Could not read your agents just now. The list below may be out of date.";
    return;
  }
  $("agents-error").hidden = !d.usage_error;
  if (d.usage_error) {
    // An empty usage block WITH an error means "we do not know", which is a different
    // statement from zero and is worth more than a silent dash.
    $("agents-error").textContent =
      "Your agents are listed, but their hours could not be read: " + d.usage_error;
  }

  const models = d.models || [];
  const sel = $("agent-model");
  if (sel.dataset.filled !== String(models.length)) {
    sel.innerHTML = "";
    for (const m of models) {
      const opt = document.createElement("option");
      opt.value = m; opt.textContent = m;
      sel.appendChild(opt);
    }
    sel.dataset.filled = String(models.length);
    sel.hidden = models.length < 2;
  }

  const list = $("agentlist");
  list.innerHTML = "";
  const rows = d.agents || [];
  for (const a of rows) list.appendChild(agentRow(a));
  $("agents-empty").hidden = rows.length !== 0;
}

function agentRow(a) {
  const li = document.createElement("li");
  li.className = "agent " + (a.status || "unknown");

  const head = document.createElement("div");
  head.className = "agent-head";
  const dot = document.createElement("span");
  dot.className = "status-dot";
  const name = document.createElement("span");
  // textContent: an agent name is user-chosen. It is slug-constrained server side, and
  // this page still does not hand it to the HTML parser.
  name.className = "name grow"; name.textContent = a.name;
  const state = document.createElement("span");
  state.className = "status"; state.textContent = STATUS_TEXT[a.status] || a.status;
  head.append(dot, name, state);

  const meta = document.createElement("div");
  meta.className = "agent-meta";
  const inf = a.inference || {};
  const use = a.usage;
  const bits = [];
  bits.push(inf.on_ledger === false
    ? "inference off-ledger (your own provider)"
    : `${money(inf.spend)} inference · ${compact(inf.requests)} requests`);
  if (use) {
    bits.push(`${hours(use.resident_hours)} resident`);
    bits.push(use.compute_measured
      ? `${hours(use.cpu_core_hours)} CPU-core`
      : "compute not measured");
  } else {
    bits.push("no usage recorded yet");
  }
  // WHICH connectors, never a credential. The server sends booleans read off the pod
  // template — there is no endpoint that could send back a token, so there is nothing
  // here that could render one.
  const wired = Object.keys(a.connectors || {}).filter((k) => a.connectors[k]).sort();
  bits.push(wired.length ? wired.join(" + ") + " connected" : "no chat or email yet");
  meta.textContent = bits.join(" · ");

  const controls = document.createElement("div");
  controls.className = "agent-controls";

  const open = document.createElement("a");
  open.className = "btn small"; open.textContent = "Open console";
  open.href = a.console_url; open.target = "_blank"; open.rel = "noopener";
  if (a.status !== "running") open.classList.add("disabled");
  controls.appendChild(open);

  const toggle = document.createElement("button");
  toggle.className = "btn small ghost";
  toggle.textContent = a.status === "stopped" ? "Start" : "Stop";
  toggle.addEventListener("click", () => a.status === "stopped"
    ? act(`/portal/api/agents/${encodeURIComponent(a.name)}/start`, `${a.name} is starting.`)
    : stopAgent(a.name));
  controls.appendChild(toggle);

  // The same wizard, attached to an agent that already exists. An agent created before
  // this existed, or created without a connector, is reachable here rather than needing
  // to be deleted and made again.
  const wire = document.createElement("button");
  wire.className = "btn small ghost"; wire.textContent = "Chat & email";
  wire.addEventListener("click", () => openSetup(a.name));
  controls.appendChild(wire);

  const drop = document.createElement("button");
  drop.className = "btn small danger"; drop.textContent = "Delete";
  drop.addEventListener("click", () => deleteAgent(a.name));
  controls.appendChild(drop);

  li.append(head, meta, controls);
  return li;
}

async function act(path, okMsg) {
  if (AGENTS_BUSY) return;
  AGENTS_BUSY = true;
  try {
    const { ok, data } = await post(path, {});
    if (!ok) { toast(data.detail || "That did not work.", false); return; }
    toast(okMsg);
    await loadAgents();
  } finally { AGENTS_BUSY = false; }
}

async function stopAgent(name) {
  const ok = await confirmDialog({
    title: `Stop ${name}?`,
    body: "It stops running and stops costing anything. Its work is kept on its disk and "
        + "picks up where it left off when you start it again.",
    danger: "Stop it",
  });
  if (ok) await act(`/portal/api/agents/${encodeURIComponent(name)}/stop`, `${name} is stopping.`);
}

async function deleteAgent(name) {
  const ok = await confirmDialog({
    title: `Delete ${name}?`,
    body: "This destroys its disk and everything on it, and cannot be undone. Its key "
        + "stops working immediately. Stopping it instead keeps all of that.",
    danger: "Delete it for good",
  });
  if (!ok) return;
  if (AGENTS_BUSY) return;
  AGENTS_BUSY = true;
  try {
    const { ok: good, data } = await del(`/portal/api/agents/${encodeURIComponent(name)}`);
    if (!good) { toast(data.detail || "Could not delete that agent.", false); return; }
    // Honest about the one step that lags: the volume keeps a finalizer while its pod is
    // still terminating. Claiming it is gone when it is not is how a deleted agent
    // reappears on the next refresh and looks like a bug.
    toast(data.volume_terminating
      ? `${name} is deleted; its disk is still being released.`
      : `${name} is deleted.`);
    await loadAgents();
  } finally { AGENTS_BUSY = false; }
}

/* ---------------------------------------------------------------- the setup wizard

   THE STEP THAT USED TO NEED AN OPERATOR. Creating an agent has been self-serve since
   -627; giving it the Slack workspace or the mailbox that makes it useful could only be
   done by somebody with a kubeconfig running provision-agent.sh. This dialog is that step,
   and nothing else: it POSTs the user's own credential to
   /portal/api/agents/<name>/connectors, which writes the same Secret the shell script
   writes and rolls the pod.

   THE FIELD LIST IS NOT INVENTED HERE. Every `key` below is one of the AGENT_<KIND>_*
   names app/agents.py allows and deploy/bin/provision-agent.sh allows, and
   control-plane/tests/test_portal_connectors.py compares this table against the Python
   one so a field added on one side cannot go missing on the other. A key this page made
   up would be refused by the endpoint; a key it omitted would be a setting no user could
   reach.

   `secret: true` means the input is type=password AND is never read back — there is no
   endpoint that returns a credential, so the only place these values ever exist on this
   page is between the keystroke and the POST. */

const CONNECTOR_FIELDS = {
  slack: [
    { key: "AGENT_SLACK_BOT_TOKEN", label: "Bot token", hint: "starts xoxb-",
      secret: true, required: true },
    // Both, always. The bot token posts; the app-level token opens the Socket Mode
    // connection the agent LISTENS on. With only the first it can talk and never hear an
    // answer, which is not a state worth letting somebody create.
    { key: "AGENT_SLACK_APP_TOKEN", label: "App-level token", hint: "starts xapp-",
      secret: true, required: true },
    { key: "AGENT_SLACK_DEFAULT_CHANNEL", label: "Default channel",
      hint: "optional — a channel id like C0123ABCD" },
  ],
  discord: [
    { key: "AGENT_DISCORD_BOT_TOKEN", label: "Bot token", secret: true, required: true },
    { key: "AGENT_DISCORD_DEFAULT_CHANNEL", label: "Default channel",
      hint: "optional — a channel id" },
  ],
  email: [
    { key: "AGENT_EMAIL_ADDRESS", label: "Address", required: true,
      hint: "the address it sends from" },
    { key: "AGENT_EMAIL_USERNAME", label: "Username", hint: "optional — defaults to the address" },
    { key: "AGENT_EMAIL_PASSWORD", label: "Password", secret: true, required: true },
    { key: "AGENT_EMAIL_SMTP_HOST", label: "SMTP host", required: true },
    { key: "AGENT_EMAIL_SMTP_PORT", label: "SMTP port", hint: "optional" },
    { key: "AGENT_EMAIL_SMTP_SECURITY", label: "SMTP security", hint: "starttls or ssl" },
    { key: "AGENT_EMAIL_IMAP_HOST", label: "IMAP host", required: true },
    { key: "AGENT_EMAIL_IMAP_PORT", label: "IMAP port", hint: "optional" },
    { key: "AGENT_EMAIL_IMAP_SECURITY", label: "IMAP security", hint: "ssl or starttls" },
  ],
};

// Setup guidance shown inline with the fields, because "App-level token (starts xapp-)" is
// not a thing a first-time user can produce without being told where it lives. Steps are
// the same ones tracked as the human prerequisites (enterpriseaiframework-54d Slack,
// -9ce Discord); the link goes to the console that mints the credential.
const CONNECTOR_HELP = {
  slack: {
    summary: "How to create a Slack bot token",
    url: "https://api.slack.com/apps",
    steps: [
      "At api.slack.com/apps → Create New App → From scratch, and pick your workspace.",
      "OAuth & Permissions → Bot Token Scopes: add chat:write (and channels:history + app_mentions:read so it can read). Install to Workspace, then copy the Bot User OAuth Token — that is the xoxb- bot token.",
      "Socket Mode → turn it on, and Generate an App-Level Token with the connections:write scope — that xapp- token is the App-level token above (it is how the agent listens).",
      "Event Subscriptions → enable, and subscribe to message.channels (and/or app_mention).",
      "In Slack, /invite @your-bot to a channel. For the default channel, open the channel details and copy its ID (looks like C0123ABCD).",
    ],
  },
  discord: {
    summary: "How to create a Discord bot token",
    url: "https://discord.com/developers/applications",
    steps: [
      "At discord.com/developers/applications → New Application.",
      "Bot → Reset Token → copy the token — that is the Bot token above.",
      "Bot → Privileged Gateway Intents → turn ON MESSAGE CONTENT INTENT. Without it Discord delivers every message empty and reports no error.",
      "OAuth2 → URL Generator → scopes: bot; bot permissions: Send Messages + Read Message History. Open the generated URL to add the bot to your server.",
      "Settings → Advanced → enable Developer Mode, then right-click a channel → Copy Channel ID for the default channel.",
    ],
  },
  email: {
    summary: "Where these SMTP/IMAP settings come from",
    steps: [
      "Host, port and security are your email provider's — the agent connects as a normal client, nothing is self-hosted.",
      "Gmail: smtp.gmail.com 465 ssl · imap.gmail.com 993 ssl · and an App Password (not your login) if 2-step verification is on.",
      "Microsoft 365: smtp.office365.com 587 starttls · outlook.office365.com 993 ssl.",
    ],
  },
};

let SETUP_MODE = "create";   // "create" also names and creates the agent
let SETUP_NAME = "";

function connectorHelp(container, kind) {
  const help = CONNECTOR_HELP[kind];
  if (!help) return;
  const d = document.createElement("details");
  d.className = "connector-help";
  const s = document.createElement("summary");
  s.textContent = help.summary;
  d.appendChild(s);
  const ol = document.createElement("ol");
  for (const step of help.steps) {
    const li = document.createElement("li");
    li.textContent = step;
    ol.appendChild(li);
  }
  d.appendChild(ol);
  if (help.url) {
    const a = document.createElement("a");
    a.href = help.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.className = "connector-help-link";
    a.textContent = "Open " + new URL(help.url).host + " ↗";
    d.appendChild(a);
  }
  container.appendChild(d);
}

function connectorFields(container, kind) {
  container.innerHTML = "";
  connectorHelp(container, kind);
  for (const f of CONNECTOR_FIELDS[kind] || []) {
    const label = document.createElement("label");
    label.className = "field";
    const span = document.createElement("span");
    span.textContent = f.label;
    const input = document.createElement("input");
    // type=password on anything that is a credential: it keeps it off the screen of a
    // shared machine and out of the browser's autofill history for ordinary text fields.
    input.type = f.secret ? "password" : "text";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.dataset.key = f.key;
    input.dataset.kind = kind;
    if (f.required) input.dataset.required = "1";
    if (f.hint) input.placeholder = f.hint;
    label.append(span, input);
    container.appendChild(label);
  }
}

function readConnector(container, kind) {
  const values = {};
  for (const input of container.querySelectorAll("input[data-key]")) {
    const v = input.value.trim();
    if (v) values[input.dataset.key] = v;
  }
  const missing = [...container.querySelectorAll("input[data-required]")]
    .filter((i) => !i.value.trim())
    .map((i) => i.previousElementSibling.textContent);
  return { kind, values, missing };
}

function openSetup(name) {
  SETUP_MODE = name ? "configure" : "create";
  SETUP_NAME = name || "";
  $("setup-title").textContent = name ? `Chat and email for ${name}` : "Set up an agent";
  $("setup-identity").hidden = SETUP_MODE === "configure";
  $("setup-name").required = SETUP_MODE === "create";
  $("setup-name").value = SETUP_MODE === "create" ? ($("agent-name").value.trim() || "") : "";
  $("setup-submit").textContent = name ? "Connect it" : "Create and connect";
  $("setup-chat").value = "slack";
  $("setup-email-on").checked = false;
  $("setup-email-fields").hidden = true;
  $("setup-error").hidden = true;
  $("setup-progress").hidden = true;
  connectorFields($("setup-chat-fields"), "slack");
  connectorFields($("setup-email-fields"), "email");
  // The model list is already loaded for the inline form; mirror it rather than fetching.
  $("setup-model").innerHTML = $("agent-model").innerHTML;
  $("setup-model-field").hidden = $("agent-model").hidden;
  $("dlg-agent-setup").showModal();
}

function closeSetup() {
  // Clear before closing: a token left in a detached-but-still-parented input is a
  // credential sitting in the DOM of whatever tab this is, for as long as it stays open.
  for (const input of $("dlg-agent-setup").querySelectorAll("input")) {
    if (input.type === "checkbox") input.checked = false; else input.value = "";
  }
  $("dlg-agent-setup").close();
}

$("agent-setup-open").addEventListener("click", () => openSetup(""));
$("setup-close").addEventListener("click", closeSetup);
$("setup-chat").addEventListener("change", () => {
  const kind = $("setup-chat").value;
  connectorFields($("setup-chat-fields"), kind === "none" ? "" : kind);
});
$("setup-email-on").addEventListener("change", () => {
  $("setup-email-fields").hidden = !$("setup-email-on").checked;
});

async function configureConnector(name, kind, values) {
  return post(`/portal/api/agents/${encodeURIComponent(name)}/connectors`,
              { kind, values });
}

$("setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (AGENTS_BUSY) return;

  const chat = $("setup-chat").value;
  const wanted = [];
  if (chat !== "none") wanted.push(readConnector($("setup-chat-fields"), chat));
  if ($("setup-email-on").checked) wanted.push(readConnector($("setup-email-fields"), "email"));

  // Checked here so a half-filled form does not create an agent and then fail — the
  // create is the irreversible half of this pair.
  const short = wanted.find((w) => w.missing.length);
  if (short) {
    $("setup-error").hidden = false;
    $("setup-error").textContent =
      `${short.kind} needs ${short.missing.join(" and ")}. A connector with only some of `
      + "its settings can talk and not listen, which looks like the agent ignoring you.";
    return;
  }
  $("setup-error").hidden = true;

  const name = SETUP_MODE === "create" ? $("setup-name").value.trim() : SETUP_NAME;
  if (!name) return;

  AGENTS_BUSY = true;
  $("setup-submit").disabled = true;
  $("setup-progress").hidden = false;
  try {
    if (SETUP_MODE === "create") {
      $("setup-progress").textContent = `Creating ${name}…`;
      const { ok, data } = await post("/portal/api/agents",
        { name, model: $("setup-model").value || undefined });
      if (!ok) {
        $("setup-error").hidden = false;
        $("setup-error").textContent = data.detail || "Could not create that agent.";
        return;
      }
      $("agent-name").value = "";
    }
    for (const w of wanted) {
      $("setup-progress").textContent = `Connecting ${w.kind}…`;
      const { ok, data } = await configureConnector(name, w.kind, w.values);
      if (!ok) {
        // Precise about the split outcome: the agent exists, this one connector did not
        // take. Saying "it failed" would send somebody to create a second agent.
        $("setup-error").hidden = false;
        $("setup-error").textContent =
          (SETUP_MODE === "create" ? `${name} was created, but ` : "")
          + `${w.kind} was not connected: ` + (data.detail || "the request was refused.");
        return;
      }
    }
    closeSetup();
    toast(wanted.length
      ? `${name} is starting with ${wanted.map((w) => w.kind).join(" and ")} connected.`
      : `${name} is starting. It takes about a minute to be ready.`);
  } finally {
    AGENTS_BUSY = false;
    $("setup-submit").disabled = false;
    $("setup-progress").hidden = true;
    await loadAgents();
  }
});

$("agent-new").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("agent-name");
  const name = input.value.trim();
  if (!name || AGENTS_BUSY) return;
  AGENTS_BUSY = true;
  $("agent-create").disabled = true;
  try {
    const { ok, data } = await post("/portal/api/agents",
      { name, model: $("agent-model").value || undefined });
    if (!ok) { toast(data.detail || "Could not create that agent.", false); return; }
    input.value = "";
    toast(`${name} is starting. It takes about a minute to be ready.`);
    await loadAgents();
  } finally {
    AGENTS_BUSY = false;
    $("agent-create").disabled = false;
  }
});

/* ---------------------------------------------------------------- boot */

async function boot() {
  $("failbar").hidden = true;
  try {
    await loadMe();
  } catch (e) {
    // Identity is the one thing the page cannot do without — everything else is scoped
    // to it. Fail visibly rather than rendering an empty shell that looks like an account
    // with nothing in it.
    $("failbar").hidden = false;
    return;
  }
  // Settings content is fetched when the sheet is opened, not on load: it is not on the
  // critical path to somebody's work, and three requests behind a page that is about to
  // mount two iframes is three requests competing with the thing they came for.
}

$("retry").addEventListener("click", boot);
boot();
