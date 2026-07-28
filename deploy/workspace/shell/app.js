/* Workshop shell behaviour. No framework and no build step on purpose — this has to keep
 * working for as long as the camp does, without anyone running npm.
 */

const $ = (id) => document.getElementById(id);

/* Starter prompts. Phrased the way a child would say it, and each one is deliberately
 * whole — "a game where..." rather than "a game" — because a vague ask gets a vague
 * program and the child concludes the tool is bad. */
const IDEAS = [
  "Make a game where a unicorn runs and jumps over rocks. Put it all in index.html.",
  "Make a drawing app where I can paint with rainbow colours and clear the screen.",
  "Make a memory card game with animal emojis that I can play.",
  "Make a page that asks me questions and tells me which dinosaur I am.",
  "Make a maze I can walk through with the arrow keys.",
  "Make a music toy where each key on the keyboard plays a different sound.",
];

let toastTimer = null;
function toast(msg, ok = true) {
  const el = $("toast");
  el.innerHTML = msg;
  el.classList.toggle("bad", !ok);
  el.hidden = false;
  clearTimeout(toastTimer);
  // Long enough to read a URL, short enough not to sit over the app.
  toastTimer = setTimeout(() => { el.hidden = true; }, ok ? 9000 : 12000);
}

/* ------------------------------------------------------------------ preview */

function refreshPreview() {
  const f = $("preview");
  // Cache-bust rather than reload(): the iframe is same-origin but a plain reload can be
  // served from cache, and a child who sees no change assumes their edit failed.
  const url = new URL("/preview/", location.origin);
  url.searchParams.set("_", Date.now());
  f.src = url.toString();
}

$("refresh").addEventListener("click", refreshPreview);
$("popout").addEventListener("click", () => window.open("/preview/", "_blank", "noopener"));

let autoTimer = null;
$("autorefresh").addEventListener("change", (e) => {
  clearInterval(autoTimer);
  if (e.target.checked) {
    // 3s: fast enough to feel live while the agent writes, slow enough that a half-written
    // file is unlikely to be what you catch.
    autoTimer = setInterval(refreshPreview, 3000);
  }
});

/* ------------------------------------------------------------------ panel */

function showPanel(which) {
  $("panel").hidden = false;
  $("panel-guide").hidden = which !== "guide";
  $("panel-settings").hidden = which !== "settings";
  $("tab-guide").setAttribute("aria-pressed", which === "guide");
  $("tab-settings").setAttribute("aria-pressed", which === "settings");
}
$("tab-guide").addEventListener("click", () => showPanel("guide"));
$("tab-settings").addEventListener("click", () => showPanel("settings"));
$("panel-close").addEventListener("click", () => { $("panel").hidden = true; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("panel").hidden = true; });

$("ideas").innerHTML = "";
for (const idea of IDEAS) {
  const li = document.createElement("li");
  const b = document.createElement("button");
  b.textContent = idea;
  b.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(idea);
      toast("Copied. Click the black panel on the left and paste it in.");
    } catch {
      // Clipboard needs a secure context and permission; failing silently would look
      // like a dead button.
      toast("Could not copy — select the text and copy it by hand.", false);
    }
  });
  li.appendChild(b);
  $("ideas").appendChild(li);
}

/* ------------------------------------------------------------------ state */

async function loadState() {
  let s;
  try {
    s = await (await fetch("/api/state")).json();
  } catch {
    toast("Lost contact with the workspace. Reload the page.", false);
    return;
  }
  $("who").textContent = s.user;

  const sel = $("model");
  sel.innerHTML = "";
  for (const m of s.models) {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = m.label;
    o.selected = m.id === s.model;
    sel.appendChild(o);
  }

  $("published-url").value = s.published_url || "(not shared yet)";
  $("published-state").textContent = s.published
    ? "Shared. Anyone with the link can open it."
    : "Nothing shared yet. Press “Share it”.";
}

$("model").addEventListener("change", async (e) => {
  const r = await fetch("/api/model", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: e.target.value }),
  });
  const d = await r.json();
  toast(d.message || "Saved.", r.ok);
});

$("copy-url").addEventListener("click", async () => {
  const v = $("published-url").value;
  if (!v.startsWith("http")) { toast("Share it first.", false); return; }
  try { await navigator.clipboard.writeText(v); toast("Link copied."); }
  catch { toast("Could not copy — select it and copy by hand.", false); }
});

/* ------------------------------------------------------------------ publish */

$("publish").addEventListener("click", async () => {
  const btn = $("publish");
  btn.disabled = true;
  btn.textContent = "Sharing…";
  try {
    const r = await fetch("/api/publish", { method: "POST" });
    const d = await r.json();
    if (d.ok && d.url) {
      toast(`Shared. Send this to anyone: <a href="${d.url}" target="_blank" rel="noopener">${d.url}</a>`);
      await loadState();
    } else {
      // The publish command explains its own refusals (no index.html, too big) better
      // than a second copy of that logic here would.
      toast(d.message || "Could not share.", false);
    }
  } catch {
    toast("Could not reach the workspace.", false);
  } finally {
    btn.disabled = false;
    btn.textContent = "Share it";
  }
});

loadState();
