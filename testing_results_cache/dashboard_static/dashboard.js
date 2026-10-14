"use strict";

(function () {
  const SOURCE = "dashboard.json";
  const REFRESH_MS = 60000;
  const DAY_MS = 86400000;
  const LATEST_ROWS = 12;
  const THEME_KEY = "cardano-test-runs-theme";
  const SVG = "http://www.w3.org/2000/svg";

  let data = null;
  let days = 30;
  let kind = "all";
  let source = "all";
  let only = null;

  // Small DOM helpers. Data is only ever set as text, never as markup.
  const svgEl = (tag, attrs, text) => {
    const n = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const h = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const byId = (id) => document.getElementById(id);
  const fmt = (n) => Math.round(n).toLocaleString("en-US");
  const compact = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e4 ? `${(n / 1e3).toFixed(0)}k` : fmt(n));
  const dur = (s) => {
    const hh = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return hh ? `${fmt(hh)}h ${m}m` : `${m}m`;
  };
  const stamp = (iso) => iso.slice(0, 16).replace("T", " ");
  const dateOf = (ms) => new Date(ms).toISOString().slice(0, 10);

  function valid(doc) {
    return Boolean(doc) && doc.schema === 1 && typeof doc.generated_at === "string" && doc.all_time && doc.all_time.by_source &&
      ["test_runs", "daily", "latest", "coverage", "releases"].every((k) => Array.isArray(doc[k]));
  }

  // A testrun is a name plus its source: a local "nightly" is not the CI "nightly".
  const keyOf = (e) => `${e.name}|${e.source}`;
  function typeOf(name) {
    const t = data.test_runs.find((r) => r.name === name);
    return t ? t.type : "regression";
  }
  const isRelease = (name) => data.test_runs.some((r) => r.name === name && r.release);
  const kindMatch = (e) => kind === "all" || typeOf(e.name) === kind;
  const sourceMatch = (e) => source === "all" || e.source === source;
  const filterMatch = (e) => kindMatch(e) && sourceMatch(e) && (!only || keyOf(e) === only);

  function niceMax(v) {
    const p = Math.pow(10, Math.floor(Math.log10(Math.max(v, 1))));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }

  const W = 560;
  const H = 220;
  const PAD = { t: 10, r: 10, b: 24, l: 52 };
  const PW = W - PAD.l - PAD.r;
  const PH = H - PAD.t - PAD.b;

  function axes(svg, rows, lo, hi) {
    for (const f of [0, 0.5, 1]) {
      const y = PAD.t + PH - f * PH;
      svg.appendChild(svgEl("line", { class: f ? "grid" : "axis", x1: PAD.l, x2: W - PAD.r, y1: y, y2: y }));
      svg.appendChild(svgEl("text", { class: "tick", x: PAD.l - 6, y: y + 4, "text-anchor": "end" }, compact(lo + (hi - lo) * f)));
    }
    const step = PW / rows.length;
    const every = Math.ceil(rows.length / 6);
    rows.forEach((r, i) => {
      if (i % every === 0 || i === rows.length - 1) {
        svg.appendChild(svgEl("text", { class: "tick", x: PAD.l + i * step + step / 2, y: H - 6, "text-anchor": "middle" }, r.date.slice(5)));
      }
    });
    return step;
  }

  function empty(svg) {
    svg.appendChild(svgEl("text", { class: "empty", x: W / 2, y: H / 2, "text-anchor": "middle" }, "No runs in this window"));
  }

  function bars(container, rows, series, unit) {
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` });
    const totals = rows.map((r) => series.reduce((s, k) => s + r[k], 0));
    const max = niceMax(Math.max(...totals));
    const step = axes(svg, rows, 0, max);
    const bw = Math.max(1, step * 0.72);
    rows.forEach((r, i) => {
      const x = PAD.l + i * step + (step - bw) / 2;
      let y = PAD.t + PH;
      for (const k of series) {
        const hh = (r[k] / max) * PH;
        if (hh <= 0) continue;
        y -= hh;
        const rect = svgEl("rect", { class: k, x, y, width: bw, height: hh });
        rect.appendChild(svgEl("title", {}, `${r.date}: ${fmt(r[k])} ${unit(k)}`));
        svg.appendChild(rect);
      }
    });
    if (totals.every((t) => t === 0)) empty(svg);
    container.replaceChildren(svg);
  }

  function line(container, rows, key, opts) {
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` });
    const vals = rows.map((r) => r[key]).filter((v) => v !== null);
    if (!vals.length) {
      axes(svg, rows, 0, 1);
      empty(svg);
      container.replaceChildren(svg);
      return;
    }
    const lo = Math.floor(Math.min(...vals) * 0.98);
    const hi = Math.max(niceMax(Math.max(...vals)), lo + 1);
    const step = axes(svg, rows, lo, hi);
    const pts = [];
    rows.forEach((r, i) => {
      if (r[key] !== null) pts.push([PAD.l + i * step + step / 2, PAD.t + PH - ((r[key] - lo) / (hi - lo)) * PH]);
    });
    if (opts.area) {
      const d = `M${pts[0][0]},${PAD.t + PH} ` + pts.map((p) => `L${p[0]},${p[1]}`).join(" ") + ` L${pts[pts.length - 1][0]},${PAD.t + PH} Z`;
      svg.appendChild(svgEl("path", { class: "area", d }));
    }
    svg.appendChild(svgEl("polyline", { class: opts.cls, points: pts.map((p) => p.join(",")).join(" ") }));
    const last = pts[pts.length - 1];
    svg.appendChild(svgEl("circle", { class: opts.dot, cx: last[0], cy: last[1], r: 4 }));
    svg.appendChild(svgEl("text", { class: "tick", x: last[0] - 6, y: last[1] - 8, "text-anchor": "end" }, compact(vals[vals.length - 1])));
    container.replaceChildren(svg);
  }

  function typeCell(e, steps) {
    const c = h("td");
    const t = typeOf(e.name);
    c.append(h("span", `tag t-${t}`, t === "upgrade" ? "Upgrade" : "Regression"));
    if (steps > 1) c.append(h("span", "steps", `${steps} steps`));
    if (e.source === "local") c.append(h("span", "tag local", "Local"));
    else if (isRelease(e.name)) c.append(h("span", "tag release", "Release"));
    return c;
  }

  function noRows(cols) {
    const tr = h("tr");
    const c = h("td", "none", "No runs in this window");
    c.colSpan = cols;
    tr.append(c);
    return [tr];
  }

  function renderRunList(windowDaily) {
    const counts = new Map();
    for (const d of windowDaily) {
      if (kindMatch(d) && sourceMatch(d)) counts.set(keyOf(d), (counts.get(keyOf(d)) || 0) + d.runs);
    }
    if (only && !counts.has(only)) only = null;
    const total = [...counts.values()].reduce((s, n) => s + n, 0);
    const split = (key) => { const i = key.lastIndexOf("|"); return { name: key.slice(0, i), source: key.slice(i + 1) }; };
    const order = (k) => { const e = split(k); return [e.source === "local", isRelease(e.name), e.name]; };
    const keys = [...counts.keys()].sort((a, b) => {
      const x = order(a);
      const y = order(b);
      return (x[0] - y[0]) || (x[1] - y[1]) || x[2].localeCompare(y[2]);
    });
    const items = [["All test runs", null, total, null], ...keys.map((k) => [split(k).name, k, counts.get(k), split(k)])];
    byId("runlist").replaceChildren(...items.map(([label, value, n, e]) => {
      const li = h("li");
      const b = h("button");
      b.type = "button";
      b.setAttribute("aria-pressed", String(only === value));
      const kindCls = e ? (e.source === "local" ? "kind local" : `kind ${typeOf(e.name)}`) : "kind";
      b.append(h("span", kindCls), h("span", "nm", e && e.source === "local" ? `${label} (local)` : label), h("span", "ct", fmt(n)));
      b.addEventListener("click", () => { only = value; render(); });
      li.append(b);
      return li;
    }));
  }

  function renderStatic() {
    byId("a-runs").textContent = fmt(data.all_time.runs);
    byId("a-cases").textContent = fmt(data.all_time.tests);
    byId("a-hours").textContent = fmt(data.all_time.seconds / 3600);
    const ci = data.all_time.by_source.ci;
    const local = data.all_time.by_source.local;
    byId("a-runs-hint").textContent = `${fmt(ci.runs)} CI · ${fmt(local.runs)} local`;
    byId("a-cases-hint").textContent = `${fmt(ci.tests)} CI · ${fmt(local.tests)} local`;
    byId("a-hours-hint").textContent = `${fmt(ci.seconds / 3600)} CI · ${fmt(local.seconds / 3600)} local`;
    const releases = data.releases.map((r) => {
      const li = h("li");
      const left = h("span");
      left.append(h("b", "", r.version ? `cardano-node ${r.version}` : r.name));
      li.append(left, h("span", "sub", `${fmt(r.runs)} run${r.runs === 1 ? "" : "s"} · ${fmt(r.tests)} tests · ${r.last.slice(0, 10)}`));
      return li;
    });
    byId("releases").replaceChildren(...(releases.length ? releases : [h("li", "sub", "No release runs yet")]));
  }

  function render() {
    if (!data) return;
    const ref = Date.parse(data.generated_at.slice(0, 10));
    const start = dateOf(ref - (days - 1) * DAY_MS);
    const windowDaily = data.daily.filter((d) => d.date >= start);
    renderRunList(windowDaily);
    const daily = windowDaily.filter(filterMatch);

    const onlyLabel = only ? only.replace(/\|ci$/, "").replace(/\|local$/, " (local)") : null;
    byId("heading").textContent = onlyLabel || (kind === "all" ? "Cardano testing at a glance" : kind === "upgrade" ? "Upgrade testing" : "Regression testing");
    byId("subheading").textContent = `Last ${days} days · ${source === "all" ? "CI and local runs" : source === "ci" ? "CI runs" : "local runs"}`;

    const sum = (list, k) => list.reduce((s, d) => s + d[k], 0);
    const runs = sum(daily, "runs");
    const upgradeRuns = sum(daily.filter((d) => typeOf(d.name) === "upgrade"), "runs");
    const localRuns = sum(daily.filter((d) => d.source === "local"), "runs");
    byId("t-runs").textContent = fmt(runs);
    byId("t-runs-hint").textContent = `${fmt(runs - upgradeRuns)} regression · ${fmt(upgradeRuns)} upgrade · ${fmt(localRuns)} local`;
    byId("t-cases").textContent = fmt(sum(daily, "tests"));
    byId("t-time").textContent = dur(sum(daily, "seconds"));

    const cov = data.coverage.filter((c) => c.date >= start);
    if (cov.length) {
      const first = cov[0].pct;
      const last = cov[cov.length - 1].pct;
      const change = last - first;
      byId("t-cov").textContent = `${last.toFixed(1)}%`;
      byId("t-cov-hint").textContent = `${change >= 0 ? "+" : ""}${change.toFixed(1)} points in ${days} days`;
    } else {
      byId("t-cov").textContent = "–";
      byId("t-cov-hint").textContent = "no coverage report in this window";
    }

    const allTimeTests = data.test_runs.filter(filterMatch).reduce((s, t) => s + t.tests, 0);
    let running = allTimeTests - sum(daily, "tests");
    const rows = [];
    for (let i = days - 1; i >= 0; i -= 1) {
      const date = dateOf(ref - i * DAY_MS);
      const today = daily.filter((d) => d.date === date);
      const tests = sum(today, "tests");
      running += tests;
      const suiteCandidates = today.filter((d) => d.source === "ci" && typeOf(d.name) === "regression" && !isRelease(d.name) && d.runs > 0);
      rows.push({
        date,
        regression: sum(today.filter((d) => typeOf(d.name) === "regression"), "runs"),
        upgrade: sum(today.filter((d) => typeOf(d.name) === "upgrade"), "runs"),
        tests,
        cumul: running,
        suite: suiteCandidates.length ? Math.max(...suiteCandidates.map((d) => d.tests / d.runs)) : null,
      });
    }
    bars(byId("chart-runs"), rows, ["regression", "upgrade"], (k) => `${k} runs`);
    bars(byId("chart-tests"), rows, ["tests"], () => "tests");
    line(byId("chart-cumul"), rows, "cumul", { cls: "lineA", dot: "endptA", area: false });
    line(byId("chart-suite"), rows, "suite", { cls: "lineS", dot: "endpt", area: true });

    const groups = new Map();
    for (const d of daily) {
      const g = groups.get(keyOf(d)) || { e: d, runs: 0, tests: 0, seconds: 0 };
      g.runs += d.runs;
      g.tests += d.tests;
      g.seconds += d.seconds;
      groups.set(keyOf(d), g);
    }
    const lastRun = (key) => (data.test_runs.find((t) => keyOf(t) === key) || {}).last || "";
    const byName = [...groups.entries()]
      .sort((a, b) => ((a[1].e.source === "local") - (b[1].e.source === "local")) || (isRelease(a[1].e.name) - isRelease(b[1].e.name)) || b[1].runs - a[1].runs)
      .map(([key, g]) => {
        const tr = h("tr");
        tr.append(h("td", "", g.e.name), typeCell(g.e, 1), h("td", "num", fmt(g.runs)), h("td", "num", fmt(g.tests)),
          h("td", "num", dur(g.seconds)), h("td", "num", fmt(g.tests / g.runs)), h("td", "mono", stamp(lastRun(key))));
        return tr;
      });
    byId("by-name").replaceChildren(...(byName.length ? byName : noRows(7)));

    const latest = data.latest
      .filter((r) => r.started.slice(0, 10) >= start && filterMatch(r))
      .slice(0, LATEST_ROWS)
      .map((r) => {
        const tr = h("tr");
        tr.append(h("td", "mono", stamp(r.started)), h("td", "", r.name), typeCell(r, r.steps),
          h("td", "mono", r.node || "–"), h("td", "num", fmt(r.tests)), h("td", "num", dur(r.seconds)));
        return tr;
      });
    byId("latest").replaceChildren(...(latest.length ? latest : noRows(6)));
    renderUpdated();
  }

  function renderUpdated(problem) {
    if (!data) {
      byId("updated").textContent = problem || "Loading…";
      return;
    }
    const minutes = Math.max(0, Math.round((Date.now() - Date.parse(data.generated_at)) / 60000));
    const ago = minutes < 1 ? "just now" : minutes < 120 ? `${minutes} minutes ago` : `${Math.round(minutes / 60)} hours ago`;
    byId("updated").textContent = `Updated ${ago}${problem ? ` · ${problem}` : ""}`;
  }

  async function load() {
    try {
      const response = await fetch(SOURCE, { cache: "no-cache" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const doc = await response.json();
      if (!valid(doc)) throw new Error("unexpected data");
      const changed = !data || doc.generated_at !== data.generated_at;
      data = doc;
      if (changed) {
        renderStatic();
        render();
      } else {
        renderUpdated();
      }
    } catch (err) {
      // Keep the last good data on screen and try again on the next poll.
      renderUpdated(`could not refresh (${err.message})`);
    }
  }

  function wire(selector, attr, set) {
    const buttons = document.querySelectorAll(selector);
    for (const b of buttons) {
      b.addEventListener("click", () => {
        for (const o of buttons) o.setAttribute("aria-pressed", String(o === b));
        set(b.dataset[attr]);
        render();
      });
    }
  }

  // Theme: "system" follows the operating system; light and dark override it.
  const themeButtons = document.querySelectorAll("[data-theme-choice]");
  function applyTheme(choice) {
    if (choice === "light" || choice === "dark") document.documentElement.setAttribute("data-theme", choice);
    else document.documentElement.removeAttribute("data-theme");
    for (const b of themeButtons) b.setAttribute("aria-pressed", String(b.dataset.themeChoice === choice));
  }
  let saved = "system";
  try { saved = localStorage.getItem(THEME_KEY) || "system"; } catch (e) { /* storage blocked */ }
  applyTheme(saved);
  for (const b of themeButtons) {
    b.addEventListener("click", () => {
      applyTheme(b.dataset.themeChoice);
      try { localStorage.setItem(THEME_KEY, b.dataset.themeChoice); } catch (e) { /* storage blocked */ }
    });
  }

  wire("[data-days]", "days", (v) => { days = Number(v); });
  wire("[data-kind]", "kind", (v) => { kind = v; });
  wire("[data-source]", "source", (v) => { source = v; });

  load();
  setInterval(() => { if (document.visibilityState === "visible") load(); }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") load(); });
})();
