/* График + кнопки панели. Не зависит от остального скрипта страницы. */
(function () {
  if (!window.__neftFetchWrapped) {
    window.__neftFetchWrapped = true;
    const _fetch = window.fetch.bind(window);
    window.fetch = function (url, opt) {
    opt = Object.assign({ credentials: "same-origin" }, opt || {});
    const method = (opt.method || "GET").toUpperCase();
    if (String(url).startsWith("/") && method !== "GET" && method !== "HEAD") {
      const m = document.cookie.match(/(?:^|; )neft_csrf=([^;]*)/);
      const h = Object.assign({}, opt.headers || {});
      if (!h["X-NEFT-CSRF"] && !h["x-neft-csrf"])
        h["X-NEFT-CSRF"] = m ? decodeURIComponent(m[1]) : "";
      opt.headers = h;
    }
    return _fetch(url, opt);
    };
  }
  const $ = (id) => document.getElementById(id);
  const set = (id, t) => { const e = $(id); if (e) e.textContent = t; };
  let C = {};
  try { C = JSON.parse(localStorage.getItem("neft_chart") || "{}") || {}; } catch (e) {}
  const st = {
    pair: C.pair || "ETHUSDT",
    tf: C.tf || "1m",
    type: C.type || "candle",
    ema: C.ema !== false,
    vol: C.vol !== false,
    lines: C.lines !== false,
    emaN: C.emaN || 100,
    scale: C.scale || "0",
    grid: C.grid !== false,
    bars: [],
    series: { main: null, ema: null, vol: null },
  };

  window.openSheet = window.openSheet || function () {};
  window.run = window.run || async function (mode) {
    try {
      const r = await (await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      })).json();
      set("runmsg", r.ok ? (mode === "paper" ? "тест запущен" : "демо запущено") : (r.error || "ошибка"));
      set("jobs", r.ok ? "бот работает" : "бот выключен");
    } catch (e) { set("runmsg", "не удалось запустить"); }
  };
  window.stopBot = window.stopBot || async function () {
    try {
      await fetch("/api/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      set("runmsg", "остановлено"); set("jobs", "бот выключен");
    } catch (e) {}
  };

  window.__neftKeepPair = (pair, tf) => {
    if (pair) st.pair = pair;
    if (tf) st.tf = tf;
    save();
  };
  function save() {
    localStorage.setItem("neft_chart", JSON.stringify({
      pair: st.pair, tf: st.tf, type: st.type, ema: st.ema, vol: st.vol, lines: st.lines, emaN: st.emaN, scale: st.scale, grid: st.grid,
    }));
  }
  function clean(raw) {
    const seen = new Set(), out = [];
    for (const x of raw || []) {
      const t = Math.floor(Number(x.time));
      if (!t || seen.has(t)) continue;
      seen.add(t);
      const o = +x.open, h = +x.high, l = +x.low, c = +x.close;
      if (![o, h, l, c].every(Number.isFinite)) continue;
      out.push({ time: t, open: o, high: h, low: l, close: c, volume: +x.volume || 0 });
    }
    return out.sort((a, b) => a.time - b.time);
  }
  function emaCalc(bars, p) {
    const k = 2 / (p + 1);
    let e = null, out = [];
    for (const c of bars) {
      e = e == null ? c.close : c.close * k + e * (1 - k);
      out.push({ time: c.time, value: e });
    }
    return out;
  }
  function asMain(bars) {
    if (st.type === "line" || st.type === "area") return bars.map((c) => ({ time: c.time, value: c.close }));
    return bars;
  }
  function sync() {
    document.querySelectorAll("[data-tf]").forEach((b) => b.classList.toggle("on", b.dataset.tf === st.tf));
    document.querySelectorAll("[data-type]").forEach((b) => b.classList.toggle("on", b.dataset.type === st.type));
    if ($("btnEma")) $("btnEma").classList.toggle("on", st.ema);
    if ($("btnVol")) $("btnVol").classList.toggle("on", st.vol);
    if ($("btnLines")) $("btnLines").classList.toggle("on", st.lines);
    if ($("optScale")) $("optScale").value = st.scale;
    if ($("optEma")) $("optEma").value = st.emaN;
    if ($("optGrid")) $("optGrid").checked = st.grid;
  }
  function applyOpts() {
    const chart = window.__neftChart;
    if (!chart) return;
    chart.applyOptions({
      grid: { vertLines: { visible: st.grid, color: "#241f1a" }, horzLines: { visible: st.grid, color: "#241f1a" } },
    });
    try { chart.priceScale("right").applyOptions({ mode: +st.scale }); } catch (e) {}
    if (st.series.ema) st.series.ema.applyOptions({ visible: st.ema });
    if (st.series.vol) st.series.vol.applyOptions({ visible: st.vol });
  }
  function dropSeries(chart) {
    ["main", "ema", "vol"].forEach((k) => {
      try { if (st.series[k]) chart.removeSeries(st.series[k]); } catch (e) {}
      st.series[k] = null;
    });
  }
  function makeSeries(chart) {
    dropSeries(chart);
    const up = "#6fbf8a", dn = "#d36b6b";
    if (st.type === "bar") st.series.main = chart.addBarSeries({ upColor: up, downColor: dn });
    else if (st.type === "line") st.series.main = chart.addLineSeries({ color: up, lineWidth: 2 });
    else if (st.type === "area") st.series.main = chart.addAreaSeries({ lineColor: up, topColor: "#6fbf8a55", bottomColor: "#6fbf8a08" });
    else st.series.main = chart.addCandlestickSeries({
      upColor: up, downColor: dn, borderVisible: false, wickUpColor: up, wickDownColor: dn,
    });
    st.series.ema = chart.addLineSeries({ color: "#8a8478", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, visible: st.ema });
    try {
      st.series.vol = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", color: "#3a3530", visible: st.vol });
      chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    } catch (e) { st.series.vol = null; }
    window.__neftCandle = st.series.main;
  }
  function paint(bars) {
    st.bars = bars || [];
    const s = st.series;
    if (!s.main) return;
    try { s.main.setData(asMain(st.bars)); } catch (e) { return; }
    try { if (s.ema) s.ema.setData(st.ema ? emaCalc(st.bars, st.emaN) : []); } catch (e) {}
    try {
      if (s.vol) s.vol.setData(st.bars.map((x) => ({
        time: x.time, value: x.volume, color: x.close >= x.open ? "#2a4" : "#622",
      })));
    } catch (e) {}
    if (st.bars.length) {
      const last = st.bars[st.bars.length - 1].close;
      set("lastPx", String(last));
      const lastBar = st.bars[st.bars.length - 1];
      const from = lastBar.time - 86400;
      let ref = st.bars[0];
      for (const x of st.bars) { if (x.time >= from) { ref = x; break; } }
      const first = ref.open;
      if (first) {
        const pct = ((last - first) / first) * 100;
        set("chg", (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%");
        const el = $("chg");
        if (el) el.style.color = pct >= 0 ? "#6fbf8a" : "#d36b6b";
      }
      set("hudBars", String(st.bars.length));
    }
  }
  function ensureChart() {
    const box = $("chart");
    if (!box) return null;
    if (window.__neftChart) {
      if (!st.series.main) makeSeries(window.__neftChart);
      return window.__neftChart;
    }
    if (!window.LightweightCharts) { set("feed", "нет библиотеки графика"); return null; }
    const MSK = "Europe/Moscow";
    const mskSec = (time) => {
      if (typeof time === "number") return time < 1e12 ? time : Math.floor(time / 1000);
      if (time && time.year) return Date.UTC(time.year, time.month - 1, time.day) / 1000;
      let s = String(time).trim();
      if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(s) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(s))
        s = s.replace(" ", "T") + "Z";
      const n = Date.parse(s);
      return Number.isFinite(n) ? Math.floor(n / 1000) : 0;
    };
    const mskTick = (time, type) => {
      const d = new Date(mskSec(time) * 1000);
      const tz = { timeZone: MSK };
      if (type === 0) return d.toLocaleDateString("ru-RU", { ...tz, year: "numeric" });
      if (type === 1) return d.toLocaleDateString("ru-RU", { ...tz, month: "short" });
      if (type === 2) return d.toLocaleDateString("ru-RU", { ...tz, day: "2-digit", month: "short" });
      if (type === 4) return d.toLocaleTimeString("ru-RU", { ...tz, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
      return d.toLocaleTimeString("ru-RU", { ...tz, hour: "2-digit", minute: "2-digit", hour12: false });
    };
    const mskLabel = (time) => {
      const d = new Date(mskSec(time) * 1000);
      const day = d.toLocaleDateString("ru-RU", { timeZone: MSK, day: "2-digit", month: "short" });
      const tm = d.toLocaleTimeString("ru-RU", { timeZone: MSK, hour: "2-digit", minute: "2-digit", hour12: false });
      return day + " " + tm;
    };
    const chart = LightweightCharts.createChart(box, {
      width: Math.max(box.clientWidth || 0, 360),
      height: Math.max(box.clientHeight || 0, 260),
      layout: { background: { color: "#0c0b0a" }, textColor: "#c8c2b6" },
      grid: { vertLines: { color: "#241f1a" }, horzLines: { color: "#241f1a" } },
      localization: { locale: "ru-RU", dateFormat: "dd MMM", timeFormatter: mskLabel },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#26231e", tickMarkFormatter: mskTick },
      rightPriceScale: { borderColor: "#26231e" },
      crosshair: { mode: 0 },
    });
    window.__neftChart = chart;
    makeSeries(chart);
    applyOpts();
    if (window.ResizeObserver) {
      new ResizeObserver(() => {
        const cw = box.clientWidth, ch = box.clientHeight;
        if (cw > 20 && ch > 20) chart.resize(cw, ch);
      }).observe(box);
    }
    return chart;
  }
  const TEST = [
    ["ETH", "binance"], ["BNB", "binance"], ["XRP", "binance"],
    ["BCH", "binance"], ["SOL", "binance"], ["DOGE", "binance"], ["ZEC", "binance"],
    ["SUI", "binance"], ["HYPE", "bybit"],
  ];
  function venueOf(pair) {
    return String(pair).startsWith("HYPE") ? "bybit" : "binance";
  }
  function fillList() {
    const list = $("list");
    if (!list || list.querySelector("[data-k]")) return;
    list.innerHTML = TEST.map(([c, v]) =>
      `<button class="sym" type="button" data-p="${c}USDT" onclick="chartShow('${c}USDT')"><span>${c}</span><small>${v}</small></button>`
    ).join("");
  }

  async function show(pair, tf) {
    if (pair) st.pair = pair;
    if (tf) st.tf = tf;
    save();
    sync();
    const chart = ensureChart();
    if (!st.series.main) return;
    set("symName", st.pair.replace("USDT", "") + " " + st.tf);
    set("feed", "загрузка…");
    const r = await (await fetch("/api/klines?pair=" + st.pair + "&tf=" + st.tf + "&venue=" + venueOf(st.pair))).json();
    const data = clean(r.candles);
    if (!data.length) { set("feed", r.error || "нет свечей"); return; }
    paint(data);
    if (typeof homeView === "function") homeView();
    else if (window.__neftChart) {
      const ts = window.__neftChart.timeScale();
      const w = ($("chart") && $("chart").clientWidth) || 800;
      ts.applyOptions({ rightOffset: 8, barSpacing: 10 });
      if (ts.scrollToRealTime) ts.scrollToRealTime();
    }
    set("feed", "рынок · " + st.tf);
    fillList();
    window.__neftPair = st.pair;
    window.__neftTf = st.tf;
  }

  window.chartShow = (pair) => show(pair, st.tf).catch((e) => set("feed", String(e.message || e)));
  window.chartTf = (tf) => show(st.pair, tf).catch((e) => set("feed", String(e.message || e)));
  window.chartType = (type) => {
    st.type = type; save(); sync();
    if (!window.__neftChart) return;
    makeSeries(window.__neftChart); applyOpts(); paint(st.bars);
  };
  window.chartEma = () => {
    st.ema = !st.ema; save(); sync();
    if (st.series.ema) st.series.ema.setData(st.ema ? emaCalc(st.bars, st.emaN) : []);
  };
  window.chartVol = () => {
    st.vol = !st.vol; save(); sync();
    if (st.series.vol) st.series.vol.applyOptions({ visible: st.vol });
  };
  window.chartLines = () => {
    st.lines = !st.lines; save(); sync();
    if (typeof window.chartApplyLines === "function") window.chartApplyLines(st.lines);
  };
  window.chartFit = () => { if (window.__neftChart) window.__neftChart.timeScale().fitContent(); };
  window.chartReset = () => {
    const keep = window.__neftPair || st.pair;
    const keepTf = window.__neftTf || st.tf;
    st.pair = keep; st.tf = keepTf;
    st.type = "candle"; st.ema = true; st.vol = true; st.lines = true;
    st.emaN = 100; st.scale = "0"; st.grid = true;
    save(); sync();
    const chart = window.__neftChart;
    const ser = window.__neftCandle;
    if (ser) {
      try { if (ser.setMarkers) ser.setMarkers([]); } catch (e) {}
      try {
        if (typeof priceLines !== "undefined" && priceLines.length) {
          priceLines.forEach((l) => { try { ser.removePriceLine(l); } catch (e) {} });
          priceLines = [];
        }
      } catch (e) {}
    }
    try { if (typeof lastOverlay !== "undefined") lastOverlay = ""; } catch (e) {}
    if (chart) {
      makeSeries(chart);
      applyOpts();
      try {
        chart.priceScale("right").applyOptions({
          autoScale: true, mode: 0, invertScale: false,
          scaleMargins: { top: 0.08, bottom: 0.18 },
        });
        chart.timeScale().applyOptions({
          rightOffset: 8,
          barSpacing: 10, minBarSpacing: 3,
          fixLeftEdge: false, fixRightEdge: false,
        });
      } catch (e) {}
      const box = $("chart");
      if (box && box.clientWidth > 20 && box.clientHeight > 20) chart.resize(box.clientWidth, box.clientHeight);
    }
    if (typeof pref !== "undefined") {
      pref.type = "candle"; pref.ema = true; pref.vol = true; pref.lines = true;
      pref.emaN = 100; pref.scale = "0"; pref.grid = true;
      try { if (typeof savePref === "function") savePref(); if (typeof syncToolbar === "function") syncToolbar(); } catch (e) {}
    }
    const p = $("chartPop"); if (p) p.classList.remove("show");
    if (typeof homeView === "function") homeView();
    else if (window.__neftChart) {
      const ts = window.__neftChart.timeScale();
      const w = ($("chart") && $("chart").clientWidth) || 800;
      ts.applyOptions({ rightOffset: 8, barSpacing: 10 });
      if (ts.scrollToRealTime) ts.scrollToRealTime();
    }
    set("feed", "исходный вид");
  };
  window.chartGear = (ev) => {
    if (ev) ev.stopPropagation();
    const p = $("chartPop");
    if (p) p.classList.toggle("show");
  };
  window.chartOpts = () => {
    if ($("optScale")) st.scale = $("optScale").value;
    if ($("optEma")) st.emaN = +$("optEma").value || 100;
    if ($("optGrid")) st.grid = $("optGrid").checked;
    save(); applyOpts();
    if (st.ema && st.series.ema) st.series.ema.setData(emaCalc(st.bars, st.emaN));
  };

  document.addEventListener("click", (e) => {
    if (!e.target.closest || !e.target.closest(".ctb")) {
      const p = $("chartPop"); if (p) p.classList.remove("show");
    }
  });

  window.addEventListener("error", (ev) => { set("feed", String(ev.message || "ошибка").slice(0, 90)); });

  function start() {
    sync();
    if (window.__neftPair) return;
    show(st.pair || "ETHUSDT", st.tf).catch((e) => set("feed", String(e && e.message || e)));
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
