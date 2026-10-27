// Scanner PWA: offline-first warehouse actions over /api/v1/sync.
//
// Every write is a command with a device-generated id, queued in IndexedDB and pushed in order whenever the
// device is online. The server applies each command once (a retry gets the stored outcome), so a lost
// response or a flaky connection never double-books stock. Reference data (bins, items, ASNs, tasks,
// counts) is pulled per site and kept locally so the floor keeps working without Wi-Fi.
"use strict";

(() => {
  const API = "/api/v1";
  const CSRF_COOKIE = document.body.dataset.csrfCookie || "csrftoken";
  const SYNC_EVERY_MS = 15000;
  const FULL_PULL_EVERY_MS = 24 * 3600 * 1000;
  const PUSH_BATCH = 50;
  const HISTORY_SIZE = 100;
  const CLOSED = new Set(["closed", "cancelled", "done", "short", "posted", "rejected", "awaiting_approval"]);

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
        const req = indexedDB.open("scanner", 1);
        req.onupgradeneeded = () => {
          req.result.createObjectStore("kv");
          req.result.createObjectStore("queue", { keyPath: "seq", autoIncrement: true });
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
        headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrfToken() },
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

  async function enqueue(type, payload, summary) {
    await db.putCommand({
      command_id: uuid4(),
      type,
      payload,
      summary,
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

  async function pushQueue() {
    for (;;) {
      await refreshQueue();
      const batch = pending().slice(0, PUSH_BATCH);
      if (!batch.length) return;
      const res = await api("POST", "/sync/push", {
        device_id: state.deviceId,
        commands: batch.map((c) => ({ command_id: c.command_id, type: c.type, payload: c.payload, recorded_at: c.recorded_at })),
      });
      if (res.status !== 200) throw Object.assign(new Error("push failed"), { status: res.status });
      for (const result of res.data.results) {
        const cmd = batch.find((c) => c.command_id === result.command_id);
        if (result.status === "applied") {
          await db.dropCommand(cmd.seq);
          await remember(cmd, "applied");
        } else if (result.status === "rejected") {
          cmd.state = "rejected";
          cmd.problem = result.problem;
          await db.putCommand(cmd);
          await remember(cmd, "rejected");
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
      if (state.siteId) {
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
                  h("button", { class: "secondary", onclick: guarded(async () => { await db.dropCommand(c.seq); await refreshQueue(); renderStatus(); route(); }) }, "Dismiss"),
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

  // --- Session ------------------------------------------------------------------------------------------------

  async function startSession(session) {
    state.session = session;
    await db.set("session", session);
    state.history = (await db.get(`history:${session.user_id}`)) || [];
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
    if (!state.session) return viewLogin();
    if (!state.siteId || location.hash === "#site") return viewSites();
    const [page, id] = location.hash.slice(1).split("/");
    switch (page) {
      case "receive":
        return id ? viewReceive(id) : viewReceiveList();
      case "putaway":
        return id ? viewPutaway(id) : viewPutawayList();
      case "pick":
        return id ? viewPick(id) : viewPickList();
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
