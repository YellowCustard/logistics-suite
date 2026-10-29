// Nuronteq admin shell: loading feedback, sidebar collapse, drawer and command palette. No dependencies.
"use strict";
(() => {
  const root = document.documentElement;

  /* Loading feedback: a top bar on every same-origin navigation, busy state on submitters. */
  const bar = document.getElementById("nq-progress");
  let barTimer;
  const progress = {
    start() {
      if (!bar) return;
      bar.dataset.state = "";
      void bar.offsetWidth; // restart the transition
      bar.dataset.state = "loading";
      clearTimeout(barTimer);
      barTimer = setTimeout(progress.done, 15000); // downloads never fire pageshow
    },
    done() {
      if (bar && bar.dataset.state === "loading") bar.dataset.state = "done";
    },
  };

  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if ((a.target && a.target !== "_self") || a.hasAttribute("download")) return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin || !/^https?:$/.test(url.protocol)) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return; // in-page anchor
    progress.start();
  });

  document.addEventListener("submit", (e) => {
    if (e.defaultPrevented) return;
    const form = e.target;
    if (form.dataset.nqSubmitting) {
      e.preventDefault(); // double-submit guard
      return;
    }
    form.dataset.nqSubmitting = "1";
    // Never disable or relabel the submitter: Django reads its name (_save, _continue) and the action "index" value.
    const submitter = e.submitter;
    submitter?.setAttribute("aria-busy", "true");
    setTimeout(() => {
      delete form.dataset.nqSubmitting;
      submitter?.removeAttribute("aria-busy");
    }, 15000);
    progress.start();
  });

  window.addEventListener("pageshow", () => {
    // Also runs on back/forward cache restores, where the old busy state would otherwise stick.
    progress.done();
    document.querySelectorAll("form[data-nq-submitting]").forEach((f) => delete f.dataset.nqSubmitting);
    document.querySelectorAll('[aria-busy="true"]').forEach((el) => el.removeAttribute("aria-busy"));
  });

  /* Sidebar: collapse to a rail; a section icon in the rail expands the sidebar on that section. */
  function setSidebar(state) {
    root.dataset.nqSidebar = state;
    document
      .querySelectorAll("[data-nq-toggle-sidebar]")
      .forEach((b) => b.setAttribute("aria-expanded", String(state === "expanded")));
    try {
      localStorage.setItem("nq.sidebar", state);
    } catch {
      // Storage blocked: the state lasts for this page only.
    }
  }
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-nq-toggle-sidebar]")) {
      setSidebar(root.dataset.nqSidebar === "collapsed" ? "expanded" : "collapsed");
      return;
    }
    const summary = e.target.closest("#nq-sidebar summary");
    if (summary && root.dataset.nqSidebar === "collapsed") {
      e.preventDefault();
      setSidebar("expanded");
      summary.parentElement.open = true;
    }
  });

  /* Desktop account menu: close on outside click and Escape. */
  document.addEventListener("click", (e) => {
    document.querySelectorAll("details.nq-menu[open]").forEach((d) => {
      if (!d.contains(e.target)) d.open = false;
    });
  });

  /* Dialogs (drawer, palette): showModal() gives the focus trap, Escape and an inert page. */
  let opener = null;
  function openDialog(id, from) {
    const d = document.getElementById(id);
    if (!d || d.open) return;
    opener = from || document.activeElement;
    d.showModal();
    if (id === "nq-palette") palette.reset();
  }
  document.addEventListener("click", (e) => {
    const o = e.target.closest("[data-nq-open]");
    if (o) {
      openDialog(o.dataset.nqOpen, o);
      return;
    }
    const c = e.target.closest("[data-nq-close]");
    if (c) {
      c.closest("dialog")?.close();
      return;
    }
    // A click on the backdrop targets the dialog element itself; its content fills the rest.
    if (e.target instanceof HTMLDialogElement && e.target.classList.contains("nq-ui")) e.target.close();
  });
  document.querySelectorAll("dialog.nq-ui").forEach((d) =>
    d.addEventListener("close", () => {
      opener?.focus?.();
      opener = null;
    }),
  );

  /* Command palette over the lists and add forms this user may open (rendered by nq_palette_data). */
  const dataEl = document.getElementById("nq-palette-data");
  const data = dataEl ? JSON.parse(dataEl.textContent) : { items: [], ids: {} };
  const lists = new Map(data.items.filter((i) => !i.key.endsWith(":add")).map((i) => [i.key, i]));
  const ID_RE = /^([A-Z]{2,3})-\d{4}(-\d{1,6})?$/i;
  const input = document.getElementById("nq-palette-input");
  const list = document.getElementById("nq-palette-list");
  const status = document.getElementById("nq-palette-status");
  let results = [];
  let active = 0;

  function search(raw) {
    const q = raw.trim();
    const out = [];
    const id = q.match(ID_RE);
    const target = id && lists.get(data.ids[id[1].toUpperCase()]);
    if (target) {
      const n = q.toUpperCase();
      out.push({ group: "Go to record", label: `Find ${n} in ${target.label}`, hint: "Search", url: `${target.url}?q=${encodeURIComponent(n)}` });
    }
    const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
    for (const item of data.items) {
      const hay = `${item.label} ${item.group}`.toLowerCase();
      if (terms.every((t) => hay.includes(t))) out.push(item);
    }
    return out;
  }

  function render() {
    const rows = [];
    let group = null;
    results.forEach((r, i) => {
      if (r.group !== group) {
        group = r.group;
        const h = document.createElement("li");
        h.setAttribute("role", "presentation");
        h.className = "nq-pal-group";
        h.textContent = group;
        rows.push(h);
      }
      const li = document.createElement("li");
      li.id = `nq-opt-${i}`;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === active));
      li.className = "nq-pal-opt";
      li.dataset.index = String(i);
      const label = document.createElement("span");
      label.className = "nq-pal-label";
      label.textContent = r.label;
      const hint = document.createElement("span");
      hint.className = "nq-pal-hint";
      hint.textContent = r.hint || "";
      li.append(label, hint);
      rows.push(li);
    });
    if (!results.length) {
      const empty = document.createElement("li");
      empty.setAttribute("role", "presentation");
      empty.className = "nq-pal-group";
      empty.textContent = "No matches. Try a model name or a number such as ORD-2026-000412.";
      rows.push(empty);
    }
    list.replaceChildren(...rows);
    if (results.length) input.setAttribute("aria-activedescendant", `nq-opt-${active}`);
    else input.removeAttribute("aria-activedescendant");
    status.textContent = results.length ? `${results.length} results` : "No results";
    document.getElementById(`nq-opt-${active}`)?.scrollIntoView({ block: "nearest" });
  }

  function go(url) {
    progress.start();
    location.assign(url);
  }

  const palette = {
    reset() {
      if (!input) return;
      input.value = "";
      results = search("");
      active = 0;
      render();
      input.focus();
    },
  };

  input?.addEventListener("input", () => {
    results = search(input.value);
    active = 0;
    render();
  });
  input?.addEventListener("keydown", (e) => {
    if ((e.key === "ArrowDown" || e.key === "ArrowUp") && results.length) {
      e.preventDefault();
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + results.length) % results.length;
      render();
    } else if (e.key === "Enter" && results[active]) {
      e.preventDefault();
      go(results[active].url);
    }
  });
  list?.addEventListener("click", (e) => {
    const opt = e.target.closest('[role="option"]');
    if (opt) go(results[Number(opt.dataset.index)].url);
  });

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "k" && dataEl) {
      e.preventDefault();
      openDialog("nq-palette");
    } else if (e.key === "Escape") {
      document.querySelectorAll("details.nq-menu[open]").forEach((d) => {
        d.open = false;
      });
    }
  });

  const platform = navigator.userAgentData?.platform || navigator.platform || "";
  if (/mac|iphone|ipad/i.test(platform)) {
    document.querySelectorAll("[data-nq-shortcut]").forEach((k) => {
      k.textContent = "⌘K";
    });
  }
})();
