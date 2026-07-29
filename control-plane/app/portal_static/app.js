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

   Both surfaces stay mounted once visited. Switching tabs only flips visibility, because
   reloading chat every time somebody glances at their code would throw away the
   conversation they were in the middle of — which is precisely the disjointedness this
   whole change exists to remove. */

let LINKS = {};
let IS_ADMIN = false;
const TAB_KEY = "eai.tab.v1";

function mountFrames() {
  const last = localStorage.getItem(TAB_KEY) === "code" ? "code" : "chat";
  showTab(last);
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

function showTab(which) {
  const code = which === "code";
  $("view-chat").hidden = code;
  $("view-code").hidden = !code;
  $("tab-chat").setAttribute("aria-selected", String(!code));
  $("tab-code").setAttribute("aria-selected", String(code));
  loadFrame(which);
  // Tell the frame to re-measure once it is actually on screen. A frame laid out while
  // its tab was hidden measures zero width, and ttyd sizes its terminal to whatever it
  // measured — which is how the prompt ends up off-screen. Same-origin, so this is a
  // plain event; wrapped because a frame that has not finished loading has no window yet.
  requestAnimationFrame(() => {
    const f = which === "code" ? $("frame-code") : $("frame-chat");
    try { f.contentWindow?.dispatchEvent(new Event("resize")); } catch {}
  });
  localStorage.setItem(TAB_KEY, which);
  document.title = code ? "Code — Enterprise AI" : "Chat — Enterprise AI";
}

$("tab-chat").addEventListener("click", () => showTab("chat"));
$("tab-code").addEventListener("click", () => showTab("code"));
$("code-retry").addEventListener("click", () => {
  const f = $("frame-code");
  delete f.dataset.loaded;
  $("code-fallback").hidden = true;
  loadFrame("code");
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
      // How many of those were answered from cache, and therefore cost nothing. The
      // number the API has carried since d58; until it was rendered here the operator's
      // only sight of it was the raw JSON from `make spend`.
      `<td class="num">${compact(r.cached_requests)}</td>` +
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
      `<td class="num">${compact(p.cached_requests)}</td>` +
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
