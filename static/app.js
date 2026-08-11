/* BlockClock Connect - vanilla JS, no build step.
 *
 * Endpoints used (see app.py):
 *   GET  /api/state     connection + running + saved (redacted) config
 *   GET  /api/sources   catalogue: categories, sources, schemas, frames
 *   GET  /api/geocode   city typeahead matches (weather settings)
 *   GET  /api/preview   library of the SAVED config (drives Test gating)
 *   POST /api/preview   live preview of the candidate (unsaved) config
 *   POST /api/config    validate + save + hot-reload the feeder
 *   POST /api/test      push one frame to the real clock now
 *   POST /api/connect | /api/discover | /api/disconnect
 *
 * Secrets are WRITE-ONLY: the server only ever sends {<key>_set, <key>_hint};
 * a typed value lives in a password input + S.secretsTyped until saved, and is
 * never rendered anywhere. clear_<key>:true removes a stored secret.
 */
"use strict";

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- state ----

const S = {
  catalogue: [],        // /api/sources sources array (order = display order)
  categories: [],       // [{id,label}] in display order
  currencies: [],
  bySource: {},         // sid -> catalogue entry
  frameInfo: {},        // fid -> {id,label,category,sid}
  opts: {},             // sid -> editable non-secret options
  secretMeta: {},       // sid -> {key: {set,hint}}
  secretsTyped: {},     // sid -> {key: value}   (write-only, cleared on save)
  secretsClear: {},     // sid -> {key: true}
  rotation: [],         // ordered frame ids
  frameSettings: {},    // fid -> {dwell, window:{from_hour,to_hour}}
  interval: 65,
  preview: {},          // fid -> live candidate preview {slots,color,...}
  previewLoaded: false,
  savedAvailable: new Set(),  // frame ids the SAVED config can build (Test)
  serverConfig: null,   // last adopted server config (for Discard)
  connected: null,
  running: false,
  nextWriteS: null,
  dirty: false,
  apiAccess: { enabled: false },  // redacted view only ({enabled, token_set})
};

// legacy per-source pickers the backend still honours; the rotation is the
// real picker now, so these are hidden and derived from it on save
const HIDDEN_KEYS = {
  network: ["stats"],
  weather: ["show_condition"],
  shopify: ["frames"],
};
// frames that need extra per-frame credentials inside an otherwise-ok source
const FRAME_NEEDS = {
  core_peers: "core", core_height: "core", core_mempool: "core",
  core_uptime: "core",
  lnd_channels: "lnd", lnd_balance: "lnd",
  spx_index: "fred", us_10y: "fred",
  countdown: "countdown_date",
};
// the schema arrives with keys alphabetised; present fields in a human order
const FIELD_ORDER = {
  price: ["exchange", "currency"],
  node: ["base_url", "core_host", "core_port", "core_user", "core_pass",
         "lnd_host", "lnd_port", "lnd_macaroon"],
  weather: ["city", "units"],
  macro: ["forex_base", "forex_quote", "fred_api_key"],
  space: ["countdown_label", "countdown_date"],
  shopify: ["shop_domain", "token", "currency", "daily_goal",
            "tz_offset_hours", "flash_on_sale"],
  btcpay: ["base_url", "store_id", "api_key", "tz_offset_hours"],
};
const STARTER_SET = ["btc_price", "sats_per_unit", "moscow_time",
                     "block_height", "fees", "halving"];
const OPEN_CATS = ["price", "network"];
const NODE_ADVANCED = ["core_host", "core_port", "core_user", "core_pass",
                       "lnd_host", "lnd_port", "lnd_macaroon"];
const UNIT_LABELS = { C: "Celsius (°C)", F: "Fahrenheit (°F)" };

// ---------------------------------------------------------------- utils ----

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-json error */ }
  if (!res.ok) {
    const msg = (data && data.error) ? data.error
                                     : `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

let toastTimer = null;
function toast(msg, bad = false) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.toggle("bad", bad);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 5200);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function fmtHour(h) {
  h = ((h % 24) + 24) % 24;
  if (h === 0) return "12am";
  if (h === 12) return "12pm";
  return h < 12 ? `${h}am` : `${h - 12}pm`;
}

// -------------------------------------------------- LED colour handling ----
// The clock's LED spec is RRGGBBWW (or a named pattern). On screen we blend
// the W (warm-white) channel into RGB, then lift dim colours so the glow is
// visible on a dark page.

function ledInfo(color) {
  const named = {
    flash: { css: "#ffffff", flash: true },
    white: { css: "#ffffff" },
    off: { css: "#3a3f52" },
    yellow_1: { css: "#ffd54a" },
    yellow_stars: { css: "#ffd54a", flash: true },
  };
  if (!color) return { css: "#666c82", soft: "rgba(102,108,130,.4)" };
  if (named[color]) {
    const n = named[color];
    return { css: n.css, soft: rgbaOf(n.css, 0.55), flash: !!n.flash };
  }
  const m = /^([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})?$/
    .exec(color);
  if (!m) return { css: "#666c82", soft: "rgba(102,108,130,.4)" };
  let r = parseInt(m[1], 16), g = parseInt(m[2], 16), b = parseInt(m[3], 16);
  const w = m[4] ? parseInt(m[4], 16) : 0;
  // blend the white channel in with a slightly warm cast
  r = Math.min(255, r + w);
  g = Math.min(255, g + Math.round(w * 0.93));
  b = Math.min(255, b + Math.round(w * 0.78));
  // normalise brightness so every backlight reads clearly on screen
  const mx = Math.max(r, g, b);
  if (mx > 0 && mx < 230) {
    const f = Math.min(230 / mx, 3);
    r = Math.round(r * f); g = Math.round(g * f); b = Math.round(b * f);
  }
  return { css: `rgb(${r},${g},${b})`, soft: `rgba(${r},${g},${b},.5)` };
}

function rgbaOf(hex, a) {
  const m = /^#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})$/.exec(hex);
  if (!m) return hex;
  return `rgba(${parseInt(m[1], 16)},${parseInt(m[2], 16)},` +
         `${parseInt(m[3], 16)},${a})`;
}

// ---------------------------------------------------------- mock render ----

function renderMock(slots, color, sizeCls) {
  const led = ledInfo(color);
  const mock = el("div", "mock" + (sizeCls ? ` ${sizeCls}` : "") +
                         (led.flash ? " flashing" : ""));
  mock.style.setProperty("--led", led.css);
  mock.style.setProperty("--led-soft", led.soft);
  for (const s of (slots || [])) {
    if (typeof s === "string" && s.startsWith("/")) {
      const cell = el("div", "cell pair");
      for (const part of s.slice(1).split("/")) {
        cell.appendChild(el("span", null, part));
      }
      mock.appendChild(cell);
    } else {
      const ch = (s || " ");
      mock.appendChild(el("div", "cell" + (ch.trim() === "" ? " blank" : ""),
                          ch));
    }
  }
  return mock;
}

function placeholderMock(word, sizeCls) {
  const mock = el("div", "mock" + (sizeCls ? ` ${sizeCls}` : ""));
  mock.style.setProperty("--led", "#3a3f52");
  mock.style.setProperty("--led-soft", "rgba(58,63,82,.35)");
  const txt = (word || "").toUpperCase().slice(0, 7).padStart(7, " ");
  for (const ch of txt) {
    mock.appendChild(el("div", "cell dim", ch === " " ? "" : ch));
  }
  return mock;
}

// ------------------------------------------------------ gates (validity) ----

function secretPresent(sid, key) {
  if ((S.secretsTyped[sid] || {})[key]) return true;
  if ((S.secretsClear[sid] || {})[key]) return false;
  return !!(((S.secretMeta[sid] || {})[key]) || {}).set;
}

function sourceGate(sid) {
  const o = S.opts[sid] || {};
  switch (sid) {
    case "weather":
      return (o.city || "").trim()
        ? { ok: true }
        : { ok: false, reason: "Enter your city first" };
    case "shopify": {
      const dom = (o.shop_domain || "").trim().toLowerCase();
      if (!dom || !secretPresent("shopify", "token")) {
        return { ok: false,
                 reason: "Add your shop domain and Admin API token" };
      }
      if (!dom.endsWith(".myshopify.com")) {
        return { ok: false, reason: "Use your .myshopify.com domain" };
      }
      return { ok: true };
    }
    case "btcpay": {
      const base = (o.base_url || "").trim();
      if (!/^https?:\/\//.test(base) || !(o.store_id || "").trim() ||
          !secretPresent("btcpay", "api_key")) {
        return { ok: false,
                 reason: "Add your server URL, store ID and API key" };
      }
      return { ok: true };
    }
    default:
      return { ok: true };
  }
}

function frameGate(fid) {
  const info = S.frameInfo[fid];
  if (!info) return { ok: false, reason: "Unknown frame" };
  const g = sourceGate(info.sid);
  if (!g.ok) return { ok: false, sid: info.sid, reason: g.reason };
  const need = FRAME_NEEDS[fid];
  const o = S.opts[info.sid] || {};
  if (need === "core" && !((o.core_host || "").trim() &&
      (o.core_user || "").trim() && secretPresent("node", "core_pass"))) {
    return { ok: false, sid: info.sid,
             reason: "Needs your Bitcoin Core RPC details" };
  }
  if (need === "lnd" && !((o.lnd_host || "").trim() &&
      secretPresent("node", "lnd_macaroon"))) {
    return { ok: false, sid: info.sid,
             reason: "Needs your LND host + readonly macaroon" };
  }
  if (need === "fred" && !secretPresent("macro", "fred_api_key")) {
    return { ok: false, sid: info.sid,
             reason: "Needs a free FRED API key" };
  }
  if (need === "countdown_date" && !((o.countdown_date || "").trim())) {
    return { ok: false, sid: info.sid, reason: "Pick a date to count to" };
  }
  return { ok: true, sid: info.sid };
}

function orderedSchemaEntries(src) {
  const order = FIELD_ORDER[src.id] || [];
  return Object.entries(src.options_schema || {}).sort((a, b) => {
    const ia = order.indexOf(a[0]), ib = order.indexOf(b[0]);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });
}

function visibleRotation() {
  return S.rotation.filter((fid) => frameGate(fid).ok);
}

// -------------------------------------------------------- config payloads ----

function multiChoiceIds(sid, key) {
  const schema = (S.bySource[sid] || {}).options_schema || {};
  return ((schema[key] || {}).choices || [])
    .map((c) => (typeof c === "object" ? c.id : c));
}

function optionsPayload(sid) {
  const src = S.bySource[sid];
  const out = {};
  for (const [key, meta] of Object.entries(src.options_schema || {})) {
    if (meta.type === "password") {
      const typed = (S.secretsTyped[sid] || {})[key];
      if (typed) out[key] = typed;
      else if ((S.secretsClear[sid] || {})[key]) out["clear_" + key] = true;
      continue; // a blank/absent secret keeps the saved one (server rule)
    }
    if ((HIDDEN_KEYS[sid] || []).includes(key)) continue;
    const o = S.opts[sid] || {};
    if (key in o) out[key] = o[key];
  }
  if (sid === "weather") {
    // a typeahead pick rides along as explicit coordinates so the save
    // resolves to exactly that place (no geocode-first-result ambiguity);
    // they're cleared the moment the user types a different city
    const o = S.opts.weather || {};
    for (const k of ["lat", "lon", "place"]) {
      if (o[k] !== undefined && o[k] !== null && o[k] !== "") out[k] = o[k];
    }
  }
  return out;
}

// candidate = "preview everything that COULD render": every valid source
// enabled, every frame in rotation, legacy pickers maxed out
function candidateConfig() {
  const sources = {};
  for (const src of S.catalogue) {
    const o = optionsPayload(src.id);
    if (src.id === "network") o.stats = multiChoiceIds("network", "stats");
    if (src.id === "weather") o.show_condition = true;
    if (src.id === "shopify") o.frames = multiChoiceIds("shopify", "frames");
    sources[src.id] = { enabled: sourceGate(src.id).ok, options: o };
  }
  return { sources, rotation: Object.keys(S.frameInfo),
           write_interval_s: S.interval };
}

// save = exactly what the user built: sources enabled iff valid AND used,
// legacy pickers derived from the rotation so the feeder shows what's chosen
function savePayload() {
  const rotSet = new Set(S.rotation);
  const sources = {};
  for (const src of S.catalogue) {
    const o = optionsPayload(src.id);
    if (src.id === "network") {
      o.stats = multiChoiceIds("network", "stats")
        .filter((s) => rotSet.has(s));
    }
    if (src.id === "weather") o.show_condition = rotSet.has("weather_condition");
    if (src.id === "shopify") {
      o.frames = multiChoiceIds("shopify", "frames")
        .filter((f) => rotSet.has(f));
    }
    const used = (src.frames || []).some((f) => rotSet.has(f.id));
    sources[src.id] = { enabled: sourceGate(src.id).ok && used, options: o };
  }
  const frames = {};
  for (const [fid, st] of Object.entries(S.frameSettings)) {
    if (!S.frameInfo[fid] || !st) continue;
    const entry = {};
    if (st.dwell && st.dwell > 1) entry.dwell = st.dwell;
    if (st.window && st.window.from_hour !== st.window.to_hour) {
      entry.window = { from_hour: st.window.from_hour,
                       to_hour: st.window.to_hour };
    }
    if (Object.keys(entry).length) frames[fid] = entry;
  }
  return { sources, rotation: [...S.rotation], frames,
           write_interval_s: S.interval };
}

// ------------------------------------------------------------ dirty/save ----

function markDirty() {
  S.dirty = true;
  updateSaveBar();
}

function updateSaveBar() {
  const bar = $("save-bar");
  bar.hidden = !S.dirty;
  if (S.dirty) {
    $("save-msg").textContent =
      "Unsaved changes — your clock keeps its last saved setup until " +
      "you save.";
  }
}

async function save(fromPlay = false) {
  const btns = [$("btn-save"), $("btn-play")];
  btns.forEach((b) => { b.disabled = true; });
  try {
    const d = await api("/api/config", { method: "POST",
                                         body: savePayload() });
    S.secretsTyped = {};
    S.secretsClear = {};
    adoptServerConfig(d.config);
    S.dirty = false;
    updateSaveBar();
    rerenderAll();
    refreshSavedAvailable();
    refreshPreview();
    if (fromPlay && S.connected) {
      toast("Playing — your clock picks it up within about a minute.");
    } else if (S.connected) {
      toast("Saved — your clock updates on its next write.");
    } else {
      toast("Saved. Connect your clock any time to play it.");
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    btns.forEach((b) => { b.disabled = false; });
  }
}

function discard() {
  if (!S.serverConfig) return;
  S.secretsTyped = {};
  S.secretsClear = {};
  adoptServerConfig(S.serverConfig);
  S.dirty = false;
  updateSaveBar();
  rerenderAll();
  refreshPreview();
  toast("Changes discarded.");
}

// ------------------------------------------------------- adopt server cfg ----

function adoptServerConfig(config) {
  S.serverConfig = JSON.parse(JSON.stringify(config));
  S.opts = {};
  S.secretMeta = {};
  for (const src of S.catalogue) {
    const saved = (((config.sources || {})[src.id]) || {}).options || {};
    const o = {};
    const meta = {};
    for (const [k, v] of Object.entries(saved)) {
      if (k.endsWith("_set")) {
        const base = k.slice(0, -4);
        meta[base] = Object.assign(meta[base] || {}, { set: !!v });
      } else if (k.endsWith("_hint")) {
        const base = k.slice(0, -5);
        meta[base] = Object.assign(meta[base] || {}, { hint: v || "" });
      } else {
        o[k] = v;
      }
    }
    S.opts[src.id] = o;
    S.secretMeta[src.id] = meta;
  }
  S.rotation = (config.rotation || []).filter((fid) => S.frameInfo[fid]);
  S.frameSettings = {};
  for (const [fid, st] of Object.entries(config.frames || {})) {
    if (S.frameInfo[fid]) S.frameSettings[fid] = JSON.parse(JSON.stringify(st));
  }
  S.interval = Math.max(65, parseInt(config.write_interval_s, 10) || 65);
  S.apiAccess = config.api_access || { enabled: false };
}

// --------------------------------------------------------------- preview ----

let previewTimer = null;
let previewBusy = false;
let previewQueued = false;

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshPreview, 400);
}

async function refreshPreview() {
  if (previewBusy) { previewQueued = true; return; }
  previewBusy = true;
  try {
    const d = await api("/api/preview",
                        { method: "POST", body: { config: candidateConfig() } });
    S.preview = {};
    for (const f of (d.frames || [])) S.preview[f.name] = f;
    S.previewLoaded = true;
    const warnEl = $("library-warnings");
    const warnings = d.warnings || [];
    warnEl.hidden = warnings.length === 0;
    warnEl.textContent = warnings.join(" · ");
    updateCards();
    renderRotation();
    updatePlayView();
  } catch (e) {
    // transient - keep the last good previews on screen
  } finally {
    previewBusy = false;
    if (previewQueued) { previewQueued = false; refreshPreview(); }
  }
}

async function refreshSavedAvailable() {
  try {
    const d = await api("/api/preview");
    S.savedAvailable = new Set((d.frames || []).map((f) => f.name));
  } catch (e) { /* keep the old set */ }
}

// --------------------------------------------------------------- library ----

function renderLibrary() {
  const openNow = new Set(
    [...document.querySelectorAll("details.cat[open]")]
      .map((d) => d.dataset.cat));
  const useDefaults = openNow.size === 0 &&
    !document.querySelector("details.cat");
  const root = $("library");
  root.innerHTML = "";
  for (const cat of S.categories) {
    const srcs = S.catalogue.filter((s) => s.category === cat.id);
    if (!srcs.length) continue;
    const det = el("details", "cat");
    det.dataset.cat = cat.id;
    det.open = useDefaults ? OPEN_CATS.includes(cat.id) : openNow.has(cat.id);
    const sum = el("summary");
    sum.appendChild(el("span", "cat-name", cat.label));
    sum.appendChild(el("span", "cat-added"));
    sum.appendChild(el("span", "cat-count"));
    det.appendChild(sum);
    for (const src of srcs) det.appendChild(buildSourceBlock(src));
    root.appendChild(det);
  }
  const total = Object.keys(S.frameInfo).length;
  $("library-count").textContent = `${total} stats available`;
  updateCards();
}

function buildSourceBlock(src) {
  const block = el("div", "src");
  block.dataset.sid = src.id;

  const head = el("div", "src-head");
  const meta = el("div", "src-meta");
  meta.appendChild(el("h3", null, src.label));
  meta.appendChild(el("p", "hint", src.description));
  head.appendChild(meta);
  head.appendChild(el("span", "src-chip"));

  const hasSettings = Object.entries(src.options_schema || {})
    .some(([k]) => !(HIDDEN_KEYS[src.id] || []).includes(k));
  let panel = null;
  if (hasSettings) {
    const btn = el("button", "btn btn-small", "Settings");
    btn.setAttribute("aria-expanded", "false");
    btn.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      btn.setAttribute("aria-expanded", String(!panel.hidden));
    });
    head.appendChild(btn);
    block._settingsBtn = btn;
  }
  block.appendChild(head);

  if (hasSettings) {
    panel = buildSettingsPanel(src);
    panel.hidden = true;
    block.appendChild(panel);
    block._panel = panel;
  }

  const grid = el("div", "fgrid");
  for (const fr of src.frames) {
    grid.appendChild(buildFrameCard(fr.id, fr.label));
  }
  block.appendChild(grid);
  return block;
}

function buildFrameCard(fid, label) {
  const card = el("div", "fcard");
  card.dataset.fid = fid;

  const mockwrap = el("div", "fcard-mockwrap");
  card.appendChild(mockwrap);

  const foot = el("div", "fcard-foot");
  const lab = el("div", "fcard-label");
  const led = el("span", "led");
  led.setAttribute("aria-hidden", "true");
  lab.appendChild(led);
  lab.appendChild(document.createTextNode(label));
  lab.title = label;
  foot.appendChild(lab);

  const actions = el("div", "fcard-actions");
  const add = el("button", "btn btn-small btn-add", "+ Add");
  add.addEventListener("click", () => onCardAdd(fid));
  const test = el("button", "btn btn-small btn-ghost", "Test");
  test.title = "Show this on my clock now";
  test.addEventListener("click", () => testFrame(fid, test));
  actions.appendChild(add);
  actions.appendChild(test);
  foot.appendChild(actions);
  card.appendChild(foot);

  const note = el("div", "fcard-note");
  note.hidden = true;
  card.appendChild(note);
  return card;
}

function onCardAdd(fid) {
  const gate = frameGate(fid);
  if (!gate.ok) { openSourceSettings(gate.sid); return; }
  if (S.rotation.includes(fid)) {
    S.rotation = S.rotation.filter((f) => f !== fid);
  } else {
    S.rotation.push(fid);
  }
  markDirty();
  updateCards();
  renderRotation();
  updatePlayView();
}

function openSourceSettings(sid) {
  const block = document.querySelector(`.src[data-sid="${sid}"]`);
  if (!block) return;
  const det = block.closest("details.cat");
  if (det) det.open = true;
  if (block._panel) {
    block._panel.hidden = false;
    if (block._settingsBtn) {
      block._settingsBtn.setAttribute("aria-expanded", "true");
    }
  }
  block.scrollIntoView({ behavior: "smooth", block: "center" });
  const first = block._panel &&
    block._panel.querySelector("input, select");
  if (first) setTimeout(() => first.focus({ preventScroll: true }), 350);
}

function updateCards() {
  const rotSet = new Set(S.rotation);
  const addedByCat = {};
  for (const fid of S.rotation) {
    const info = S.frameInfo[fid];
    if (info) addedByCat[info.category] = (addedByCat[info.category] || 0) + 1;
  }

  for (const card of document.querySelectorAll(".fcard")) {
    const fid = card.dataset.fid;
    const gate = frameGate(fid);
    const prev = S.preview[fid];
    const inRot = rotSet.has(fid);

    card.classList.toggle("added", inRot);
    card.classList.toggle("blocked", !gate.ok);
    card.classList.toggle("unavail", gate.ok && S.previewLoaded && !prev);

    const mockwrap = card.querySelector(".fcard-mockwrap");
    mockwrap.replaceChildren(
      prev ? renderMock(prev.slots, prev.color)
           : placeholderMock(gate.ok ? (S.previewLoaded ? "no data" : "")
                                     : "set up"));

    const led = card.querySelector(".led");
    led.style.color = prev ? ledInfo(prev.color).css : "#3a3f52";

    const add = card.querySelector(".btn-add");
    if (!gate.ok) {
      add.textContent = "Set up";
      add.classList.remove("is-added");
      add.title = gate.reason || "Needs setup first";
    } else if (inRot) {
      add.textContent = "✓ Added";
      add.classList.add("is-added");
      add.title = "In your rotation — click to remove";
    } else {
      add.textContent = "+ Add";
      add.classList.remove("is-added");
      add.title = "Add to your rotation";
    }

    const test = card.querySelector(".btn-ghost");
    test.hidden = !S.connected;

    const note = card.querySelector(".fcard-note");
    if (!gate.ok) {
      note.hidden = false;
      note.classList.remove("gray");
      note.textContent = gate.reason;
    } else if (S.previewLoaded && !prev) {
      note.hidden = false;
      note.classList.add("gray");
      note.textContent = "No data right now — it comes back when the " +
                         "source answers.";
    } else {
      note.hidden = true;
    }
  }

  // per-source chips + category counters
  for (const block of document.querySelectorAll(".src")) {
    const sid = block.dataset.sid;
    const src = S.bySource[sid];
    const gate = sourceGate(sid);
    const chip = block.querySelector(".src-chip");
    const added = (src.frames || []).filter((f) => rotSet.has(f.id)).length;
    if (!gate.ok) {
      chip.textContent = "Needs setup";
      chip.className = "src-chip needs";
      chip.title = gate.reason || "";
    } else if (added) {
      chip.textContent = `${added} in rotation`;
      chip.className = "src-chip ok";
      chip.title = "";
    } else {
      chip.textContent = "Ready";
      chip.className = "src-chip";
      chip.title = "";
    }
  }
  for (const det of document.querySelectorAll("details.cat")) {
    const cat = det.dataset.cat;
    const srcs = S.catalogue.filter((s) => s.category === cat);
    const total = srcs.reduce((n, s) => n + s.frames.length, 0);
    const added = addedByCat[cat] || 0;
    det.querySelector(".cat-count").textContent =
      `${total} stat${total === 1 ? "" : "s"}`;
    const badge = det.querySelector(".cat-added");
    badge.hidden = added === 0;
    badge.textContent = `${added} added`;
  }
}

// -------------------------------------------------------- settings forms ----

function buildSettingsPanel(src) {
  const sid = src.id;
  const panel = el("div", "src-settings");
  const rows = el("div", "row-wrap");
  panel.appendChild(rows);

  let advancedWrap = null;
  if (sid === "node") {
    advancedWrap = el("details", "advanced");
    advancedWrap.appendChild(el("summary", null,
      "Advanced · read Bitcoin Core / LND directly"));
    advancedWrap._rows = el("div", "row-wrap");
    advancedWrap.appendChild(advancedWrap._rows);
  }

  let hasSecret = false;
  for (const [key, meta] of orderedSchemaEntries(src)) {
    if ((HIDDEN_KEYS[sid] || []).includes(key)) continue;
    const target = (sid === "node" && NODE_ADVANCED.includes(key))
      ? advancedWrap._rows : rows;
    if (meta.type === "password") {
      hasSecret = true;
      target.appendChild(buildSecretField(sid, key, meta));
    } else {
      target.appendChild(buildOptionField(sid, key, meta));
    }
  }
  if (advancedWrap) panel.appendChild(advancedWrap);
  if (hasSecret) {
    panel.appendChild(el("p", "secret-note",
      "Stays on your Umbrel; only used to read your data. Never shown " +
      "again once saved."));
  }
  return panel;
}

function buildOptionField(sid, key, meta) {
  const field = el("label", "field");
  field.appendChild(document.createTextNode(meta.label || key));
  const o = S.opts[sid] || (S.opts[sid] = {});
  let input;

  if (meta.type === "select") {
    input = document.createElement("select");
    const fill = (choices) => {
      input.innerHTML = "";
      for (const c of choices) {
        const id = (typeof c === "object") ? c.id : c;
        const label = (typeof c === "object") ? (c.label || c.id) : c;
        const opt = el("option", null,
                       UNIT_LABELS[id] && sid === "weather" && key === "units"
                         ? UNIT_LABELS[id] : label);
        opt.value = id;
        input.appendChild(opt);
      }
    };
    if (sid === "price" && key === "currency") {
      fill(currentExchangeCurrencies());
      input.dataset.role = "price-currency";
    } else {
      fill(meta.choices || []);
    }
    input.value = (o[key] !== undefined && o[key] !== null)
      ? String(o[key]) : String(meta.default ?? "");
    if (!input.value && input.options.length) input.selectedIndex = 0;
    input.addEventListener("change", () => {
      o[key] = input.value;
      if (sid === "price" && key === "exchange") onExchangeChange();
      onOptionEdited(sid);
    });
  } else if (meta.type === "number") {
    input = document.createElement("input");
    input.type = "number";
    if (meta.min !== undefined) input.min = meta.min;
    if (meta.max !== undefined) input.max = meta.max;
    input.value = (o[key] !== undefined && o[key] !== null)
      ? o[key] : (meta.default ?? 0);
    input.addEventListener("change", () => {
      const v = parseFloat(input.value);
      o[key] = Number.isFinite(v) ? v : (meta.default ?? 0);
      onOptionEdited(sid);
    });
  } else if (meta.type === "bool") {
    field.className = "field";
    const wrap = el("div");
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = (o[key] !== undefined) ? !!o[key] : !!meta.default;
    input.addEventListener("change", () => {
      o[key] = input.checked;
      onOptionEdited(sid);
    });
    wrap.appendChild(input);
    field.appendChild(wrap);
    return field;
  } else { // text
    input = document.createElement("input");
    input.type = (sid === "space" && key === "countdown_date")
      ? "date" : "text";
    if (meta.placeholder) input.placeholder = meta.placeholder;
    input.autocomplete = "off";
    input.spellcheck = false;
    input.value = (o[key] !== undefined && o[key] !== null) ? o[key] : "";
    if (!input.value && input.type === "text" && meta.default) {
      input.value = meta.default;
      o[key] = meta.default;
    }
    const commit = () => {
      o[key] = input.value.trim();
      validateTextField(sid, key, field, input);
      onOptionEdited(sid);
    };
    input.addEventListener("input", commit);
  }
  field.appendChild(input);

  if (sid === "weather" && key === "city") attachCityTypeahead(field, input);

  // per-exchange note (Binance USDT, Peach P2P rate)
  if (sid === "price" && key === "exchange") {
    const note = el("span", "opt-note");
    note.dataset.role = "exchange-note";
    field.appendChild(note);
    setTimeout(updateExchangeNote, 0);
  }
  if (sid === "shopify" && key === "shop_domain") {
    field.appendChild(el("span", "opt-note",
      "Use your .myshopify.com domain — that's where the Admin API " +
      "lives."));
  }
  return field;
}

// city typeahead (weather) ----------------------------------------------------
// GET /api/geocode?q= returns [{name, admin1, country, country_code,
// latitude, longitude}]. Picking one stores the exact coordinates alongside
// the city; typing again clears them so the save falls back to the server's
// geocode-first-result behaviour, exactly as before.

function attachCityTypeahead(field, input) {
  field.classList.add("typeahead");
  const list = el("ul", "ta-list");
  list.hidden = true;
  const note = el("span", "resolved-place");
  field.append(list, note);
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");

  const opts = () => S.opts.weather || (S.opts.weather = {});
  let items = [];
  let active = -1;
  let timer = null;
  let seq = 0;

  const syncNote = () => {
    const o = opts();
    const has = o.lat !== undefined && o.lat !== null && o.place;
    note.hidden = !has;
    note.textContent = has ? `Weather for: ${o.place}` : "";
  };
  syncNote();

  const close = () => {
    list.hidden = true;
    list.innerHTML = "";
    items = [];
    active = -1;
    input.setAttribute("aria-expanded", "false");
  };

  const choose = (m) => {
    const o = opts();
    o.city = m.name;
    o.lat = m.latitude;
    o.lon = m.longitude;
    o.place = m.country_code ? `${m.name}, ${m.country_code}` : m.name;
    input.value = m.name;
    close();
    syncNote();
    onOptionEdited("weather");
  };

  const render = () => {
    list.innerHTML = "";
    items.forEach((m, i) => {
      const li = el("li", "ta-item" + (i === active ? " active" : ""));
      li.appendChild(document.createTextNode(m.name));
      const sub = [m.admin1, m.country].filter(Boolean).join(", ");
      if (sub) li.appendChild(el("span", "ta-sub", sub));
      // pointerdown fires before the input's blur closes the list
      li.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        choose(m);
      });
      list.appendChild(li);
    });
    list.hidden = items.length === 0;
    input.setAttribute("aria-expanded", String(!list.hidden));
  };

  const search = async () => {
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    const mine = ++seq;
    try {
      const d = await api("/api/geocode?q=" + encodeURIComponent(q));
      if (mine !== seq) return;  // superseded by a newer keystroke
      items = d.results || [];
      active = -1;
      render();
    } catch (e) { /* fail soft - save still geocodes the typed name */ }
  };

  input.addEventListener("input", () => {
    // typing invalidates any picked coordinates
    const o = opts();
    delete o.lat;
    delete o.lon;
    delete o.place;
    syncNote();
    clearTimeout(timer);
    timer = setTimeout(search, 250);
  });
  input.addEventListener("keydown", (e) => {
    if (list.hidden) {
      if (e.key === "ArrowDown") search();
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      active = (active + step + items.length) % items.length;
      render();
    } else if (e.key === "Enter") {
      if (active >= 0 && items[active]) {
        e.preventDefault();
        choose(items[active]);
      }
    } else if (e.key === "Escape") {
      close();
    }
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
}

function validateTextField(sid, key, field, input) {
  let err = field.querySelector(".field-err");
  let msg = "";
  const v = input.value.trim();
  if ((sid === "node" || sid === "btcpay") && key === "base_url" &&
      v && !/^https?:\/\//.test(v)) {
    msg = "Must start with http:// or https://";
  }
  if (sid === "novelty" && key === "github_repo" && v &&
      !/^[^\s/]+\/[^\s/]+$/.test(v)) {
    msg = "Use the owner/name form, e.g. bitcoin/bitcoin";
  }
  if (msg) {
    if (!err) { err = el("span", "field-err"); field.appendChild(err); }
    err.textContent = msg;
  } else if (err) {
    err.remove();
  }
}

function currentExchangeCurrencies() {
  const schema = (S.bySource.price || {}).options_schema || {};
  const choices = (schema.exchange || {}).choices || [];
  const ex = ((S.opts.price || {}).exchange) || "coinbase";
  const spec = choices.find((c) => c.id === ex);
  return (spec && spec.currencies) ? spec.currencies : S.currencies;
}

function onExchangeChange() {
  const sel = document.querySelector('[data-role="price-currency"]');
  if (sel) {
    const ccys = currentExchangeCurrencies();
    const cur = (S.opts.price || {}).currency || "USD";
    sel.innerHTML = "";
    for (const c of ccys) {
      const opt = el("option", null, c);
      opt.value = c;
      sel.appendChild(opt);
    }
    sel.value = ccys.includes(cur) ? cur : ccys[0];
    S.opts.price.currency = sel.value;
  }
  updateExchangeNote();
}

function updateExchangeNote() {
  const note = document.querySelector('[data-role="exchange-note"]');
  if (!note) return;
  const schema = (S.bySource.price || {}).options_schema || {};
  const choices = (schema.exchange || {}).choices || [];
  const spec = choices.find((c) => c.id === ((S.opts.price || {}).exchange
                                             || "coinbase"));
  note.textContent = (spec && spec.note) ? spec.note : "";
  note.hidden = !note.textContent;
}

function onOptionEdited(sid) {
  markDirty();
  schedulePreview();
  updateCards();       // validity chips react immediately
  renderRotation();    // hidden/visible rows may change
}

// secret (write-only) fields --------------------------------------------------

function buildSecretField(sid, key, meta) {
  const field = el("div", "field secret");
  field.dataset.secret = `${sid}:${key}`;
  field.appendChild(el("span", null, meta.label || key));

  const saved = el("div", "secret-saved");
  const savedTxt = el("span");
  const code = el("code");
  savedTxt.appendChild(document.createTextNode("Saved "));
  savedTxt.appendChild(code);
  const btnChange = el("button", "btn btn-small", "Change");
  const btnClear = el("button", "btn btn-small btn-ghost", "Clear");
  saved.append(savedTxt, btnChange, btnClear);

  const clearing = el("div", "secret-clearing");
  const btnUndo = el("button", "btn btn-small btn-ghost", "Undo");
  clearing.append(
    document.createTextNode("Will be removed when you save. "), btnUndo);

  const input = document.createElement("input");
  input.type = "password";
  input.autocomplete = "new-password";
  input.spellcheck = false;
  input.placeholder = meta.placeholder || "";
  input.setAttribute("aria-label", meta.label || key);

  field.append(saved, clearing, input);

  let changing = false;
  const sync = () => {
    const m = ((S.secretMeta[sid] || {})[key]) || {};
    const cleared = !!((S.secretsClear[sid] || {})[key]);
    const typed = !!((S.secretsTyped[sid] || {})[key]);
    const showSaved = m.set && !cleared && !typed && !changing;
    saved.hidden = !showSaved;
    clearing.hidden = !cleared;
    input.hidden = showSaved || cleared;
    code.textContent = m.hint || "••••";
  };
  field._sync = sync;

  input.addEventListener("input", () => {
    const t = S.secretsTyped[sid] || (S.secretsTyped[sid] = {});
    const v = input.value.trim();
    if (v) t[key] = v; else delete t[key];
    markDirty();
    schedulePreview();
    updateCards();
    renderRotation();
  });
  btnChange.addEventListener("click", () => {
    changing = true;
    sync();
    input.focus();
  });
  btnClear.addEventListener("click", () => {
    (S.secretsClear[sid] || (S.secretsClear[sid] = {}))[key] = true;
    const t = S.secretsTyped[sid] || {};
    delete t[key];
    input.value = "";
    changing = false;
    sync();
    markDirty();
    updateCards();
    renderRotation();
    toast("It will be removed when you save.");
  });
  btnUndo.addEventListener("click", () => {
    delete (S.secretsClear[sid] || {})[key];
    changing = false;
    sync();
    markDirty();
    updateCards();
    renderRotation();
  });

  sync();
  return field;
}

function syncSecretWidgets() {
  for (const f of document.querySelectorAll("[data-secret]")) {
    if (f._sync) f._sync();
  }
}

// -------------------------------------------------------------- rotation ----

function renderRotation() {
  const list = $("rotation-list");
  list.innerHTML = "";
  const visible = visibleRotation();
  $("rotation-empty").hidden = S.rotation.length > 0;

  // stats saved in the rotation but waiting on source setup
  const hiddenIds = S.rotation.filter((fid) => !frameGate(fid).ok);
  const noteEl = $("rotation-hidden-note");
  if (hiddenIds.length) {
    const bySrc = {};
    for (const fid of hiddenIds) {
      const label = (S.bySource[S.frameInfo[fid].sid] || {}).label || "?";
      bySrc[label] = (bySrc[label] || 0) + 1;
    }
    noteEl.hidden = false;
    noteEl.textContent =
      `${hiddenIds.length} saved stat${hiddenIds.length === 1 ? " is" : "s are"} ` +
      `waiting on setup: ${Object.keys(bySrc).join(", ")}. ` +
      "They join the rotation once their source is set up.";
  } else {
    noteEl.hidden = true;
  }

  visible.forEach((fid, i) => {
    list.appendChild(buildRotationRow(fid, i, visible.length));
  });
  updateLoopEstimate();
}

function buildRotationRow(fid, index, count) {
  const info = S.frameInfo[fid];
  const prev = S.preview[fid];
  const li = el("li", "rrow");
  li.dataset.fid = fid;
  li.draggable = true;

  const grip = el("span", "grip", "⋮⋮");
  grip.title = "Drag to reorder";
  li.appendChild(grip);

  const order = el("div", "rr-order");
  const up = el("button", null, "▲");
  up.title = "Move up";
  up.setAttribute("aria-label", `Move ${info.label} up`);
  up.disabled = index === 0;
  up.addEventListener("click", () => moveVisible(fid, -1));
  const down = el("button", null, "▼");
  down.title = "Move down";
  down.setAttribute("aria-label", `Move ${info.label} down`);
  down.disabled = index === count - 1;
  down.addEventListener("click", () => moveVisible(fid, +1));
  order.append(up, down);
  li.appendChild(order);

  const main = el("div", "rr-main");
  const top = el("div", "rr-top");
  const led = el("span", "led");
  led.setAttribute("aria-hidden", "true");
  led.style.color = prev ? ledInfo(prev.color).css : "#3a3f52";
  top.appendChild(led);
  top.appendChild(el("strong", null, info.label));
  top.appendChild(el("span", "rr-src",
                     (S.bySource[info.sid] || {}).label || info.sid));
  main.appendChild(top);

  const mid = el("div", "rr-midrow");
  const mockslot = el("div", "rr-mock");
  mockslot.appendChild(prev ? renderMock(prev.slots, prev.color, "mock-sm")
                            : placeholderMock("no data", "mock-sm"));
  mid.appendChild(mockslot);

  const ctl = el("div", "rr-ctl");
  const st = S.frameSettings[fid] || {};

  // dwell
  const dwellLab = el("label");
  dwellLab.appendChild(document.createTextNode("Hold"));
  const dwell = document.createElement("input");
  dwell.type = "number";
  dwell.className = "rr-dwell";
  dwell.min = 1; dwell.max = 48;
  dwell.value = st.dwell || 1;
  dwell.setAttribute("aria-label", `Screens to hold ${info.label}`);
  const eta = el("span", "rr-dwell-eta");
  const setEta = () => {
    const n = Math.max(1, Math.min(48, parseInt(dwell.value, 10) || 1));
    eta.textContent = `screen${n > 1 ? "s" : ""} ≈ ` +
      `${Math.round(n * S.interval)}s`;
  };
  setEta();
  dwell.addEventListener("change", () => {
    let n = parseInt(dwell.value, 10);
    if (!Number.isFinite(n)) n = 1;
    n = Math.max(1, Math.min(48, n));
    dwell.value = n;
    const cur = S.frameSettings[fid] || (S.frameSettings[fid] = {});
    if (n > 1) cur.dwell = n; else delete cur.dwell;
    setEta();
    updateLoopEstimate();
    markDirty();
  });
  dwellLab.append(dwell, eta);
  ctl.appendChild(dwellLab);

  // window
  const winBtn = el("button", "rr-winbtn");
  const winRow = el("div", "rr-win");
  winRow.hidden = true;
  const selFrom = hourSelect(`Show ${info.label} from hour`);
  const selTo = hourSelect(`Show ${info.label} until hour`);
  const clearBtn = el("button", "btn btn-small btn-ghost", "All day");
  const winLabel = () => {
    const w = (S.frameSettings[fid] || {}).window;
    if (!w || w.from_hour === w.to_hour) return "When: all day";
    const wrap = w.from_hour > w.to_hour ? " (overnight)" : "";
    return `When: ${fmtHour(w.from_hour)}–${fmtHour(w.to_hour)}${wrap}`;
  };
  const syncWin = () => {
    const w = (S.frameSettings[fid] || {}).window;
    winBtn.textContent = winLabel();
    winBtn.classList.toggle("active", !!w && w.from_hour !== w.to_hour);
    selFrom.value = w ? w.from_hour : 6;
    selTo.value = w ? w.to_hour : 9;
  };
  const commitWin = () => {
    const f = parseInt(selFrom.value, 10);
    const t = parseInt(selTo.value, 10);
    const cur = S.frameSettings[fid] || (S.frameSettings[fid] = {});
    if (f === t) delete cur.window;
    else cur.window = { from_hour: f, to_hour: t };
    syncWin();
    markDirty();
  };
  winBtn.addEventListener("click", () => {
    winRow.hidden = !winRow.hidden;
    if (!winRow.hidden && !(S.frameSettings[fid] || {}).window) {
      commitWin(); // opening it applies the default 6am-9am starting point
    }
  });
  selFrom.addEventListener("change", commitWin);
  selTo.addEventListener("change", commitWin);
  clearBtn.addEventListener("click", () => {
    const cur = S.frameSettings[fid] || {};
    delete cur.window;
    winRow.hidden = true;
    syncWin();
    markDirty();
  });
  winRow.append(el("span", null, "from"), selFrom,
                el("span", null, "to"), selTo, clearBtn);
  syncWin();
  ctl.appendChild(winBtn);
  mid.appendChild(ctl);
  main.appendChild(mid);
  main.appendChild(winRow);
  li.appendChild(main);

  const actions = el("div", "rr-actions");
  if (S.connected) {
    const test = el("button", "btn btn-small btn-ghost", "Test");
    test.title = "Show this on my clock now";
    test.addEventListener("click", () => testFrame(fid, test));
    actions.appendChild(test);
  }
  const rm = el("button", "rr-remove", "✕");
  rm.title = "Remove from rotation";
  rm.setAttribute("aria-label", `Remove ${info.label} from rotation`);
  rm.addEventListener("click", () => {
    S.rotation = S.rotation.filter((f) => f !== fid);
    markDirty();
    renderRotation();
    updateCards();
    updatePlayView();
  });
  actions.appendChild(rm);
  li.appendChild(actions);

  // drag to reorder
  li.addEventListener("dragstart", (e) => {
    li.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", fid); } catch (err) { /* ok */ }
  });
  li.addEventListener("dragend", () => {
    li.classList.remove("dragging");
    commitDomOrder();
  });
  return li;
}

function hourSelect(aria) {
  const sel = document.createElement("select");
  sel.setAttribute("aria-label", aria);
  for (let h = 0; h < 24; h++) {
    const o = el("option", null, fmtHour(h));
    o.value = h;
    sel.appendChild(o);
  }
  return sel;
}

function moveVisible(fid, delta) {
  const visible = visibleRotation();
  const i = visible.indexOf(fid);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= visible.length) return;
  const a = S.rotation.indexOf(visible[i]);
  const b = S.rotation.indexOf(visible[j]);
  [S.rotation[a], S.rotation[b]] = [S.rotation[b], S.rotation[a]];
  markDirty();
  renderRotation();
  updatePlayView();
}

function commitDomOrder() {
  const domOrder = [...$("rotation-list").querySelectorAll(".rrow")]
    .map((li) => li.dataset.fid);
  const hidden = S.rotation.filter((fid) => !domOrder.includes(fid));
  const next = [...domOrder, ...hidden];
  if (next.join() !== S.rotation.join()) {
    S.rotation = next;
    markDirty();
    updatePlayView();
  }
  renderRotation();
}

function initRotationDnD() {
  const list = $("rotation-list");
  list.addEventListener("dragover", (e) => {
    e.preventDefault();
    const dragging = list.querySelector(".dragging");
    if (!dragging) return;
    const after = [...list.querySelectorAll(".rrow:not(.dragging)")]
      .find((row) => {
        const r = row.getBoundingClientRect();
        return e.clientY < r.top + r.height / 2;
      });
    if (after) list.insertBefore(dragging, after);
    else list.appendChild(dragging);
  });
  list.addEventListener("drop", (e) => e.preventDefault());
}

function updateLoopEstimate() {
  const visible = visibleRotation();
  const elEst = $("loop-estimate");
  if (!visible.length) { elEst.textContent = ""; return; }
  const screens = visible.reduce(
    (n, fid) => n + ((S.frameSettings[fid] || {}).dwell || 1), 0);
  const secs = screens * S.interval;
  const mins = secs / 60;
  const t = mins >= 1.5 ? `≈ ${Math.round(mins)} min per full loop`
                        : `≈ ${Math.round(secs)}s per full loop`;
  elEst.textContent = `${visible.length} stat${visible.length === 1 ? "" : "s"}` +
                      ` · ${t}`;
}

// ----------------------------------------------------------- play preview ----

let playIdx = 0;

function playFrames() {
  return visibleRotation().filter((fid) => S.preview[fid]);
}

function updatePlayView() {
  const frames = playFrames();
  const empty = frames.length === 0;
  $("play-empty").hidden = !empty;
  $("play-live").hidden = empty;
  if (empty) return;
  if (playIdx >= frames.length) playIdx = 0;
  const fid = frames[playIdx];
  const prev = S.preview[fid];
  $("play-mock").replaceChildren(renderMock(prev.slots, prev.color, "mock-lg"));
  $("play-label").textContent = prev.label || S.frameInfo[fid].label;
  $("play-pos").textContent = frames.length > 1
    ? `${playIdx + 1} of ${frames.length} · cycling preview`
    : "the only stat in your rotation";
  $("btn-play").textContent = S.connected
    ? (!S.dirty && S.running ? "Playing — rotation is live"
                             : "Play my rotation on the clock")
    : "Save my rotation";
  $("btn-play-test").hidden = !S.connected;
  $("btn-play-test").dataset.fid = fid;
}

function advancePlay() {
  const frames = playFrames();
  if (frames.length < 2 || document.hidden) return;
  playIdx = (playIdx + 1) % frames.length;
  updatePlayView();
}

// ----------------------------------------------------------------- test ----

async function testFrame(fid, btn) {
  if (!S.connected) {
    toast("Connect your clock first (step 1).", true);
    return;
  }
  if (!S.savedAvailable.has(fid)) {
    toast("Save first — then your clock can build this stat.", true);
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const d = await api("/api/test", { method: "POST",
                                       body: { frame: fid } });
    toast(d.note || "Sent to your clock.");
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) setTimeout(() => { btn.disabled = false; }, 3000);
  }
}

// ------------------------------------------------------------- connect ----

async function scan() {
  const btn = $("btn-scan");
  const status = $("scan-status");
  const results = $("scan-results");
  btn.disabled = true;
  results.innerHTML = "";
  status.hidden = false;
  status.innerHTML =
    '<span class="spinner"></span>Scanning — this takes a few seconds…';
  try {
    const d = await api("/api/discover", {
      method: "POST", body: { subnet: $("subnet").value.trim() },
    });
    if (!d.clocks.length) {
      status.textContent = "No clocks found on that subnet. Double-check " +
        "the subnet, or enter the clock's IP below (it's on the clock " +
        "under Settings → Info).";
      return;
    }
    status.textContent =
      `Found ${d.clocks.length} clock${d.clocks.length > 1 ? "s" : ""}:`;
    for (const clk of d.clocks) {
      const li = el("li");
      const meta = el("div", "meta");
      meta.appendChild(el("strong", null, clk.model));
      meta.appendChild(el("span", null,
                          `${clk.ip} · firmware ${clk.version}`));
      const btn2 = el("button", "btn btn-primary btn-small", "Connect");
      btn2.addEventListener("click", () => connect(clk.ip, btn2));
      li.append(meta, btn2);
      results.appendChild(li);
    }
  } catch (e) {
    status.textContent = "";
    status.hidden = true;
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function connect(ip, btn) {
  const err = $("connect-error");
  err.hidden = true;
  if (btn) btn.disabled = true;
  try {
    const d = await api("/api/connect", { method: "POST", body: { ip } });
    toast(`Connected to ${d.clock.model} at ${d.clock.ip}`);
    await pollState();
    updateCards();
    renderRotation();
    updatePlayView();
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function disconnect() {
  try {
    const d = await api("/api/disconnect", { method: "POST", body: {} });
    toast(d.note || "Disconnected.");
    await pollState();
    updateCards();
    renderRotation();
    updatePlayView();
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------- state ----

function applyStateView(d) {
  S.connected = d.connected || null;
  S.running = !!d.running;
  S.nextWriteS = d.next_write_in_s;
  $("offline-pill").hidden = !d.offline;

  const subnetEl = $("subnet");
  if (!subnetEl.value && d.suggested_subnet) {
    subnetEl.value = d.suggested_subnet;
  }

  const pill = $("status-pill");
  if (d.connected) {
    $("connect-form").hidden = true;
    $("connect-done").hidden = false;
    $("clock-model").textContent = d.connected.model;
    $("clock-detail").textContent =
      `${d.connected.ip} · firmware ${d.connected.version}`;
    if (d.running) {
      pill.textContent = "Driving your clock";
      pill.className = "pill pill-on";
      $("driving-note").textContent = "This app is driving the display.";
    } else {
      pill.textContent = "Connected · idle";
      pill.className = "pill pill-idle";
      $("driving-note").textContent =
        "Add some stats below and save to start driving the display.";
    }
  } else {
    $("connect-form").hidden = false;
    $("connect-done").hidden = true;
    pill.textContent = "Not connected";
    pill.className = "pill pill-off";
  }

  const box = $("now-box");
  if (d.connected && d.running && d.last_frame) {
    box.hidden = false;
    $("now-label").textContent = d.last_frame.label || d.last_frame.name;
    $("now-mock").replaceChildren(
      renderMock(d.last_frame.slots || [], d.last_frame.color, "mock-sm"));
    renderCountdown();
  } else {
    box.hidden = true;
  }
}

function renderCountdown() {
  const n = S.nextWriteS;
  $("now-countdown").textContent =
    (n === null || n === undefined) ? "" :
    (n <= 1 ? "next change any moment"
            : `next change in ~${Math.max(0, Math.round(n))}s`);
}

function tickCountdown() {
  if (typeof S.nextWriteS === "number" && S.nextWriteS > 0) {
    S.nextWriteS -= 1;
    if (!$("now-box").hidden) renderCountdown();
  }
}

let hadConnection = null;
let hadRunning = null;
async function pollState() {
  let d;
  try {
    d = await api("/api/state");
  } catch (e) {
    return; // transient; the next poll retries
  }
  applyStateView(d);
  // connection just changed -> Test buttons / row actions need re-rendering
  const nowConn = !!d.connected;
  if (hadConnection !== null && nowConn !== hadConnection) {
    updateCards();
    renderRotation();
    updatePlayView();
  } else if (hadRunning !== null && !!d.running !== hadRunning) {
    updatePlayView();
  }
  hadConnection = nowConn;
  hadRunning = !!d.running;
}

// -------------------------------------------------- agent / API access ----
// The card only ever holds the REDACTED view (enabled / token_set). The raw
// token is fetched on demand from /api/access-token - the one deliberate
// reveal - shown or copied, and dropped again; it is never kept in S.

const TOKEN_MASK = "••••••••••";
let agentRevealed = false;

function hideAgentToken() {
  agentRevealed = false;
  const code = $("agent-token");
  if (code) code.textContent = TOKEN_MASK;
  const btn = $("btn-token-reveal");
  if (btn) btn.textContent = "Reveal";
}

function syncAgentCard() {
  const acc = S.apiAccess || { enabled: false };
  const on = !!acc.enabled;
  $("agent-enabled").checked = on;
  $("agent-on").hidden = !on;
  const pill = $("agent-pill");
  pill.textContent = on ? "On" : "Off";
  pill.className = "pill " + (on ? "pill-on" : "pill-off");
  $("agent-base").textContent = location.origin;
  hideAgentToken();
}

async function fetchAgentToken() {
  const d = await api("/api/access-token");
  return d.token || "";
}

async function setAgentAccess(body) {
  const d = await api("/api/access", { method: "POST", body });
  S.apiAccess = d.api_access || { enabled: false };
  if (S.serverConfig) S.serverConfig.api_access = S.apiAccess;
  syncAgentCard();
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    // clipboard API can be unavailable on plain-http LAN pages
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e2) { /* no */ }
    ta.remove();
    return ok;
  }
}

function initAgentCard() {
  $("agent-enabled").addEventListener("change", async (e) => {
    const want = e.currentTarget.checked;
    try {
      await setAgentAccess({ enabled: want });
      toast(want
        ? "API access is ON — reveal the token and hand it to your assistant."
        : "API access is OFF — the agent endpoints answer nothing now.");
    } catch (err) {
      e.currentTarget.checked = !want;
      toast(err.message, true);
    }
  });
  $("btn-token-reveal").addEventListener("click", async (e) => {
    if (agentRevealed) { hideAgentToken(); return; }
    try {
      const token = await fetchAgentToken();
      if (!token) { toast("No token yet — turn API access on first.", true); return; }
      $("agent-token").textContent = token;
      agentRevealed = true;
      e.currentTarget.textContent = "Hide";
    } catch (err) {
      toast(err.message, true);
    }
  });
  $("btn-token-copy").addEventListener("click", async () => {
    try {
      const token = await fetchAgentToken();
      if (!token) { toast("No token yet — turn API access on first.", true); return; }
      toast((await copyText(token))
        ? "Token copied to your clipboard."
        : "Couldn't copy — use Reveal and copy it by hand.", false);
    } catch (err) {
      toast(err.message, true);
    }
  });
  $("btn-token-regen").addEventListener("click", async () => {
    if (!window.confirm("Regenerate the token? Anything using the current " +
                        "token stops working immediately.")) return;
    try {
      await setAgentAccess({ regenerate: true });
      toast("New token generated — the old one no longer works.");
    } catch (err) {
      toast(err.message, true);
    }
  });
}

// ---------------------------------------------------------------- init ----

function rerenderAll() {
  renderLibrary();
  renderRotation();
  updatePlayView();
  syncSecretWidgets();
  syncAgentCard();
  $("interval").value = S.interval;
}

function addStarterSet() {
  let added = 0;
  for (const fid of STARTER_SET) {
    if (S.frameInfo[fid] && frameGate(fid).ok && !S.rotation.includes(fid)) {
      S.rotation.push(fid);
      added++;
    }
  }
  if (added) {
    markDirty();
    updateCards();
    renderRotation();
    updatePlayView();
    toast(`Added ${added} starter stats — rearrange them, then save.`);
  }
}

async function init() {
  $("btn-scan").addEventListener("click", scan);
  $("btn-connect").addEventListener("click",
    () => connect($("manual-ip").value.trim(), $("btn-connect")));
  $("manual-ip").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      connect($("manual-ip").value.trim(), $("btn-connect"));
    }
  });
  $("btn-disconnect").addEventListener("click", disconnect);
  $("btn-save").addEventListener("click", () => save(false));
  $("btn-discard").addEventListener("click", discard);
  $("btn-play").addEventListener("click", () => {
    if (S.dirty || !S.connected || !S.running) save(true);
    else toast("Already playing — your rotation is live on the clock.");
  });
  $("btn-play-test").addEventListener("click", (e) => {
    const fid = e.currentTarget.dataset.fid;
    if (fid) testFrame(fid, e.currentTarget);
  });
  $("btn-starter").addEventListener("click", addStarterSet);
  initAgentCard();
  $("interval").addEventListener("change", () => {
    let v = parseInt($("interval").value, 10);
    if (!Number.isFinite(v)) v = 65;
    v = Math.max(65, v);
    $("interval").value = v;
    S.interval = v;
    markDirty();
    renderRotation(); // dwell ETAs + loop estimate use the interval
  });
  initRotationDnD();

  try {
    const d = await api("/api/sources");
    S.catalogue = d.sources || [];
    S.categories = d.categories || [];
    S.currencies = d.currencies || [];
    for (const src of S.catalogue) {
      S.bySource[src.id] = src;
      for (const f of src.frames) {
        S.frameInfo[f.id] = { id: f.id, label: f.label,
                              category: f.category, sid: src.id };
      }
    }
  } catch (e) {
    toast("Can't reach the app — is the container running? " + e.message,
          true);
    return;
  }

  try {
    const st = await api("/api/state");
    adoptServerConfig(st.config);
    applyStateView(st);
    hadConnection = !!st.connected;
  } catch (e) {
    adoptServerConfig({ sources: {}, rotation: [], frames: {},
                        write_interval_s: 65 });
  }

  rerenderAll();
  refreshSavedAvailable();
  refreshPreview();

  setInterval(pollState, 5000);
  setInterval(tickCountdown, 1000);
  setInterval(advancePlay, 2800);
}

document.addEventListener("DOMContentLoaded", init);
