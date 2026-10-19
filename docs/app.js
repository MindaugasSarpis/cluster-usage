// Renders the usage report from the JSON files in data/. No build step.
(function () {
  "use strict";

  const SLOTS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6"];
  const NAMES = { other: "Other users", system: "System" };
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const charts = {};
  let D = {};
  let range = "hourly";

  const $ = (s) => document.querySelector(s);
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const name = (g) => NAMES[g] || "User " + g;
  const fmt = (x, n = 1) => (x === null || x === undefined ? "–" : Number(x).toLocaleString("en-GB", { maximumFractionDigits: n, minimumFractionDigits: n }));

  function store(k, v) {
    try { localStorage.setItem(k, v); } catch (e) { /* storage blocked */ }
  }
  function recall(k) {
    try { return localStorage.getItem(k); } catch (e) { return null; }
  }

  // Colour follows the letter: featured letters keep their slot in every chart.
  function colour(g) {
    if (g === "system") return css("--s-system");
    if (g === "other") return css("--s-other");
    const i = D.meta.featured.indexOf(g);
    return i >= 0 ? css(SLOTS[i]) : css("--s-other");
  }

  function niceLabel(l) {
    if (l.length === 7) { // YYYY-MM
      const [y, m] = l.split("-");
      return new Date(+y, +m - 1, 1).toLocaleDateString("en-GB", { month: "short", year: "numeric" });
    }
    const d = new Date(l.length === 10 ? l + "T12:00" : l);
    if (l.length === 10) return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
    return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric" }) + " " + l.slice(11, 16);
  }

  function baseOptions(yTitle, opts = {}) {
    const grid = css("--grid"), muted = css("--muted"), ink2 = css("--ink-2");
    return {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom", labels: { color: ink2, boxWidth: 10, boxHeight: 10, useBorderRadius: true, borderRadius: 2, padding: 12 } },
        tooltip: {
          backgroundColor: css("--surface"), titleColor: css("--ink"), bodyColor: ink2,
          borderColor: css("--axis"), borderWidth: 1, padding: 10, boxPadding: 4, usePointStyle: true,
          filter: (item) => item.raw !== 0 && item.raw !== null,
          callbacks: { label: (c) => ` ${c.dataset.label}: ${fmt(c.raw, opts.digits ?? 1)}${opts.unit || ""}` },
          itemSort: (a, b) => (b.raw || 0) - (a.raw || 0),
        },
      },
      scales: {
        x: {
          stacked: !!opts.stacked, grid: { display: false }, border: { color: css("--axis") },
          ticks: { color: muted, maxRotation: 0, autoSkip: true, autoSkipPadding: 14 },
        },
        y: {
          stacked: !!opts.stacked, beginAtZero: true, max: opts.max,
          grid: { color: grid }, border: { display: false },
          ticks: { color: muted, maxTicksLimit: 6 },
          title: { display: !!yTitle, text: yTitle, color: muted, font: { size: 12 } },
        },
      },
    };
  }

  function draw(id, config) {
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(document.getElementById(id), config);
  }

  function barSets(series, key, members) {
    const surface = css("--surface");
    return members.map((g) => ({
      label: name(g), data: series[g][key], backgroundColor: colour(g),
      borderColor: surface, borderWidth: { top: 2 }, borderSkipped: "bottom",
      maxBarThickness: 24, categoryPercentage: 0.9, barPercentage: 1,
    }));
  }

  function lineSet(label, data, colourVar, opts = {}) {
    const c = css(colourVar);
    return Object.assign({
      label, data, borderColor: c, backgroundColor: c, borderWidth: 2,
      // A value with gaps on both sides has no line to draw, so it gets a dot.
      pointRadius: (ctx) => {
        const d = ctx.dataset.data, i = ctx.dataIndex;
        return d[i] !== null && (d[i - 1] ?? null) === null && (d[i + 1] ?? null) === null ? 3 : 0;
      },
      pointHoverRadius: 4, tension: 0, spanGaps: false,
      borderJoinStyle: "round", borderCapStyle: "round",
    }, opts);
  }

  // Members that have any non-zero value in the given key, in legend order.
  function present(series, key) {
    return D.meta.members.filter((g) => series[g] && series[g][key].some((v) => v));
  }

  function table(el, headers, rows) {
    const t = document.createElement("table");
    t.innerHTML = "<thead><tr>" + headers.map((h) => `<th scope="col">${h}</th>`).join("") + "</tr></thead>";
    const tb = document.createElement("tbody");
    tb.innerHTML = rows.map((r) => "<tr>" + r.map((c, i) => (i ? `<td>${c}</td>` : `<th scope="row">${c}</th>`)).join("") + "</tr>").join("");
    t.appendChild(tb);
    const host = document.getElementById(el);
    host.replaceChildren(t);
  }

  function renderTiles() {
    const h = D.hourly.host;
    const last = (arr, n) => arr.slice(-n).filter((v) => v !== null);
    const avg = (a) => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : null);
    const busy = avg(last(h.busy_cores, 24));
    const util = avg(last(h.util_pct, 24));
    const load = avg(last(h.load, 24));
    let jobs = null;
    const s = D.hourly.series;
    const sums = D.meta.members.map((g) => s[g].slurm_cpus.slice(-24).filter((v) => v !== null));
    if (sums.some((a) => a.length)) jobs = sums.reduce((t, a) => t + (avg(a) || 0), 0);
    const tiles = [
      ["Cores in use", fmt(busy), "average, last 24 h"],
      ["Utilisation", busy === null ? "–" : fmt(util, 0) + "%", `of ${D.meta.ncpu} cores, last 24 h`],
      ["Load average", fmt(load), "1-minute, averaged over 24 h"],
      ["Slurm CPUs allocated", fmt(jobs), "average, last 24 h"],
    ];
    $("#tiles").innerHTML = tiles.map(([l, v, hint]) =>
      `<div class="tile"><div class="label">${l}</div><div class="value">${v}</div><div class="hint">${hint}</div></div>`).join("");
  }

  function renderRange() {
    const R = D[range];
    const labels = R.labels.map(niceLabel);
    const s = R.series, host = R.host;
    const hourly = range === "hourly";

    // CPU
    const cpuMembers = present(s, "cpu_h");
    document.querySelector("[data-note-cpu]").textContent = hourly
      ? "Core-hours used in each hour, which equals the average number of cores busy in that hour. Stacked by user."
      : "Core-hours used each day, stacked by user. 24 core-hours is one core busy all day; gluon has " + D.meta.ncpu * 24 + " a day.";
    draw("c-cpu", { type: "bar", data: { labels, datasets: barSets(s, "cpu_h", cpuMembers) },
      options: baseOptions(hourly ? "core-hours per hour" : "core-hours per day", { stacked: true, unit: " core-h" }) });
    const fullCpu = R.full;
    table("t-cpu", [hourly ? "Hour" : "Day"].concat(fullCpu.letters.map(name)).concat(["Measured total", "Hours sampled"]),
      R.labels.map((l, i) => [niceLabel(l)].concat(fullCpu.letters.map((g) => fmt(fullCpu.cpu_h[g][i], 2)))
        .concat([fmt(host.busy_cores[i] === null ? null : host.busy_cores[i] * host.coverage_h[i], 2), fmt(host.coverage_h[i], 1)])).reverse());

    // Memory
    const memMembers = present(s, "mem_gb");
    draw("c-mem", { type: "line", data: { labels, datasets: memMembers.map((g) => lineSet(name(g), s[g].mem_gb, "--s1",
      { borderColor: colour(g), backgroundColor: colour(g), fill: false })) },
      options: baseOptions("GiB resident", { unit: " GiB" }) });
    table("t-mem", [hourly ? "Hour" : "Day"].concat(fullCpu.letters.map(name)).concat(["Host memory in use"]),
      R.labels.map((l, i) => [niceLabel(l)].concat(fullCpu.letters.map((g) => fmt(fullCpu.mem_gb[g][i], 2)))
        .concat([fmt(host.mem_used_gb[i], 1)])).reverse());

    // Load vs cores in use (one axis: both are counts of tasks/cores)
    const ncpu = R.labels.map(() => D.meta.ncpu);
    draw("c-load", { type: "line", data: { labels, datasets: [
      lineSet("Load average (1 min)", host.load, "--s2"),
      lineSet("Cores in use", host.busy_cores, "--s1"),
      lineSet(`Cores on gluon (${D.meta.ncpu})`, ncpu, "--axis", { borderWidth: 1, pointHoverRadius: 0 }),
    ] }, options: baseOptions("cores / tasks") });
    table("t-load", [hourly ? "Hour" : "Day", "Load average", "Cores in use", "Of which attributed to users"],
      R.labels.map((l, i) => [niceLabel(l), fmt(host.load[i]), fmt(host.busy_cores[i]), fmt(host.attributed_cores[i])]).reverse());

    // Utilisation
    draw("c-util", { type: "line", data: { labels, datasets: [lineSet("Utilisation", host.util_pct, "--s1",
      { fill: "origin", backgroundColor: css("--s1") + "1a" })] },
      options: Object.assign(baseOptions("% of cores", { unit: "%", digits: 0, max: 100 }), {}) });
    charts["c-util"].options.plugins.legend.display = false;
    charts["c-util"].update();
    table("t-util", [hourly ? "Hour" : "Day", "Utilisation"],
      R.labels.map((l, i) => [niceLabel(l), host.util_pct[i] === null ? "–" : fmt(host.util_pct[i], 1) + "%"]).reverse());

    // Slurm
    const slMembers = present(s, "slurm_cpus");
    draw("c-slurm", { type: "bar", data: { labels, datasets: barSets(s, "slurm_cpus", slMembers.length ? slMembers : ["other"]) },
      options: baseOptions("CPUs allocated", { stacked: true, unit: " CPUs" }) });
    const slAll = D.meta.members;
    table("t-slurm", [hourly ? "Hour" : "Day"].concat(slAll.flatMap((g) => [name(g) + " CPUs", name(g) + " running", name(g) + " pending"])),
      R.labels.map((l, i) => [niceLabel(l)].concat(slAll.flatMap((g) => [fmt(s[g].slurm_cpus[i]), fmt(s[g].slurm_jobs[i]), fmt(s[g].pend_jobs[i])]))).reverse());
  }

  function renderHeat() {
    const H = D.heat.busy_cores;
    const vals = H.flat().filter((v) => v !== null);
    const max = Math.max(D.meta.ncpu / 4, ...vals);
    const steps = ["--h1", "--h2", "--h3", "--h4", "--h5", "--h6", "--h7"];
    const step = (v) => css(steps[Math.min(steps.length - 1, Math.floor((v / max) * steps.length))]);
    let html = '<div class="heat-grid"><span></span>';
    for (let h = 0; h < 24; h++) html += `<span class="cl">${h % 3 === 0 ? h : ""}</span>`;
    H.forEach((row, d) => {
      html += `<span class="rl">${DAYS[d]}</span>`;
      row.forEach((v, h) => {
        const label = `${DAYS[d]} ${String(h).padStart(2, "0")}:00: ${v === null ? "no data yet" : fmt(v) + " cores in use"}`;
        html += `<span class="cell" tabindex="0" role="img" aria-label="${label}" data-tip="${label}"${v === null ? "" : ` style="background:${step(v)}"`}></span>`;
      });
    });
    html += "</div>";
    html += '<div class="heat-legend">0 ' + steps.map((s) => `<span class="sw" style="background:${css(s)}"></span>`).join("") + ` ${fmt(max, 0)} cores · grey: no data yet</div>`;
    html += '<div class="heat-tip" aria-live="polite"></div>';
    $("#heat").innerHTML = html;
    const tip = $("#heat .heat-tip");
    $("#heat").addEventListener("mouseover", (e) => { if (e.target.dataset.tip) tip.textContent = e.target.dataset.tip; });
    $("#heat").addEventListener("focusin", (e) => { if (e.target.dataset.tip) tip.textContent = e.target.dataset.tip; });
    $("#heat-days").textContent = D.heat.days === 1 ? "1 day so far" : D.heat.days + " days so far";
    table("t-heat", ["Hour"].concat(DAYS), Array.from({ length: 24 }, (_, h) =>
      [String(h).padStart(2, "0") + ":00"].concat(DAYS.map((_, d) => fmt(H[d][h])))));
  }

  function renderLogins() {
    const L = D.logins;
    const labels = L.months.map(niceLabel);
    const members = L.members.filter((g) => L.hours[g] && L.hours[g].some((v) => v));
    const sets = members.map((g) => ({ label: name(g), data: L.hours[g], backgroundColor: colour(g),
      borderColor: css("--surface"), borderWidth: { top: 2 }, borderSkipped: "bottom", maxBarThickness: 24, categoryPercentage: 0.9, barPercentage: 1 }));
    draw("c-login", { type: "bar", data: { labels, datasets: sets },
      options: baseOptions("hours logged in", { stacked: true, unit: " h", digits: 0 }) });
    const letters = Object.keys(L.full);
    table("t-login", ["Month"].concat(letters.map(name)),
      L.months.map((m, i) => [niceLabel(m)].concat(letters.map((g) => fmt(L.full[g][i], 0)))).reverse());
    $("#wtmp-first").textContent = new Date(D.meta.wtmp_first * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
  }

  function renderBoot() {
    const B = D.boot;
    const isLetter = (l) => /^[A-Z]+$/.test(l) || l === "system";
    const labels = B.parts.map((p) => (isLetter(p.label) ? name(p.label) : p.label.charAt(0).toUpperCase() + p.label.slice(1)));
    const colours = B.parts.map((p) => (isLetter(p.label) ? colour(p.label) : css("--s-other")));
    const opts = baseOptions("", { unit: " core-h", digits: 0 });
    opts.indexAxis = "y";
    opts.plugins.legend.display = false;
    opts.interaction = { mode: "nearest", axis: "y", intersect: false };
    [opts.scales.x, opts.scales.y] = [
      Object.assign(opts.scales.y, { title: { display: true, text: "core-hours", color: css("--muted"), font: { size: 12 } } }),
      Object.assign(opts.scales.x, { ticks: { color: css("--ink-2"), autoSkip: false } }),
    ];
    draw("c-boot", { type: "bar", data: { labels, datasets: [{ label: "CPU since boot", data: B.parts.map((p) => p.core_h),
      backgroundColor: colours, borderRadius: { topRight: 4, bottomRight: 4 }, borderSkipped: "left", maxBarThickness: 20 }] }, options: opts });
    table("t-boot", ["Part", "Core-hours", "Share of all CPU since boot"],
      B.parts.map((p, i) => [labels[i], fmt(p.core_h, 0), fmt((p.core_h / B.host_core_h) * 100, 1) + "%"])
        .concat([["All CPU on gluon", fmt(B.host_core_h, 0), "100%"]]));
    const long = { day: "numeric", month: "long", year: "numeric" };
    $("#boot-date").textContent = new Date(B.boot * 1000).toLocaleDateString("en-GB", long);
    $("#boot-snap").textContent = new Date(B.snapshot).toLocaleDateString("en-GB", long);
  }

  function renderAll() {
    renderTiles();
    renderRange();
    renderHeat();
    renderLogins();
    renderBoot();
  }

  function setTheme(t) {
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    document.querySelectorAll("[data-set-theme]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.setTheme === t)));
    store("theme", t);
  }

  function wire() {
    document.querySelectorAll("[data-set-theme]").forEach((b) => b.addEventListener("click", () => { setTheme(b.dataset.setTheme); renderAll(); }));
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => renderAll());
    document.querySelectorAll("[data-range]").forEach((b) => b.addEventListener("click", () => {
      range = b.dataset.range;
      document.querySelectorAll("[data-range]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      store("range", range);
      renderRange();
    }));
  }

  async function main() {
    setTheme(recall("theme") || "auto");
    const saved = recall("range");
    if (saved === "daily" || saved === "hourly") {
      range = saved;
      document.querySelectorAll("[data-range]").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.range === range)));
    }
    const get = (f) => fetch("data/" + f, { cache: "no-cache" }).then((r) => r.json());
    const [meta, daily, hourly, heat, logins, boot] = await Promise.all(
      ["meta.json", "daily.json", "hourly.json", "heatmap.json", "logins.json", "sinceboot.json"].map(get));
    D = { meta, daily, hourly, heat, logins, boot };
    $("#updated").textContent = new Date(meta.generated).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) + " (" + meta.timezone + ")";
    $("#updated").setAttribute("datetime", meta.generated);
    $("#since").textContent = meta.first_sample_day ? new Date(meta.first_sample_day + "T12:00").toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" }) : "–";
    document.querySelectorAll("[data-ncpu]").forEach((e) => (e.textContent = meta.ncpu));
    Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
    wire();
    renderAll();
  }

  main().catch((e) => {
    document.querySelector("main").insertAdjacentHTML("afterbegin", `<p class="card">Could not load the data: ${e.message}</p>`);
  });
})();
