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

function sinceParam() {
  const v = $("since").value;
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
  $("who").hidden = false;
  document.title = `${me.username} — Enterprise AI`;

  const L = me.links || {};
  wire("card-chat", L.chat);
  wire("card-published", L.published);
  wire("link-password", L.password || L.account);
  wire("link-account", L.account);
  if (L.signout) $("signout").href = L.signout;

  // A workspace is provisioned per user and may not exist yet. Saying so plainly beats a
  // link that goes nowhere.
  if (L.workspace) {
    wire("card-workspace", L.workspace);
  } else {
    const c = $("card-workspace");
    c.classList.add("disabled");
    c.removeAttribute("href");
    $("no-workspace").hidden = false;
  }
  return me;
}

function wire(id, href) {
  const el = $(id);
  if (!el) return;
  if (href) el.href = href;
  else { el.classList.add("disabled"); el.removeAttribute("href"); }
}

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
  // Independently, so one slow or broken panel cannot hold up the others.
  loadSpend();
  loadPublished();
  loadKeys();
}

$("retry").addEventListener("click", boot);
boot();
