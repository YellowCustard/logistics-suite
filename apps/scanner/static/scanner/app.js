// Scanner PWA: offline-first warehouse and driver actions over /api/v1/sync.
//
// Every write is a command with a device-generated id, queued in IndexedDB and pushed in order whenever the
// device is online. The server applies each command once (a retry gets the stored outcome), so a lost
// response or a flaky connection never double-books stock. Reference data (bins, items, ASNs, tasks,
// counts) is pulled per site and kept locally so the floor keeps working without Wi-Fi. Drivers get their own
// menu: their trips are cached on the device, and signatures and photos wait in IndexedDB until there is signal,
// then upload before the proof of delivery that names them.
"use strict";

(() => {
  const API = "/api/v1";
  const CSRF_COOKIE = document.body.dataset.csrfCookie || "csrftoken";
  const SYNC_EVERY_MS = 15000;
  const FULL_PULL_EVERY_MS = 24 * 3600 * 1000;
  const PUSH_BATCH = 50;
  const HISTORY_SIZE = 100;
  const CLOSED = new Set(["closed", "cancelled", "done", "short", "posted", "rejected", "awaiting_approval"]);
  const WAREHOUSE_ROLES = new Set(["system_admin", "site_manager", "warehouse_supervisor", "warehouse_operator", "dispatcher"]);
  const PHOTO_MAX_PX = 1600;
  const FAILURE_REASONS = [
    ["closed", "Premises closed"],
    ["refused", "Refused by the recipient"],
    ["no_access", "No access"],
    ["wrong_address", "Wrong or unknown address"],
    ["damaged", "Goods damaged"],
    ["out_of_time", "Ran out of time"],
    ["other", "Other (say what)"],
  ];

  const app = document.getElementById("app");
  const statusEl = document.getElementById("status");

  // --- DOM ----------------------------------------------------------------------------------------------------
  // Text is always set via text nodes, never innerHTML, so nothing from the server is parsed as markup.

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else if (key === "value") el.value = value;
      else el.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function show(...nodes) {
    app.replaceChildren(...nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false));
    const first = app.querySelector("[autofocus]") || app.querySelector("input");
    if (first) first.focus();
  }

  function flash() {
    if (!state.flash) return null;
    const node = h("div", { class: `flash ${state.flash.kind}`, role: "alert" }, state.flash.text);
    state.flash = null;
    return node;
  }

  function say(text, kind = "ok") {
    state.flash = { text, kind };
  }

  function field(label, input) {
    const id = input.id || `f-${Math.random().toString(36).slice(2)}`;
    input.id = id;
    return [h("label", { for: id }, label), input];
  }

  // Barcode scanners type the code and press Enter: move to the next field instead of submitting early.
  function scanFlow(form) {
    form.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.target.tagName !== "INPUT") return;
      const inputs = [...form.querySelectorAll("input, select")].filter((el) => !el.disabled);
      const next = inputs[inputs.indexOf(event.target) + 1];
      if (next) {
        event.preventDefault();
        next.focus();
        if (next.select) next.select();
      }
    });
    return form;
  }

  // --- IndexedDB ----------------------------------------------------------------------------------------------

  const db = {
    handle: null,
    open() {
      return new Promise((resolve, reject) => {
        const req = indexedDB.open("scanner", 2);
        req.onupgradeneeded = () => {
          const stores = req.result.objectStoreNames;
          if (!stores.contains("kv")) req.result.createObjectStore("kv");
          if (!stores.contains("queue")) req.result.createObjectStore("queue", { keyPath: "seq", autoIncrement: true });
          if (!stores.contains("files")) req.result.createObjectStore("files"); // evidence waiting to upload
        };
        req.onsuccess = () => resolve((this.handle = req.result));
        req.onerror = () => reject(req.error);
      });
    },
    run(store, mode, fn) {
      return new Promise((resolve, reject) => {
        const tx = this.handle.transaction(store, mode);
        const req = fn(tx.objectStore(store));
        tx.oncomplete = () => resolve(req && req.result);
        tx.onerror = () => reject(tx.error);
      });
    },
    get: (key) => db.run("kv", "readonly", (s) => s.get(key)),
    set: (key, value) => db.run("kv", "readwrite", (s) => s.put(value, key)),
    del: (key) => db.run("kv", "readwrite", (s) => s.delete(key)),
    queue: () => db.run("queue", "readonly", (s) => s.getAll()),
    putCommand: (cmd) => db.run("queue", "readwrite", (s) => s.put(cmd)),
    dropCommand: (seq) => db.run("queue", "readwrite", (s) => s.delete(seq)),
    putFile: (id, record) => db.run("files", "readwrite", (s) => s.put(record, id)),
    getFile: (id) => db.run("files", "readonly", (s) => s.get(id)),
    dropFile: (id) => db.run("files", "readwrite", (s) => s.delete(id)),
  };

  // --- State --------------------------------------------------------------------------------------------------

  const state = {
    deviceId: null,
    session: null,
    sites: [],
    siteId: null,
    cursor: null,
    lastFull: 0,
    ref: emptyRef(),
    queue: [],
    history: [],
    online: navigator.onLine,
    syncing: false,
    lastSync: null,
    flash: null,
    trips: [],
  };

  function emptyRef() {
    return { locations: new Map(), items: new Map(), notices: new Map(), tasks: new Map(), pick_tasks: new Map(), counts: new Map() };
  }

  function uuid4() {
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const hex = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  async function saveRef() {
    const plain = Object.fromEntries(Object.entries(state.ref).map(([k, m]) => [k, [...m.values()]]));
    await db.set(`ref:${state.siteId}`, { ref: plain, cursor: state.cursor, lastFull: state.lastFull });
  }

  async function loadRef() {
    state.ref = emptyRef();
    state.cursor = null;
    state.lastFull = 0;
    const saved = state.siteId && (await db.get(`ref:${state.siteId}`));
    if (!saved) return;
    for (const [kind, rows] of Object.entries(saved.ref)) if (state.ref[kind]) for (const row of rows) state.ref[kind].set(row.id, row);
    state.cursor = saved.cursor;
    state.lastFull = saved.lastFull;
  }

  // --- Server -------------------------------------------------------------------------------------------------

  function csrfToken() {
    const match = document.cookie.split("; ").find((c) => c.startsWith(`${CSRF_COOKIE}=`));
    return match ? decodeURIComponent(match.split("=")[1]) : "";
  }

  async function api(method, path, body) {
    let response;
    try {
      response = await fetch(API + path, {
        method,
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          "X-CSRFToken": csrfToken(),
          ...(method === "GET" ? {} : { "Idempotency-Key": uuid4() }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      setOnline(false);
      throw Object.assign(new Error("offline"), { offline: true });
    }
    setOnline(true);
    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    if (response.status === 401 && path !== "/auth/session") {
      state.session = null;
      await db.del("session");
    }
    return { status: response.status, data };
  }

  function setOnline(online) {
    state.online = online;
    renderStatus();
  }

  async function upload(path, form) {
    let response;
    try {
      response = await fetch(API + path, {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-CSRFToken": csrfToken(), "Idempotency-Key": uuid4() },
        body: form,
      });
    } catch {
      setOnline(false);
      throw Object.assign(new Error("offline"), { offline: true });
    }
    setOnline(true);
    const text = await response.text();
    return { status: response.status, data: text ? JSON.parse(text) : null };
  }

  // --- Queue and sync -----------------------------------------------------------------------------------------

  async function refreshQueue() {
    state.queue = (await db.queue()).sort((a, b) => a.seq - b.seq);
  }

  function mine(cmd) {
    return state.session && cmd.user_id === state.session.user_id;
  }

  function pending() {
    return state.queue.filter((c) => c.state === "pending" && mine(c));
  }

  // `files` are evidence saved with saveFile(): uploaded just before the command is sent, and their document ids
  // put into the payload (`signature` -> signature_document_id, `photo` -> photo_document_ids).
  async function enqueue(type, payload, summary, files = []) {
    await db.putCommand({
      command_id: uuid4(),
      type,
      payload,
      summary,
      files,
      user_id: state.session.user_id,
      site_id: state.siteId,
      recorded_at: new Date().toISOString(),
      state: "pending",
    });
    await refreshQueue();
    renderStatus();
    syncSoon();
  }

  async function remember(cmd, outcome) {
    state.history.unshift({ summary: cmd.summary, type: cmd.type, at: new Date().toISOString(), outcome });
    state.history = state.history.slice(0, HISTORY_SIZE);
    await db.set(`history:${state.session.user_id}`, state.history);
  }

  async function saveFile(blob, name, extra) {
    const id = uuid4();
    await db.putFile(id, { blob, name, captured_at: new Date().toISOString(), ...extra });
    return id;
  }

  async function dropFiles(cmd) {
    for (const ref of cmd.files || []) await db.dropFile(ref.file_id);
  }

  async function reject(cmd, problem) {
    cmd.state = "rejected";
    cmd.problem = problem;
    await db.putCommand(cmd);
    await remember(cmd, "rejected");
  }

  // Upload a command's evidence. Returns false if the server refused a file (the command is then rejected).
  async function uploadFiles(cmd) {
    const refs = cmd.files || [];
    for (const ref of refs) {
      if (ref.document_id) continue;
      const record = await db.getFile(ref.file_id);
      if (!record) {
        await reject(cmd, { title: "Evidence lost", detail: "A signature or photo is missing on this device. Capture it again." });
        return false;
      }
      const form = new FormData();
      form.append("file", record.blob, record.name);
      form.append("kind", ref.kind);
      form.append("captured_at", record.captured_at);
      if (record.latitude) form.append("latitude", record.latitude);
      if (record.longitude) form.append("longitude", record.longitude);
      const res = await upload(`/trips/${cmd.payload.trip_id}/stops/${cmd.payload.stop_id}/evidence`, form);
      if (res.status === 401 || res.status >= 500) throw Object.assign(new Error("upload failed"), { status: res.status });
      if (res.status !== 201) {
        await reject(cmd, res.data || { title: "Upload refused" });
        return false;
      }
      ref.document_id = res.data.id;
      await db.putCommand(cmd);
    }
    if (refs.length) {
      const signature = refs.find((r) => r.field === "signature");
      if (signature) cmd.payload.signature_document_id = signature.document_id;
      cmd.payload.photo_document_ids = refs.filter((r) => r.field === "photo").map((r) => r.document_id);
      await db.putCommand(cmd);
    }
    return true;
  }

  async function pushQueue() {
    for (;;) {
      await refreshQueue();
      let batch = pending().slice(0, PUSH_BATCH);
      if (!batch.length) return;
      // Evidence goes first, in queue order, so a POD never reaches the server before its signature.
      const ready = [];
      for (const cmd of batch) if (await uploadFiles(cmd)) ready.push(cmd);
      batch = ready;
      if (!batch.length) continue;
      const res = await api("POST", "/sync/push", {
        device_id: state.deviceId,
        commands: batch.map((c) => ({ command_id: c.command_id, type: c.type, payload: c.payload, recorded_at: c.recorded_at })),
      });
      if (res.status !== 200) throw Object.assign(new Error("push failed"), { status: res.status });
      for (const result of res.data.results) {
        const cmd = batch.find((c) => c.command_id === result.command_id);
        if (result.status === "applied") {
          await db.dropCommand(cmd.seq);
          await dropFiles(cmd);
          await remember(cmd, "applied");
        } else if (result.status === "rejected") {
          await reject(cmd, result.problem);
        }
      }
      if (res.data.results.some((r) => r.status === "retry") || res.data.results.length < batch.length) return;
    }
  }

  async function pull() {
    const full = !state.cursor || Date.now() - state.lastFull > FULL_PULL_EVERY_MS;
    const params = new URLSearchParams({ site_id: state.siteId });
    if (!full) params.set("cursor", state.cursor);
    const res = await api("GET", `/sync/pull?${params}`);
    if (res.status !== 200) throw Object.assign(new Error("pull failed"), { status: res.status });
    const data = res.data;
    if (data.full) state.ref = emptyRef();
    for (const kind of Object.keys(state.ref)) {
      for (const row of data[kind] || []) {
        const gone = row.is_active === false || CLOSED.has(row.status);
        if (gone) state.ref[kind].delete(row.id);
        else state.ref[kind].set(row.id, row);
      }
    }
    state.cursor = data.cursor;
    if (data.full) state.lastFull = Date.now();
    indexItems();
    await saveRef();
  }

  let syncTimer = null;
  function syncSoon() {
    clearTimeout(syncTimer);
    syncTimer = setTimeout(syncNow, 300);
  }

  async function syncNow() {
    if (state.syncing || !state.session) return;
    state.syncing = true;
    renderStatus();
    try {
      await pushQueue();
      if (driverMode()) {
        await pullTrips();
        if (!formOnScreen()) route();
      } else if (state.siteId) {
        await pull();
        if (!formOnScreen()) route(); // refresh lists and counters, but never wipe a half-scanned form
      }
      state.lastSync = new Date();
    } catch (error) {
      if (!error.offline) console.warn("sync", error);
    } finally {
      state.syncing = false;
      await refreshQueue();
      renderStatus();
      if (!state.session) route();
    }
  }

  function formOnScreen() {
    return Boolean(app.querySelector("form"));
  }

  function renderStatus() {
    const waiting = pending().length;
    const rejected = state.queue.filter((c) => c.state === "rejected" && mine(c)).length;
    const parts = [state.online ? (state.syncing ? "Syncing…" : "Online") : "Offline"];
    if (waiting) parts.push(`${waiting} to send`);
    if (rejected) parts.push(`${rejected} problem${rejected > 1 ? "s" : ""}`);
    if (state.lastSync) parts.push(`synced ${state.lastSync.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`);
    statusEl.textContent = parts.join(" · ");
    statusEl.className = state.online ? "" : "offline";
  }

  // --- Items and bins -----------------------------------------------------------------------------------------

  let byCode = new Map();
  function indexItems() {
    byCode = new Map();
    const add = (code, entry) => {
      const key = code.toUpperCase();
      if (!byCode.has(key)) byCode.set(key, []);
      byCode.get(key).push(entry);
    };
    for (const item of state.ref.items.values()) {
      add(item.sku, { item, uom: item.base_uom });
      for (const bc of item.barcodes) add(bc.barcode, { item, uom: bc.uom || item.base_uom });
    }
  }

  function findItems(code, clientId) {
    const hits = byCode.get(code.trim().toUpperCase()) || [];
    return clientId ? hits.filter((hit) => hit.item.client_id === clientId) : hits;
  }

  function bin(code) {
    const wanted = code.trim().toUpperCase();
    for (const loc of state.ref.locations.values()) if (loc.code === wanted) return loc;
    return null;
  }

  function uomOptions(item, selected) {
    const uoms = [item.base_uom, ...item.uoms.map((u) => u.uom).filter((u) => u !== item.base_uom)];
    return uoms.map((u) => h("option", { value: u, selected: u === selected }, u));
  }

  // An item picker: scan or type a barcode or SKU; pick among matches when a code is shared by several clients.
  function itemPicker(clientId, onChange) {
    const input = h("input", { autocomplete: "off", autocapitalize: "characters", placeholder: "Scan item barcode or SKU" });
    const choice = h("div");
    let picked = null;
    const choose = (hit) => {
      picked = hit;
      choice.replaceChildren(hit ? h("p", { class: "muted" }, `${hit.item.sku} · ${hit.item.description}`) : "");
      onChange(hit);
    };
    input.addEventListener("change", () => {
      const hits = findItems(input.value, clientId);
      if (hits.length === 1) return choose(hits[0]);
      if (!hits.length) {
        choose(null);
        choice.replaceChildren(h("p", { class: "flash bad" }, input.value.trim() ? "Unknown item for this site." : ""));
        return;
      }
      const select = h(
        "select",
        { onchange: () => choose(hits[Number(select.value)]) },
        h("option", { value: "" }, "Shared code: pick the item"),
        hits.map((hit, i) => h("option", { value: i }, `${hit.item.sku} · ${hit.item.description}`)),
      );
      choose(null);
      choice.replaceChildren(select);
    });
    return { nodes: [...field("Item", input), choice], get: () => picked };
  }

  // Item, quantity, pack size and (when tracked) lot and expiry.
  function stockFields(clientId) {
    const qty = h("input", { inputmode: "decimal", placeholder: "Quantity", required: true });
    const uom = h("select", { disabled: true });
    const lot = h("input", { autocomplete: "off", placeholder: "Lot" });
    const expiry = h("input", { type: "date" });
    const lotRow = h("div", { hidden: true }, field("Lot", lot));
    const expiryRow = h("div", { hidden: true }, field("Expiry", expiry));
    const picker = itemPicker(clientId, (hit) => {
      uom.replaceChildren(...(hit ? uomOptions(hit.item, hit.uom) : []));
      uom.disabled = !hit;
      lotRow.hidden = !(hit && hit.item.lot_tracked);
      expiryRow.hidden = !(hit && hit.item.expiry_tracked);
    });
    return {
      nodes: [...picker.nodes, h("div", { class: "row" }, h("div", {}, field("Quantity", qty)), h("div", {}, field("Unit", uom))), lotRow, expiryRow],
      read() {
        const hit = picker.get();
        if (!hit) throw new Error("Scan an item first.");
        const amount = Number(qty.value);
        if (!(amount >= 0) || qty.value.trim() === "") throw new Error("Enter a quantity.");
        if (hit.item.lot_tracked && !lot.value.trim()) throw new Error("This item needs a lot.");
        if (hit.item.expiry_tracked && !expiry.value) throw new Error("This item needs an expiry date.");
        return {
          item: hit.item,
          fields: { client_id: hit.item.client_id, sku: hit.item.sku, qty: qty.value.trim(), uom: uom.value, lot: lot.value.trim(), expiry: expiry.value || null },
        };
      },
    };
  }

  function guarded(fn) {
    return async (event) => {
      event.preventDefault();
      try {
        await fn(event);
      } catch (error) {
        say(error.message, "bad");
        route();
      }
    };
  }

  // --- Screens ------------------------------------------------------------------------------------------------

  function back(hash = "#") {
    return h("a", { class: "btn secondary", href: hash }, "Back");
  }

  function viewLogin() {
    const email = h("input", { type: "email", autocomplete: "username", required: true, autofocus: true });
    const password = h("input", { type: "password", autocomplete: "current-password", required: true });
    const form = h(
      "form",
      {
        onsubmit: guarded(async () => {
          const res = await api("POST", "/auth/session", { email: email.value, password: password.value });
          if (res.status !== 200) throw new Error((res.data && res.data.detail) || "Sign-in failed.");
          await startSession(res.data);
          location.hash = "#";
          route();
        }),
      },
      field("Email", email),
      field("Password", password),
      h("div", { class: "actions" }, h("button", { type: "submit" }, "Sign in")),
    );
    show(h("h1", {}, "Sign in"), flash(), form, state.online ? null : h("p", { class: "muted" }, "You are offline. Sign-in needs a connection."));
  }

  function viewSites() {
    show(
      h("h1", {}, "Choose your site"),
      flash(),
      h(
        "ul",
        { class: "list" },
        state.sites.map((site) => h("li", {}, h("a", { href: "#", onclick: guarded(() => chooseSite(site.id)) }, `${site.code} · ${site.name}`))),
      ),
      state.sites.length ? null : h("p", { class: "muted" }, "You have no warehouse sites. Ask a site manager for access."),
    );
  }

  function viewHome() {
    const site = state.sites.find((s) => s.id === state.siteId);
    const rejected = state.queue.filter((c) => c.state === "rejected" && mine(c)).length;
    show(
      h("h1", {}, site ? `${site.code} · ${site.name}` : "Scanner"),
      flash(),
      h(
        "div",
        { class: "tiles" },
        h("a", { class: "tile", href: "#receive" }, "Receive"),
        h("a", { class: "tile", href: "#putaway" }, `Put away (${openTasks().length})`),
        h("a", { class: "tile", href: "#pick" }, `Pick (${openPicks().length})`),
        h("a", { class: "tile", href: "#pack" }, "Pack"),
        h("a", { class: "tile", href: "#load" }, "Load"),
        h("a", { class: "tile", href: "#count" }, `Count (${state.ref.counts.size})`),
        h("a", { class: "tile", href: "#move" }, "Move"),
      ),
      h(
        "div",
        { class: "actions" },
        h("a", { class: "btn secondary", href: "#queue" }, rejected ? `Activity · ${rejected} problem${rejected > 1 ? "s" : ""}` : "Activity"),
        h("button", { class: "secondary", onclick: () => syncNow() }, "Sync now"),
        state.sites.length > 1 ? h("a", { class: "btn secondary", href: "#site" }, "Change site") : null,
        h("button", { class: "secondary", onclick: guarded(signOut) }, `Sign out ${state.session.email}`),
      ),
    );
  }

  // Receiving against an ASN. The first line opens the receipt on the server, so this works offline too.
  function viewReceiveList() {
    const notices = [...state.ref.notices.values()].sort((a, b) => String(a.expected_at).localeCompare(String(b.expected_at)));
    show(
      h("h1", {}, "Receive"),
      flash(),
      h(
        "ul",
        { class: "list" },
        notices.map((n) =>
          h("li", {}, h("a", { href: `#receive/${n.id}` }, h("strong", {}, n.number), n.client_ref ? ` · ${n.client_ref}` : "", h("div", { class: "muted" }, `${n.lines.length} lines · ${n.status}`))),
        ),
      ),
      notices.length ? null : h("p", { class: "muted" }, "No ASNs waiting at this site."),
      h("div", { class: "actions" }, back()),
    );
  }

  function viewReceive(noticeId) {
    const notice = state.ref.notices.get(noticeId);
    if (!notice) return viewReceiveList();
    const dock = h("input", { autocomplete: "off", autocapitalize: "characters", value: localStorage.getItem(`dock:${state.siteId}`) || "", placeholder: "Scan dock" });
    const lpn = h("input", { autocomplete: "off", placeholder: "Pallet label (optional)" });
    const stock = stockFields(notice.client_id);
    const scanned = state.queue.filter((c) => c.type.startsWith("receipt.") && c.payload.inbound_notice_id === noticeId && mine(c));
    const form = scanFlow(
      h(
        "form",
        {
          onsubmit: guarded(async () => {
            if (!bin(dock.value)) throw new Error("Scan a dock location at this site.");
            const { item, fields } = stock.read();
            if (Number(fields.qty) <= 0) throw new Error("Quantity must be more than zero.");
            localStorage.setItem(`dock:${state.siteId}`, dock.value.trim().toUpperCase());
            const { client_id: _client, ...line } = fields;
            await enqueue("receipt.line", { inbound_notice_id: noticeId, dock_code: dock.value.trim(), lpn: lpn.value.trim(), ...line }, `${notice.number}: ${fields.qty} ${fields.uom} ${item.sku}`);
            say(`Recorded ${fields.qty} ${fields.uom} ${item.sku}`);
            route();
          }),
        },
        field("Dock", dock),
        stock.nodes,
        field("Pallet label", lpn),
        h("div", { class: "actions" }, h("button", { type: "submit" }, "Add line")),
      ),
    );
    show(
      h("h1", {}, `Receive ${notice.number}`),
      flash(),
      h("h2", {}, "Expected"),
      h(
        "ul",
        { class: "list" },
        notice.lines.map((line) => {
          const item = state.ref.items.get(line.item_id);
          return h("li", {}, `${item ? item.sku : "?"} · ${line.qty} ${line.uom}`, line.lot ? ` · lot ${line.lot}` : "");
        }),
      ),
      h("h2", {}, "Scan"),
      form,
      scanned.length ? [h("h2", {}, "Waiting to send"), h("ul", { class: "list" }, scanned.map((c) => h("li", {}, c.summary)))] : null,
      h(
        "div",
        { class: "actions" },
        h(
          "button",
          {
            class: "secondary",
            onclick: guarded(async () => {
              if (!confirm(`Complete ${notice.number}? Anything not scanned is reported short.`)) return;
              await enqueue("receipt.complete", { inbound_notice_id: noticeId }, `${notice.number}: complete`);
              say(`${notice.number} will be completed`);
              location.hash = "#receive";
            }),
          },
          "Complete receipt",
        ),
        back("#receive"),
      ),
    );
  }

  function openTasks() {
    const confirming = new Set(state.queue.filter((c) => c.type === "putaway.confirm" && c.state === "pending").map((c) => c.payload.task_id));
    return [...state.ref.tasks.values()].filter(
      (t) => !confirming.has(t.id) && (!t.assigned_to || t.assigned_to === state.session.user_id),
    );
  }

  function viewPutawayList() {
    const tasks = openTasks();
    show(
      h("h1", {}, "Put away"),
      flash(),
      h(
        "ul",
        { class: "list" },
        tasks.map((t) =>
          h("li", {}, h("a", { href: `#putaway/${t.id}` }, h("strong", {}, `${t.qty} × ${t.sku}`), h("div", { class: "muted" }, `${t.from_location} → ${t.suggested_location || "any bin"}`))),
        ),
      ),
      tasks.length ? null : h("p", { class: "muted" }, "Nothing to put away."),
      h("div", { class: "actions" }, back()),
    );
  }

  function viewPutaway(taskId) {
    const task = state.ref.tasks.get(taskId);
    if (!task) return viewPutawayList();
    const target = h("input", { autocomplete: "off", autocapitalize: "characters", placeholder: "Scan bin", autofocus: true });
    const reason = h("input", { autocomplete: "off", placeholder: "Why not the suggested bin?" });
    const form = scanFlow(
      h(
        "form",
        {
          onsubmit: guarded(async () => {
            const code = target.value.trim().toUpperCase();
            if (!bin(code)) throw new Error("Unknown bin at this site.");
            if (code !== task.suggested_location && !reason.value.trim()) throw new Error("Give a reason for using a different bin.");
            const summary = `Put away ${task.qty} ${task.sku} → ${code}`;
            if (!task.assigned_to) await enqueue("putaway.claim", { task_id: taskId }, `Claim ${task.sku}`);
            await enqueue("putaway.confirm", { task_id: taskId, to_location_code: code, reason: reason.value.trim() }, summary);
            say(summary);
            location.hash = "#putaway";
          }),
        },
        field("Bin", target),
        field("Reason (if not the suggested bin)", reason),
        h("div", { class: "actions" }, h("button", { type: "submit" }, "Confirm")),
      ),
    );
    show(
      h("h1", {}, `${task.qty} × ${task.sku}`),
      flash(),
      h("p", {}, `From ${task.from_location}`),
      h("p", {}, h("strong", {}, `Suggested: ${task.suggested_location || "any free bin"}`)),
      task.lot ? h("p", { class: "muted" }, `Lot ${task.lot}${task.expiry ? ` · expires ${task.expiry}` : ""}`) : null,
      form,
      h("div", { class: "actions" }, back("#putaway")),
    );
  }

  // Picking walks the pick path: tasks sorted by bin sequence, one scan of bin and item each. A quantity below
  // the task's is a short pick and needs a reason; the server then looks for the rest in other bins.
  function openPicks() {
    const sent = new Set(state.queue.filter((c) => (c.type === "pick.confirm" || c.type === "pick.short") && c.state === "pending").map((c) => c.payload.task_id));
    return [...state.ref.pick_tasks.values()]
      .filter((t) => !sent.has(t.id) && (!t.assigned_to || t.assigned_to === state.session.user_id))
      .sort((a, b) => a.pick_sequence - b.pick_sequence || a.from_location.localeCompare(b.from_location) || a.wave_number.localeCompare(b.wave_number));
  }

  function viewPickList() {
    const picks = openPicks();
    show(
      h("h1", {}, "Pick"),
      flash(),
      h(
        "ul",
        { class: "list" },
        picks.map((t) =>
          h("li", {}, h("a", { href: `#pick/${t.id}` }, h("strong", {}, `${t.from_location} · ${Number(t.qty)} × ${t.sku}`), h("div", { class: "muted" }, `${t.wave_number}${t.lot ? ` · lot ${t.lot}` : ""}`))),
        ),
      ),
      picks.length ? null : h("p", { class: "muted" }, "Nothing to pick."),
      h("div", { class: "actions" }, back()),
    );
  }

  function viewPick(taskId) {
    const task = state.ref.pick_tasks.get(taskId);
    if (!task) return viewPickList();
    const wanted = Number(task.qty);
    const where = h("input", { autocomplete: "off", autocapitalize: "characters", placeholder: "Scan bin", autofocus: true });
    const what = h("input", { autocomplete: "off", autocapitalize: "characters", placeholder: "Scan item barcode or SKU" });
    const qty = h("input", { inputmode: "decimal", placeholder: String(wanted), required: true });
    const reason = h("input", { autocomplete: "off", placeholder: "Why is it short?" });
    const reasonRow = h("div", { hidden: true }, field("Short pick reason", reason));
    qty.addEventListener("input", () => {
      reasonRow.hidden = !(qty.value.trim() !== "" && Number(qty.value) < wanted);
    });
    const form = scanFlow(
      h(
        "form",
        {
          onsubmit: guarded(async () => {
            const code = where.value.trim().toUpperCase();
            if (code !== task.from_location) throw new Error(`Wrong bin: this pick is from ${task.from_location}.`);
            const found = Number(qty.value);
            if (qty.value.trim() === "" || !(found >= 0)) throw new Error("Enter the quantity picked.");
            if (found > wanted) throw new Error(`This pick is ${wanted}. Leave the rest in the bin.`);
            if (found > 0 && !findItems(what.value, task.client_id).some((hit) => hit.item.id === task.item_id)) {
              throw new Error(`That is not ${task.sku}.`);
            }
            if (!task.assigned_to) await enqueue("pick.claim", { task_id: taskId }, `Claim pick ${task.sku}`);
            if (found === wanted) {
              const summary = `Picked ${wanted} ${task.sku} from ${code}`;
              await enqueue("pick.confirm", { task_id: taskId, location_code: code, item_code: what.value.trim(), qty: qty.value.trim() }, summary);
              say(summary);
            } else {
              if (!reason.value.trim()) throw new Error("Say why the pick is short.");
              const summary = `Short pick ${found} of ${wanted} ${task.sku} from ${code}`;
              await enqueue("pick.short", { task_id: taskId, location_code: code, qty_found: qty.value.trim(), reason: reason.value.trim() }, summary);
              say(summary, "bad");
            }
            const next = openPicks()[0];
            location.hash = next ? `#pick/${next.id}` : "#pick";
          }),
        },
        field("Bin", where),
        field("Item", what),
        field("Quantity picked", qty),
        reasonRow,
        h("div", { class: "actions" }, h("button", { type: "submit" }, "Confirm pick")),
      ),
    );
    show(
      h("h1", {}, `${task.from_location}`),
      flash(),
      h("p", {}, h("strong", {}, `Pick ${wanted} × ${task.sku}`)),
      task.lot ? h("p", { class: "muted" }, `Lot ${task.lot}${task.expiry ? ` · expires ${task.expiry}` : ""}`) : null,
      h("p", { class: "muted" }, task.wave_number),
      form,
      h("div", { class: "actions" }, back("#pick")),
    );
  }

  // Packing happens at a bench with a printer, so it works online only: every scan goes straight to the server,
  // which hands out the SSCC and checks quantities against what was picked.
  let packOrders = [];

  async function loadPackOrders() {
    const res = await api("GET", `/pack-orders?site_id=${state.siteId}`);
    if (res.status !== 200) throw new Error((res.data && res.data.detail) || "Could not load orders to pack.");
    packOrders = res.data;
  }

  function problem(res, fallback) {
    return new Error(res.data && (res.data.detail || res.data.title) ? `${res.data.title || ""}${res.data.detail ? `: ${res.data.detail}` : ""}` : fallback);
  }

  function packOffline() {
    show(h("h1", {}, "Pack"), h("p", { class: "flash bad" }, "Packing needs a connection."), h("div", { class: "actions" }, back()));
  }

  async function viewPackList() {
    if (!state.online) return packOffline();
    try {
      await loadPackOrders();
    } catch (error) {
      if (error.offline) return packOffline();
      say(error.message, "bad");
    }
    show(
      h("h1", {}, "Pack"),
      flash(),
      h(
        "ul",
        { class: "list" },
        packOrders.map((o) => {
          const left = o.lines.reduce((sum, l) => sum + Number(l.remaining), 0);
          return h("li", {}, h("a", { href: `#pack/${o.sales_order_id}` }, h("strong", {}, o.number), h("div", { class: "muted" }, `${left} to pack${o.picking_done ? "" : " · still picking"}`)));
        }),
      ),
      packOrders.length ? null : h("p", { class: "muted" }, "Nothing waiting to be packed."),
      h("div", { class: "actions" }, back()),
    );
  }

  async function viewPackOrder(orderId, unitId) {
    if (!state.online) return packOffline();
    try {
      await loadPackOrders();
    } catch (error) {
      if (error.offline) return packOffline();
      say(error.message, "bad");
    }
    const order = packOrders.find((o) => o.sales_order_id === orderId);
    if (!order) {
      if (!state.flash) say("That order is packed or no longer waiting.");
      location.hash = "#pack";
      return;
    }
    const unit = order.open_units.find((u) => u.id === unitId) || (!unitId && order.open_units.length === 1 ? order.open_units[0] : null);
    const lines = h(
      "ul",
      { class: "list" },
      order.lines.map((l) => h("li", {}, `${l.sku}${l.lot ? ` · lot ${l.lot}` : ""}: ${Number(l.remaining)} of ${Number(l.picked)} left`)),
    );
    const start = (kind) =>
      guarded(async () => {
        const res = await api("POST", "/handling-units", { sales_order_id: orderId, kind });
        if (res.status !== 201) throw problem(res, "Could not start a handling unit.");
        say(`New ${kind} ${res.data.sscc}`);
        location.hash = `#pack/${orderId}/${res.data.id}`;
      });
    if (!unit) {
      return show(
        h("h1", {}, `Pack ${order.number}`),
        flash(),
        lines,
        order.open_units.length
          ? [h("h2", {}, "Open units"), h("ul", { class: "list" }, order.open_units.map((u) => h("li", {}, h("a", { href: `#pack/${orderId}/${u.id}` }, `${u.kind} ${u.sscc} · ${u.contents.length} lines`))))]
          : null,
        h("div", { class: "actions" }, h("button", { onclick: start("carton") }, "New carton"), h("button", { class: "secondary", onclick: start("pallet") }, "New pallet"), back("#pack")),
      );
    }
    const code = h("input", { autocomplete: "off", autocapitalize: "characters", placeholder: "Scan item barcode or SKU", autofocus: true });
    const qty = h("input", { inputmode: "decimal", placeholder: "Quantity (units)", required: true });
    const lot = h("input", { autocomplete: "off", placeholder: "Only if asked" });
    const addForm = scanFlow(
      h(
        "form",
        {
          onsubmit: guarded(async () => {
            if (!code.value.trim() || !(Number(qty.value) > 0)) throw new Error("Scan an item and enter a quantity.");
            const res = await api("POST", `/handling-units/${unit.id}/contents`, { item_code: code.value.trim(), qty: qty.value.trim(), lot: lot.value.trim() });
            if (res.status !== 201) throw problem(res, "Could not pack that.");
            say(`Packed ${qty.value.trim()} × ${code.value.trim()}`);
            route();
          }),
        },
        field("Item", code),
        field("Quantity", qty),
        field("Lot", lot),
        h("div", { class: "actions" }, h("button", { type: "submit" }, "Add")),
      ),
    );
    const weight = h("input", { inputmode: "decimal", placeholder: "kg", required: true });
    const cartons = h("input", { inputmode: "numeric", placeholder: unit.kind === "carton" ? "1" : "Cartons on the pallet" });
    const closeForm = h(
      "form",
      {
        onsubmit: guarded(async () => {
          const body = { weight_kg: weight.value.trim() };
          if (cartons.value.trim()) body.cartons = Number(cartons.value);
          const res = await api("POST", `/handling-units/${unit.id}/close`, body);
          if (res.status !== 200) throw problem(res, "Could not close the unit.");
          window.open(`${API}/handling-units/${unit.id}/label`, "_blank", "noopener");
          say(`Closed ${unit.sscc}. The label opened in a new tab.`);
          location.hash = `#pack/${orderId}`;
        }),
      },
      field("Weight (kg)", weight),
      field("Cartons", cartons),
      h("div", { class: "actions" }, h("button", { type: "submit" }, "Close and print label")),
    );
    show(
      h("h1", {}, `${unit.kind} ${unit.sscc}`),
      flash(),
      h("p", { class: "muted" }, order.number),
      h("h2", {}, "Left to pack"),
      lines,
      h("h2", {}, "Add"),
      addForm,
      h("h2", {}, `In this ${unit.kind}`),
      h(
        "ul",
        { class: "list" },
        unit.contents.map((c) =>
          h(
            "li",
            {},
            `${Number(c.qty)} × ${c.sku}${c.lot ? ` · lot ${c.lot}` : ""} `,
            h(
              "button",
              {
                class: "secondary",
                onclick: guarded(async () => {
                  const res = await api("DELETE", `/handling-units/${unit.id}/contents/${c.id}`);
                  if (res.status !== 200) throw problem(res, "Could not remove that line.");
                  say(`Removed ${c.sku}`);
                  route();
                }),
              },
              "Remove",
            ),
          ),
        ),
      ),
      unit.contents.length ? [h("h2", {}, "Close"), closeForm] : null,
      h("div", { class: "actions" }, back(`#pack/${orderId}`)),
    );
  }

  // Counting is blind: the device never has system quantities, only the bins to count.
  function viewCountList() {
    const counts = [...state.ref.counts.values()];
    show(
      h("h1", {}, "Count"),
      flash(),
      h("ul", { class: "list" }, counts.map((c) => h("li", {}, h("a", { href: `#count/${c.id}` }, h("strong", {}, c.number), h("div", { class: "muted" }, `${c.location_codes.length} bins`))))),
      counts.length ? null : h("p", { class: "muted" }, "No open counts at this site."),
      h("div", { class: "actions" }, back()),
    );
  }

  function viewCount(countId) {
    const count = state.ref.counts.get(countId);
    if (!count) return viewCountList();
    const where = h("input", { autocomplete: "off", autocapitalize: "characters", placeholder: "Scan bin", autofocus: true, value: sessionStorage.getItem(`count-bin:${countId}`) || "" });
    const stock = stockFields(null);
    const mineHere = state.history.filter((e) => e.type === "count.line" && e.summary.startsWith(count.number)).slice(0, 20);
    const queued = state.queue.filter((c) => c.type === "count.line" && c.payload.count_id === countId && mine(c));
    const form = scanFlow(
      h(
        "form",
        {
          onsubmit: guarded(async () => {
            const code = where.value.trim().toUpperCase();
            if (!count.location_codes.includes(code)) throw new Error(`${code || "That bin"} is not part of ${count.number}.`);
            const { item, fields } = stock.read();
            sessionStorage.setItem(`count-bin:${countId}`, code);
            await enqueue("count.line", { count_id: countId, location_code: code, ...fields }, `${count.number}: ${code} ${fields.qty} ${fields.uom} ${item.sku}`);
            say(`Counted ${fields.qty} ${fields.uom} ${item.sku} in ${code}`);
            route();
          }),
        },
        field("Bin", where),
        stock.nodes,
        h("div", { class: "actions" }, h("button", { type: "submit" }, "Record count")),
      ),
    );
    show(
      h("h1", {}, `Count ${count.number}`),
      flash(),
      h("p", { class: "muted" }, `Bins: ${count.location_codes.join(", ")}. Scan every item you find. A bin with nothing recorded counts as empty.`),
      form,
      queued.length ? [h("h2", {}, "Waiting to send"), h("ul", { class: "list" }, queued.map((c) => h("li", {}, c.summary)))] : null,
      mineHere.length ? [h("h2", {}, "Sent"), h("ul", { class: "list" }, mineHere.map((e) => h("li", {}, e.summary, " ", h("span", { class: `tag ${e.outcome}` }, e.outcome))))] : null,
      h("div", { class: "actions" }, back("#count")),
    );
  }

  function viewMove() {
    const from = h("input", { autocomplete: "off", autocapitalize: "characters", placeholder: "Scan source bin", autofocus: true });
    const to = h("input", { autocomplete: "off", autocapitalize: "characters", placeholder: "Scan destination bin" });
    const reason = h("input", { autocomplete: "off", placeholder: "Optional" });
    const stock = stockFields(null);
    const form = scanFlow(
      h(
        "form",
        {
          onsubmit: guarded(async () => {
            const source = bin(from.value);
            const target = bin(to.value);
            if (!source || !target) throw new Error("Scan bins at this site.");
            if (source.id === target.id) throw new Error("Choose a different destination bin.");
            const { item, fields } = stock.read();
            if (Number(fields.qty) <= 0) throw new Error("Quantity must be more than zero.");
            const summary = `Move ${fields.qty} ${fields.uom} ${item.sku} ${source.code} → ${target.code}`;
            await enqueue(
              "inventory.move",
              { site_id: state.siteId, location_code: source.code, to_location_code: target.code, reason: reason.value.trim(), ...fields },
              summary,
            );
            say(summary);
            route();
          }),
        },
        field("From bin", from),
        stock.nodes,
        field("To bin", to),
        field("Reason", reason),
        h("div", { class: "actions" }, h("button", { type: "submit" }, "Move")),
      ),
    );
    show(h("h1", {}, "Move stock"), flash(), form, h("div", { class: "actions" }, back()));
  }

  function viewQueue() {
    const rejected = state.queue.filter((c) => c.state === "rejected" && mine(c));
    const waiting = pending();
    show(
      h("h1", {}, "Activity"),
      flash(),
      rejected.length
        ? [
            h("h2", {}, "Not accepted"),
            h("p", { class: "muted" }, "These were refused by the server and changed nothing. Fix the cause and scan again."),
            h(
              "ul",
              { class: "list" },
              rejected.map((c) =>
                h(
                  "li",
                  {},
                  h("strong", {}, c.summary),
                  h("div", {}, `${(c.problem && c.problem.title) || "Rejected"}${c.problem && c.problem.detail ? `: ${c.problem.detail}` : ""}`),
                  h("button", { class: "secondary", onclick: guarded(async () => { await db.dropCommand(c.seq); await dropFiles(c); await refreshQueue(); renderStatus(); route(); }) }, "Dismiss"),
                ),
              ),
            ),
          ]
        : null,
      h("h2", {}, `Waiting to send (${waiting.length})`),
      h("ul", { class: "list" }, waiting.map((c) => h("li", {}, c.summary, " ", h("span", { class: "tag pending" }, "pending")))),
      h("h2", {}, "Recent"),
      h("ul", { class: "list" }, state.history.slice(0, 30).map((e) => h("li", {}, e.summary, " ", h("span", { class: `tag ${e.outcome}` }, e.outcome)))),
      h("div", { class: "actions" }, back()),
    );
  }

  // --- Loading ------------------------------------------------------------------------------------------------
  // Loading checks each SSCC against the trip on the server (which trip it belongs to, what's already loaded),
  // so like packing it works online only.

  function loadOffline() {
    show(h("h1", {}, "Load"), h("p", { class: "flash bad" }, "Loading needs a connection."), h("div", { class: "actions" }, back()));
  }

  async function viewLoadList() {
    if (!state.online) return loadOffline();
    let trips = [];
    try {
      const res = await api("GET", `/trips?site_id=${state.siteId}&status=planning&limit=100`);
      if (res.status !== 200) throw problem(res, "Could not load trips.");
      trips = res.data.items;
    } catch (error) {
      if (error.offline) return loadOffline();
      say(error.message, "bad");
    }
    show(
      h("h1", {}, "Load"),
      flash(),
      h(
        "ul",
        { class: "list" },
        trips.map((t) =>
          h(
            "li",
            {},
            h(
              "a",
              { href: `#load/${t.id}` },
              h("strong", {}, t.number),
              h("div", { class: "muted" }, `${t.vehicle_registration || t.carrier_code || "No vehicle yet"} · ${new Date(t.planned_start).toLocaleString()}`),
            ),
          ),
        ),
      ),
      trips.length ? null : h("p", { class: "muted" }, "No trips are being loaded at this site."),
      h("div", { class: "actions" }, back()),
    );
  }

  async function viewLoad(tripId) {
    if (!state.online) return loadOffline();
    let trip;
    try {
      const res = await api("GET", `/trips/${tripId}`);
      if (res.status !== 200) throw problem(res, "Could not load the trip.");
      trip = res.data;
    } catch (error) {
      if (error.offline) return loadOffline();
      say(error.message, "bad");
      location.hash = "#load";
      return;
    }
    const code = h("input", { autocomplete: "off", inputmode: "numeric", placeholder: "Scan pallet or carton label", autofocus: true });
    const form = h(
      "form",
      {
        onsubmit: guarded(async () => {
          const res = await api("POST", `/trips/${tripId}/load`, { sscc: code.value.trim() });
          if (res.status !== 200) throw problem(res, "Could not load that unit.");
          say(`Loaded ${res.data.sscc} · ${res.data.units_loaded} of ${res.data.units_total}`);
          if (res.data.warning) say(`${res.data.units_loaded} of ${res.data.units_total} loaded. ${res.data.warning}`, "bad");
          route();
        }),
      },
      field("SSCC", code),
      h("div", { class: "actions" }, h("button", { type: "submit" }, "Load")),
    );
    const drops = trip.stops.filter((s) => s.seq > 1);
    show(
      h("h1", {}, `Load ${trip.number}`),
      flash(),
      h("p", {}, h("strong", {}, `${trip.units_loaded} of ${trip.units_total} units loaded`)),
      h("p", { class: "muted" }, trip.vehicle_registration || trip.carrier_code || "No vehicle assigned yet"),
      form,
      h("h2", {}, "Stops"),
      h("ul", { class: "list" }, drops.map((s) => h("li", {}, `${s.seq - 1}. ${s.site_name}`, h("div", { class: "muted" }, s.shipments.map((x) => x.number).join(", "))))),
      h("div", { class: "actions" }, back("#load")),
    );
  }

  // --- Driver -------------------------------------------------------------------------------------------------
  // The driver's trips come from GET /driver/trips and are kept on the device. Actions are queued commands; until
  // the server has them, the cached trip shows their effect so the driver can carry on without signal.

  function isDriver() {
    return Boolean(state.session && state.session.roles.some((r) => r === "driver" || r === "carrier_driver"));
  }

  // A subcontractor's driver opens a link the dispatcher sent them; it signs them in for that one trip.
  async function redeemLink(token) {
    history.replaceState(null, "", `${location.pathname}#`); // keep the token out of history and screenshots
    if (!state.online) {
      say("Open the link again when you have signal.", "bad");
      return route();
    }
    try {
      const res = await api("POST", "/carrier-links/redeem", { token });
      if (res.status !== 200) throw problem(res, "This link doesn't work. Ask the dispatcher for a new one.");
      await startSession(res.data);
    } catch (error) {
      say(error.offline ? "Open the link again when you have signal." : error.message, "bad");
    }
    route();
  }

  function driverMode() {
    return isDriver() && !state.session.roles.some((r) => WAREHOUSE_ROLES.has(r));
  }

  async function pullTrips() {
    const res = await api("GET", "/driver/trips");
    if (res.status !== 200) throw Object.assign(new Error("trips failed"), { status: res.status });
    state.trips = res.data;
    await db.set(`trips:${state.session.user_id}`, state.trips);
    overlayPending();
  }

  function overlayPending() {
    for (const cmd of pending().filter((c) => c.type.startsWith("trip."))) {
      const trip = state.trips.find((t) => t.id === cmd.payload.trip_id);
      if (!trip) continue;
      if (cmd.type === "trip.complete") {
        trip.status = "completed";
        continue;
      }
      const stop = trip.stops.find((s) => s.id === cmd.payload.stop_id);
      if (!stop) continue;
      if (cmd.type === "trip.arrive" && stop.status === "pending") stop.status = "arrived";
      if (cmd.type === "trip.pod") Object.assign(stop, { status: "done", outcome: "delivered" });
      if (cmd.type === "trip.failed") Object.assign(stop, { status: "done", outcome: "failed" });
    }
  }

  function findTrip(tripId) {
    return state.trips.find((t) => t.id === tripId);
  }

  function here() {
    return new Promise((resolve) => {
      if (!navigator.geolocation) return resolve({});
      navigator.geolocation.getCurrentPosition(
        (p) => resolve({ latitude: p.coords.latitude.toFixed(6), longitude: p.coords.longitude.toFixed(6) }),
        () => resolve({}),
        { enableHighAccuracy: true, timeout: 5000, maximumAge: 60000 },
      );
    });
  }

  // Phone photos are several MB; a 1600 px JPEG is plenty for a POD and uploads quickly on a weak signal.
  async function shrink(file) {
    try {
      const bitmap = await createImageBitmap(file);
      const scale = Math.min(1, PHOTO_MAX_PX / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(bitmap.width * scale);
      canvas.height = Math.round(bitmap.height * scale);
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      return (await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.8))) || file;
    } catch {
      return file;
    }
  }

  function photoPicker() {
    const photos = [];
    const count = h("p", { class: "muted" }, "No photos yet.");
    const input = h("input", { type: "file", accept: "image/*", capture: "environment", multiple: true });
    input.addEventListener("change", async () => {
      for (const file of input.files) photos.push(await shrink(file));
      input.value = "";
      count.textContent = `${photos.length} photo${photos.length === 1 ? "" : "s"} taken.`;
    });
    return { photos, nodes: [...field("Photos", input), count] };
  }

  function signaturePad() {
    const canvas = h("canvas", { class: "signature", width: 600, height: 220 });
    const ctx = canvas.getContext("2d");
    let drawing = false;
    let signed = false;
    const point = (event) => {
      const box = canvas.getBoundingClientRect();
      return [((event.clientX - box.left) * canvas.width) / box.width, ((event.clientY - box.top) * canvas.height) / box.height];
    };
    const clear = () => {
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.lineWidth = 3;
      ctx.lineCap = "round";
      ctx.strokeStyle = "#0b1020";
      signed = false;
    };
    canvas.addEventListener("pointerdown", (event) => {
      drawing = true;
      canvas.setPointerCapture(event.pointerId);
      ctx.beginPath();
      ctx.moveTo(...point(event));
    });
    canvas.addEventListener("pointermove", (event) => {
      if (!drawing) return;
      ctx.lineTo(...point(event));
      ctx.stroke();
      signed = true;
    });
    canvas.addEventListener("pointerup", () => (drawing = false));
    clear();
    return {
      nodes: [h("label", {}, "Signature"), canvas, h("button", { type: "button", class: "secondary", onclick: clear }, "Clear signature")],
      signed: () => signed,
      blob: () => new Promise((resolve) => canvas.toBlob(resolve, "image/png")),
    };
  }

  function normaliseSscc(raw) {
    const digits = raw.replace(/\D/g, "");
    return digits.length === 20 && digits.startsWith("00") ? digits.slice(2) : digits;
  }

  function stopTitle(stop) {
    return `${stop.seq - 1}. ${stop.site_name}`;
  }

  function stopTag(stop) {
    const text = stop.outcome || stop.status;
    return h("span", { class: `tag ${stop.outcome === "failed" ? "rejected" : stop.status === "done" ? "applied" : ""}` }, text);
  }

  function viewTrips() {
    const trips = state.trips;
    const rejected = state.queue.filter((c) => c.state === "rejected" && mine(c)).length;
    show(
      h("h1", {}, "My trips"),
      flash(),
      h(
        "ul",
        { class: "list" },
        trips.map((t) => {
          const drops = t.stops.filter((s) => s.seq > 1);
          const done = drops.filter((s) => s.status === "done").length;
          return h(
            "li",
            {},
            h(
              "a",
              { href: `#trip/${t.id}` },
              h("strong", {}, t.number),
              " ",
              h("span", { class: "tag" }, t.status),
              h("div", { class: "muted" }, `${t.vehicle_registration || ""} · ${done} of ${drops.length} stops done · ${new Date(t.planned_start).toLocaleString()}`),
            ),
          );
        }),
      ),
      trips.length ? null : h("p", { class: "muted" }, state.online ? "No trips assigned to you." : "No trips saved on this device yet. Connect to load them."),
      h(
        "div",
        { class: "actions" },
        h("a", { class: "btn secondary", href: "#queue" }, rejected ? `Activity · ${rejected} problem${rejected > 1 ? "s" : ""}` : "Activity"),
        h("button", { class: "secondary", onclick: () => syncNow() }, "Sync now"),
        h("button", { class: "secondary", onclick: guarded(signOut) }, `Sign out ${state.session.email}`),
      ),
    );
  }

  function viewTrip(tripId) {
    const trip = findTrip(tripId);
    if (!trip) return viewTrips();
    const drops = trip.stops.filter((s) => s.seq > 1);
    const allDone = drops.every((s) => s.status === "done");
    const onRoad = trip.status === "dispatched";
    show(
      h("h1", {}, trip.number),
      flash(),
      h("p", { class: "muted" }, `${trip.vehicle_registration || ""} · ${trip.status}`),
      trip.status === "planning" ? h("p", { class: "flash bad" }, "This trip hasn't been dispatched yet.") : null,
      h(
        "ul",
        { class: "list" },
        drops.map((s) =>
          h(
            "li",
            {},
            h(
              "a",
              { href: `#stop/${trip.id}/${s.id}` },
              h("strong", {}, stopTitle(s)),
              " ",
              stopTag(s),
              h("div", { class: "muted" }, s.address || s.site_code),
              s.window_from ? h("div", { class: "muted" }, `Window ${new Date(s.window_from).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}–${s.window_to ? new Date(s.window_to).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}`) : null,
            ),
          ),
        ),
      ),
      h(
        "div",
        { class: "actions" },
        onRoad && allDone
          ? h(
              "button",
              {
                onclick: guarded(async () => {
                  if (!confirm(`Finish ${trip.number}? Bring back anything that wasn't delivered.`)) return;
                  await enqueue("trip.complete", { trip_id: trip.id, completed_at: new Date().toISOString() }, `${trip.number}: complete`);
                  overlayPending();
                  say(`${trip.number} finished`);
                  location.hash = "#";
                }),
              },
              "Finish trip",
            )
          : null,
        back("#"),
      ),
    );
  }

  function mapsLink(stop) {
    const where = stop.latitude && stop.longitude ? `${stop.latitude},${stop.longitude}` : `${stop.site_name}, ${stop.address}`;
    return `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(where)}`;
  }

  function viewStop(tripId, stopId) {
    const trip = findTrip(tripId);
    const stop = trip && trip.stops.find((s) => s.id === stopId);
    if (!stop) return viewTrips();
    const open = trip.status === "dispatched" && stop.status !== "done";
    show(
      h("h1", {}, stopTitle(stop)),
      flash(),
      h("p", {}, stop.address || stop.site_code, " ", stopTag(stop)),
      h("p", {}, h("a", { href: mapsLink(stop), target: "_blank", rel: "noopener" }, "Open in maps")),
      h("h2", {}, "To drop"),
      h(
        "ul",
        { class: "list" },
        stop.shipments.map((x) =>
          h("li", {}, h("strong", {}, x.number), x.client_ref ? ` · ${x.client_ref}` : "", h("div", { class: "muted" }, `${x.units.length} unit${x.units.length === 1 ? "" : "s"} · order ${x.order_number}`)),
        ),
      ),
      h(
        "div",
        { class: "actions" },
        open && stop.status === "pending"
          ? h(
              "button",
              {
                class: "secondary",
                onclick: guarded(async () => {
                  const at = { arrived_at: new Date().toISOString(), ...(await here()) };
                  await enqueue("trip.arrive", { trip_id: tripId, stop_id: stopId, ...at }, `${trip.number} ${stopTitle(stop)}: arrived`);
                  overlayPending();
                  route();
                }),
              },
              "Arrived",
            )
          : null,
        open ? h("a", { class: "btn", href: `#pod/${tripId}/${stopId}` }, "Deliver") : null,
        open ? h("a", { class: "btn secondary", href: `#fail/${tripId}/${stopId}` }, "Could not deliver") : null,
        back(`#trip/${tripId}`),
      ),
    );
  }

  function viewPod(tripId, stopId) {
    const trip = findTrip(tripId);
    const stop = trip && trip.stops.find((s) => s.id === stopId);
    if (!stop || stop.status === "done") return viewStop(tripId, stopId);
    const units = stop.shipments.flatMap((x) => x.units.map((u) => ({ ...u, shipment: x.number })));
    const outcome = new Map(units.map((u) => [u.sscc, ""]));
    const selects = new Map();
    const scan = h("input", { autocomplete: "off", inputmode: "numeric", placeholder: "Scan each pallet or carton", autofocus: true });
    scan.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const code = normaliseSscc(scan.value);
      scan.value = "";
      if (!outcome.has(code)) return alert(`${code || "That label"} is not for this stop.`);
      outcome.set(code, "delivered");
      selects.get(code).value = "delivered";
    });
    const list = h(
      "ul",
      { class: "list" },
      units.map((u) => {
        const select = h(
          "select",
          { onchange: () => outcome.set(u.sscc, select.value) },
          h("option", { value: "" }, "Not scanned (missing)"),
          h("option", { value: "delivered" }, "Delivered"),
          h("option", { value: "damaged" }, "Delivered damaged"),
          h("option", { value: "refused" }, "Refused"),
        );
        selects.set(u.sscc, select);
        return h("li", {}, h("strong", {}, u.sscc), h("div", { class: "muted" }, `${u.kind} · ${u.shipment}`), select);
      }),
    );
    const allDelivered = h(
      "button",
      {
        type: "button",
        class: "secondary",
        onclick: () => {
          for (const [code, select] of selects) {
            outcome.set(code, "delivered");
            select.value = "delivered";
          }
        },
      },
      "Mark all delivered",
    );
    const name = h("input", { autocomplete: "off", placeholder: "Who received the goods?", required: true });
    const note = h("input", { autocomplete: "off", placeholder: "Anything to report (optional)" });
    const pad = signaturePad();
    const camera = photoPicker();
    const form = h(
      "form",
      {
        onsubmit: guarded(async () => {
          if (!name.value.trim()) throw new Error("Enter the recipient's name.");
          if (!pad.signed() && !camera.photos.length) throw new Error("Get a signature or take a photo.");
          const missing = [...outcome.values()].filter((v) => !v).length;
          if (missing && !confirm(`${missing} unit${missing === 1 ? "" : "s"} not scanned will be reported missing. Continue?`)) return;
          const where = await here();
          const files = [];
          if (pad.signed()) files.push({ file_id: await saveFile(await pad.blob(), "signature.png", where), kind: "pod_signature", field: "signature" });
          for (const [n, photo] of camera.photos.entries()) {
            files.push({ file_id: await saveFile(photo, `pod-${n + 1}.jpg`, where), kind: "pod_photo", field: "photo" });
          }
          const payload = {
            trip_id: tripId,
            stop_id: stopId,
            recipient_name: name.value.trim(),
            signed_at: new Date().toISOString(),
            ...where,
            units: [...outcome].filter(([, v]) => v).map(([sscc, status]) => ({ sscc, status })),
            note: note.value.trim(),
          };
          await enqueue("trip.pod", payload, `${trip.number} ${stopTitle(stop)}: delivered to ${payload.recipient_name}`, files);
          overlayPending();
          say(state.online ? "Delivery recorded." : "Delivery saved. It will be sent when you have signal.");
          location.hash = `#trip/${tripId}`;
        }),
      },
      field("Scan", scan),
      list,
      allDelivered,
      field("Recipient", name),
      pad.nodes,
      camera.nodes,
      field("Note", note),
      h("div", { class: "actions" }, h("button", { type: "submit" }, "Confirm delivery")),
    );
    show(h("h1", {}, `Deliver: ${stop.site_name}`), flash(), form, h("div", { class: "actions" }, back(`#stop/${tripId}/${stopId}`)));
  }

  function viewFail(tripId, stopId) {
    const trip = findTrip(tripId);
    const stop = trip && trip.stops.find((s) => s.id === stopId);
    if (!stop || stop.status === "done") return viewStop(tripId, stopId);
    const reason = h("select", { required: true }, h("option", { value: "" }, "Choose a reason"), FAILURE_REASONS.map(([value, label]) => h("option", { value }, label)));
    const note = h("input", { autocomplete: "off", placeholder: "What happened?" });
    const camera = photoPicker();
    const form = h(
      "form",
      {
        onsubmit: guarded(async () => {
          if (!reason.value) throw new Error("Choose a reason.");
          if (reason.value === "other" && !note.value.trim()) throw new Error("Say what happened.");
          const where = await here();
          const files = [];
          for (const [n, photo] of camera.photos.entries()) {
            files.push({ file_id: await saveFile(photo, `failed-${n + 1}.jpg`, where), kind: "pod_photo", field: "photo" });
          }
          const payload = { trip_id: tripId, stop_id: stopId, reason: reason.value, note: note.value.trim(), failed_at: new Date().toISOString(), ...where };
          await enqueue("trip.failed", payload, `${trip.number} ${stopTitle(stop)}: not delivered (${reason.value})`, files);
          overlayPending();
          say("Failed delivery recorded. Bring the goods back.");
          location.hash = `#trip/${tripId}`;
        }),
      },
      field("Reason", reason),
      field("Note", note),
      camera.nodes,
      h("div", { class: "actions" }, h("button", { type: "submit", class: "danger" }, "Record failed delivery")),
    );
    show(h("h1", {}, `Could not deliver: ${stop.site_name}`), flash(), form, h("div", { class: "actions" }, back(`#stop/${tripId}/${stopId}`)));
  }

  function routeDriver() {
    const [page, a, b] = location.hash.slice(1).split("/");
    switch (page) {
      case "trip":
        return viewTrip(a);
      case "stop":
        return viewStop(a, b);
      case "pod":
        return viewPod(a, b);
      case "fail":
        return viewFail(a, b);
      case "queue":
        return viewQueue();
      default:
        return viewTrips();
    }
  }

  // --- Session ------------------------------------------------------------------------------------------------

  async function startSession(session) {
    state.session = session;
    await db.set("session", session);
    state.history = (await db.get(`history:${session.user_id}`)) || [];
    if (driverMode()) {
      state.trips = (await db.get(`trips:${session.user_id}`)) || [];
      await refreshQueue();
      overlayPending();
      syncSoon();
      return;
    }
    let res = null;
    try {
      res = await api("GET", "/sites?limit=200");
    } catch {
      // offline start: fall back to the sites saved last time
    }
    if (res && res.status === 200) {
      state.sites = res.data.items.filter((s) => s.is_active && s.kind === "warehouse");
      await db.set("sites", state.sites);
    } else {
      state.sites = (await db.get("sites")) || [];
    }
    const saved = await db.get(`site:${session.user_id}`);
    state.siteId = state.sites.some((s) => s.id === saved) ? saved : state.sites.length === 1 ? state.sites[0].id : null;
    await loadRef();
    indexItems();
    await refreshQueue();
    syncSoon();
  }

  async function chooseSite(siteId) {
    state.siteId = siteId;
    await db.set(`site:${state.session.user_id}`, siteId);
    await loadRef();
    indexItems();
    location.hash = "#";
    route();
    await syncNow();
    route();
  }

  async function signOut() {
    const waiting = pending().length;
    if (waiting && !confirm(`${waiting} actions have not been sent yet. They will be sent the next time you sign in on this device. Sign out?`)) return;
    try {
      await api("DELETE", "/auth/session");
    } catch {
      // Offline: the server session expires on its own; the device forgets it now.
    }
    state.session = null;
    await db.del("session");
    location.hash = "#";
    route();
  }

  // --- Routing ------------------------------------------------------------------------------------------------

  function route() {
    renderStatus();
    if (location.hash.startsWith("#link/")) return void redeemLink(location.hash.slice("#link/".length));
    if (!state.session) return viewLogin();
    if (driverMode()) return routeDriver();
    if (!state.siteId || location.hash === "#site") return viewSites();
    const [page, id] = location.hash.slice(1).split("/");
    switch (page) {
      case "receive":
        return id ? viewReceive(id) : viewReceiveList();
      case "putaway":
        return id ? viewPutaway(id) : viewPutawayList();
      case "pick":
        return id ? viewPick(id) : viewPickList();
      case "pack":
        return id ? viewPackOrder(id, location.hash.split("/")[2]) : viewPackList();
      case "load":
        return id ? viewLoad(id) : viewLoadList();
      case "count":
        return id ? viewCount(id) : viewCountList();
      case "move":
        return viewMove();
      case "queue":
        return viewQueue();
      default:
        return viewHome();
    }
  }

  async function boot() {
    await db.open();
    state.deviceId = (await db.get("device_id")) || uuid4();
    await db.set("device_id", state.deviceId);
    await refreshQueue();
    try {
      const res = await api("GET", "/auth/session");
      if (res.status === 200) await startSession(res.data);
      else await db.del("session");
    } catch {
      const cached = await db.get("session"); // offline start: keep working with the last signed-in user
      if (cached) await startSession(cached);
    }
    route();
    window.addEventListener("hashchange", route);
    window.addEventListener("online", () => syncNow());
    window.addEventListener("offline", () => setOnline(false));
    setInterval(syncNow, SYNC_EVERY_MS);
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register(document.body.dataset.sw).catch((error) => console.warn("service worker", error));
    }
  }

  boot().catch((error) => {
    console.error(error);
    app.replaceChildren(h("p", { class: "flash bad" }, "The scanner could not start. Reload the page."));
  });
})();
