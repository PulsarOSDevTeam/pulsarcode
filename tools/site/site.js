/* SPDX-License-Identifier: AGPL-3.0-or-later
   Copyright (C) 2026 PulsarOS Intelligence Inc.
   Pulsar Code site. Source of the inline script in docs/index.html (build.py pastes it, comments and
   indentation stripped). No network calls besides fetch("./demo/<scene>.json") on replay play. */
(() => {
"use strict";
const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
const RM = matchMedia("(prefers-reduced-motion: reduce)");
let reduced = RM.matches;
const D = JSON.parse($("#pdata").textContent);
const CLK = D.clk, W1 = CLK.w1, A0 = CLK.a0, P1 = CLK.P1 * 1000, HP = CLK.H * 1000;
const ICE = "#ECF6FF", ASH = "#9692AC", BAD = "#FF465A";
const E_SETTLE = "cubic-bezier(.16,1,.3,1)", E_IMPACT = "cubic-bezier(.05,.9,.2,1)", E_COOL = "cubic-bezier(.2,.8,.2,1)";
const clamp = (v, a = 0, b = 1) => v < a ? a : v > b ? b : v;
const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } } };

/* one rAF loop; a subscriber returns true while it still has work */
const subs = new Set(); let raf = 0;
function loop(now) { raf = 0; for (const f of [...subs]) if (!f(now)) subs.delete(f); if (subs.size) raf = requestAnimationFrame(loop); }
function want(f) { subs.add(f); if (!raf) raf = requestAnimationFrame(loop); }
function seen(el, cb, opt) { const io = new IntersectionObserver(es => es.forEach(e => cb(e.isIntersecting, e)), opt); io.observe(el); return io; }

/* toast and senses */
const toast = $("#toast"); let toastT = 0;
function say(m) { toast.textContent = m; toast.classList.add("on"); clearTimeout(toastT); toastT = setTimeout(() => toast.classList.remove("on"), 1800); }
const hapBtn = $("#haptics"), lisBtn = $("#listen");
const canVibe = typeof navigator.vibrate === "function";
let hap = canVibe && store.get("pc.haptics") === "1";
function buzz(p) { if (hap && canVibe && !reduced) try { navigator.vibrate(p); } catch (e) { /* ignore */ } }
if (canVibe) { hapBtn.hidden = false; hapBtn.setAttribute("aria-pressed", hap); }
hapBtn.addEventListener("click", () => { hap = !hap; hapBtn.setAttribute("aria-pressed", hap); store.set("pc.haptics", hap ? "1" : "0"); if (hap) buzz([10, 30, 10]); });

/* tabs per the ARIA pattern */
function tabs(list, onSelect) {
  const ts = $$('[role="tab"]', list);
  const pick = (tab, focus) => { ts.forEach(t => { const on = t === tab; t.setAttribute("aria-selected", on); t.tabIndex = on ? 0 : -1; });
    if (focus) tab.focus(); onSelect(tab); };
  ts.forEach((t, i) => {
    t.addEventListener("click", () => pick(t, false));
    t.addEventListener("keydown", e => {
      const k = e.key; let j = null;
      if (k === "ArrowRight") j = (i + 1) % ts.length; else if (k === "ArrowLeft") j = (i - 1 + ts.length) % ts.length;
      else if (k === "Home") j = 0; else if (k === "End") j = ts.length - 1;
      if (j !== null) { e.preventDefault(); e.stopPropagation(); pick(ts[j], true); }
    });
  });
  return pick;
}

/* install */
const GH = "https://raw.githubusercontent.com/PulsarOSDevTeam/pulsarcode/";
const CMDS = { nix: ["$ ", "curl -fsSL " + GH + "main/install.sh | bash", "curl -fsSL " + GH + "v3.0.0/install.sh | PULSAR_REF=v3.0.0 bash"],
  win: ["PS> ", "irm " + GH + "main/install.ps1 | iex", "$env:PULSAR_REF = 'v3.0.0'; irm " + GH + "v3.0.0/install.ps1 | iex"] };
let os = "nix";
const cmdtext = $("#cmdtext"), copyB = $("#copy");
const pickOS = tabs($(".install .tabs"), t => {
  os = t.dataset.os; $("#cmd").setAttribute("aria-labelledby", t.id);
  const p = document.createElement("span"); p.className = "p"; p.textContent = CMDS[os][0];
  cmdtext.textContent = ""; cmdtext.append(p, CMDS[os][1]); $("#pintext").textContent = CMDS[os][2];
});
const plat = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "";
if (/^win/i.test(plat)) pickOS($("#t-win"), false);
copyB.addEventListener("click", async () => {
  let ok = false;
  try { await navigator.clipboard.writeText(CMDS[os][1]); ok = true; } catch (e) {
    const r = document.createRange(); r.selectNodeContents(cmdtext); const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    try { ok = document.execCommand("copy"); } catch (e2) { ok = false; }
  }
  if (ok) { buzz([10, 30, 10]); copyB.classList.add("ok"); copyB.textContent = "Copied"; setTimeout(() => { copyB.classList.remove("ok"); copyB.textContent = "Copy"; }, 1600); }
  say(ok ? "Copied. Paste it into your terminal." : "Selected. Press Ctrl+C or Cmd+C to copy.");
});

/* reveal: heading glint and rising cards, once */
const rio = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.classList.add("in"); rio.unobserve(e.target); } }), { rootMargin: "0px 0px -12% 0px" });
$$(".glint,.rise").forEach(el => rio.observe(el));

/* hero: an <img> of the brand SVG (its CSS animations run off the main thread), docking mark, listen.
   The core sits at (250, 190) of the 1280 x 440 artwork; narrow screens crop it with CSS only. */
const hero = $(".hero-art"), stage = $(".stage"), header = $(".top"), dock = $(".dock");
let heroVis = true, heroT0 = 0;
function heroStart() {
  const e = hero && performance.getEntriesByName(hero.currentSrc || hero.src)[0];
  heroT0 = e ? e.responseEnd : performance.now();
}
if (hero) { if (hero.complete) heroStart(); else hero.addEventListener("load", heroStart); }
function dockVars() {
  if (!hero) return;
  const r = hero.getBoundingClientRect(), s = r.width / 1280;
  const cx = r.left + 250 * s, cy = r.top + scrollY + 190 * s;
  const sl = $(".slot").getBoundingClientRect(), st = dock.style;
  st.setProperty("--x0", cx - 14 + "px"); st.setProperty("--y0", cy - 14 + "px"); st.setProperty("--s0", Math.max(1, 5.5 * s / 3.1).toFixed(3));
  st.setProperty("--x1", sl.left + "px"); st.setProperty("--y1", sl.top + "px"); const dr = Math.max(1, cy - 14 - sl.top); st.setProperty("--y7", cy - 14 - .7 * dr + "px"); st.animationRange = "0px " + dr.toFixed(1) + "px";
}
const canDock = CSS.supports && CSS.supports("animation-timeline: scroll()");
function setupDock() {
  if (reduced) { document.body.classList.add("nodock"); dock.classList.remove("on"); return; }
  document.body.classList.remove("nodock");
  if (canDock) { dockVars(); dock.classList.add("on"); }
}
addEventListener("resize", () => { if (canDock && !reduced) dockVars(); });
seen(stage, v => { heroVis = v; stage.classList.toggle("off", !v); header.classList.toggle("past", !v); if (!canDock || reduced) header.classList.toggle("docked", !v); pulseClock(); }, { rootMargin: "-56px 0px 0px 0px" });

let actx = null, listen = store.get("pc.listen") === "1", lt = 0;
function tick() {
  if (!actx) return;
  const t = actx.currentTime, out = actx.createGain(); out.gain.value = .126; out.connect(actx.destination); /* -18 dBFS */
  const n = actx.createBuffer(1, Math.ceil(actx.sampleRate * .002), actx.sampleRate), d = n.getChannelData(0);
  for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
  const src = actx.createBufferSource(), bp = actx.createBiquadFilter(); bp.type = "bandpass"; bp.frequency.value = 2400; bp.Q.value = 1.2;
  src.buffer = n; src.connect(bp); bp.connect(out); src.start(t);
  const o = actx.createOscillator(), g = actx.createGain(); o.frequency.value = 110; g.gain.setValueAtTime(1, t); g.gain.exponentialRampToValueAtTime(.001, t + .06);
  o.connect(g); g.connect(out); o.start(t); o.stop(t + .07);
}
function pulseClock() {
  clearTimeout(lt);
  if (!listen || !heroVis || document.hidden || !actx) return;
  if (!heroT0) { lt = setTimeout(pulseClock, 500); return; }
  /* the pen's main-pulse ring in the hero SVG: delay and period read from the artwork at build time */
  const dl = CLK.pr[0] * 1000, du = CLK.pr[1] * 1000, ct = performance.now() - heroT0;
  const wait = ct < dl ? dl - ct : du - ((ct - dl) % du);
  lt = setTimeout(() => { tick(); pulseClock(); }, Math.max(16, wait));
}
function setListen(on) {
  listen = on; lisBtn.setAttribute("aria-pressed", on); store.set("pc.listen", on ? "1" : "0");
  if (on && !actx) { const AC = window.AudioContext || window.webkitAudioContext; if (AC) actx = new AC(); }
  if (actx) { if (on) actx.resume(); else actx.suspend(); }
  pulseClock();
}
lisBtn.setAttribute("aria-pressed", "false"); /* audio needs a gesture: remembered, applied on the next click */
lisBtn.addEventListener("click", () => setListen(lisBtn.getAttribute("aria-pressed") !== "true"));
document.addEventListener("visibilitychange", pulseClock);

/* ---------------- uplink: detector and constellation ---------------- */
const keyIn = $("#key"), ro = $("#readout"), cfig = $("#const"), cam = $(".cam", cfig), csv = $("svg", cfig);
const beam = csv && csv.querySelector('[class$="beam"]');
const NODES = {};
const ROUTE = { adapter: "#36E2FF", native: "#9D4DFF", cloud: "#FF6A00", local: "#FF2E88" };
let core = [640, 250];
if (csv) {
  const tr = beam && beam.parentNode.getAttribute("transform"), m = tr && tr.match(/translate\(([-\d.]+)[ ,]+([-\d.]+)/);
  if (m) core = [+m[1], +m[2]];
  $$("[data-id]", csv).forEach(g => { const c = $("circle", g), tx = $("text", g);
    NODES[g.dataset.id] = { g, c, tx, a: +g.dataset.angle, x: +c.getAttribute("cx"), y: +c.getAttribute("cy"), col: c.getAttribute("fill") }; });
}
const PFX = D.p.map(r => ({ pre: r[0], id: r[1], name: r[2], route: r[3], wire: r[4] })).concat(D.u.map(pre => ({ pre, un: 1 })))
  .sort((a, b) => b.pre.length - a.pre.length);
const ALLP = PFX.map(r => r.pre).concat(D.r.map(r => r[0]), ["sk-", "localhost"], Object.keys(D.lw));
function detect(t) {
  t = t.trim(); if (!t) return { k: "empty" };
  const lo = t.toLowerCase();
  if (D.lw[lo]) return { k: "local", ids: [D.lw[lo]], pre: t };
  if (/^(localhost|127\.0\.0\.1|https?:\/\/(localhost|127\.))/.test(lo)) return { k: "local", ids: D.g.local, pre: t.slice(0, 9) === "localhost" ? "localhost" : t, all: 1 };
  for (const [pre, note] of D.r) if (t.startsWith(pre)) return { k: "rej", pre, note };
  for (const r of PFX) if (t.startsWith(r.pre)) return r.un ? { k: "un", pre: r.pre } : Object.assign({ k: "key" }, r);
  if (t.startsWith("sk-")) return { k: "amb", pre: "sk-", ids: D.amb };
  if (ALLP.some(p => p.startsWith(t))) return { k: "part" };
  return { k: "none" };
}
/* the field shows the characters that can still be part of a public prefix; the rest become bullets */
let raw = "";
function shown(v) {
  let n = 0; while (n < v.length && ALLP.some(p => p.startsWith(v.slice(0, n + 1)))) n++;
  const r = detect(v), keep = r.pre && v.startsWith(r.pre) ? Math.max(n, r.pre.length) : n;
  return v.slice(0, keep) + "•".repeat(v.length - keep);
}
function setRaw(v, src) {
  raw = v.slice(0, 12);
  const s = keyIn.selectionStart; keyIn.value = shown(raw);
  if (src !== "chip") try { keyIn.setSelectionRange(s, s); } catch (e) { /* ignore */ }
  run(detect(raw));
}
keyIn.addEventListener("beforeinput", e => {
  const t = e.inputType; if (e.isComposing) return;
  const a = keyIn.selectionStart, b = keyIn.selectionEnd; let v = raw, pos = a;
  if (t.startsWith("insert")) { const d = (e.data != null ? e.data : e.dataTransfer ? e.dataTransfer.getData("text") : "").replace(/\s+/g, ""); v = raw.slice(0, a) + d + raw.slice(b); pos = a + d.length; }
  else if (t === "deleteContentBackward") { if (a === b && a > 0) { v = raw.slice(0, a - 1) + raw.slice(b); pos = a - 1; } else v = raw.slice(0, a) + raw.slice(b); }
  else if (t === "deleteContentForward") { v = a === b ? raw.slice(0, a) + raw.slice(a + 1) : raw.slice(0, a) + raw.slice(b); }
  else if (t.startsWith("delete")) { v = raw.slice(0, a) + raw.slice(b); }
  else return;
  e.preventDefault(); setRaw(v); pos = Math.min(pos, raw.length); try { keyIn.setSelectionRange(pos, pos); } catch (er) { /* ignore */ }
});
keyIn.addEventListener("input", () => { if (keyIn.value.indexOf("•") < 0 && keyIn.value !== raw) setRaw(keyIn.value.replace(/\s+/g, "")); });
function wipe() { raw = ""; keyIn.value = ""; }
keyIn.addEventListener("blur", wipe);
keyIn.addEventListener("focus", () => keyIn.select());
document.addEventListener("visibilitychange", () => { if (document.hidden) wipe(); });
$$(".presets .chipb").forEach(b => b.addEventListener("click", () => { setRaw(b.dataset.k, "chip"); }));

const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const AR = '<span class="ar"> → </span>', CORE = "Pulsar Core " + D.core;
function readout(r) {
  const pf = (p, c) => '<span class="pf" style="--rc:' + c + '">' + esc(p) + "</span>";
  if (r.k === "key") {
    const c = ROUTE[r.route], tail = r.route === "cloud" ? " · cloud" + AR + "Claude Code talks to the cloud directly" : " · " + r.route + " · " + r.wire + " wire" + AR + CORE;
    return pf(r.pre, c) + AR + "<b>" + esc(r.name) + "</b>" + tail;
  }
  if (r.k === "local") return pf(r.pre, ROUTE.local) + AR + "<b>" + (r.all ? "local engines" : esc(D.names[r.ids[0]])) + "</b> · local · no key" + AR + CORE +
    '<span class="why">' + (r.all ? "Servers on your machine are found on loopback: Ollama, LM Studio, llama.cpp, vLLM, Jan, LocalAI." : "Type it at the key prompt; the server is found on loopback.") + "</span>";
  if (r.k === "rej") return '<span class="bad">' + esc(r.pre) + " · refused</span>" + '<span class="why">' + esc(r.note) + " Pulsar Code will not use it.</span>";
  if (r.k === "amb") return pf("sk-", ASH) + AR + "<b>several vendors use this shape</b>" + '<span class="why">Pulsar Code asks you to choose: ' + r.ids.map(i => esc(D.names[i])).join(", ") + ", instead of trying the key against several vendors.</span>";
  if (r.k === "un") return pf(r.pre, ASH) + AR + "<b>recognised</b>" + '<span class="why">Not one of the listed provider profiles.</span>';
  if (r.k === "part") return "Listening… prefixes are matched longest first.";
  if (r.k === "none") return "No known prefix." + '<span class="why">The CLI also accepts a URL, an <b>export VAR=…</b> line or <b>ollama</b>.</span>';
  return "Waiting for a prefix. Try <b>sk-or-</b> or the fake key.";
}
let lastSig = "";
function run(r) {
  const sig = r.k + (r.id || r.pre || "");
  if (sig === lastSig) return; lastSig = sig;
  ro.innerHTML = readout(r);
  if (!csv) return;
  if (r.k === "key" && NODES[r.id]) { aimAt([r.id], true); buzz([6, 30, 10]); }
  else if ((r.k === "local" || r.k === "amb") && r.ids) { aimAt(r.ids.filter(i => NODES[i]), r.ids.length === 1); buzz([6, 30, 10]); }
  else if (r.k === "rej") { shake(BAD); clearSel(); }
  else if (r.k === "empty" || r.k === "none") clearSel();
}

/* constellation motion: CSS-speed orbit (WAAPI), spring to a node, ease back after 4 s idle */
const W1MS = W1 / 1000;                    /* deg per ms */
const WIN = 4 / W1 * 1000;                 /* ms of beam travel across +-4 deg */
let orbitAnim = null, orbitT0 = 0, orbitA = A0, mode = "rest", bA = A0, bV = 0, tgt = A0, idleT = 0, flares = [], sel = [], cVis = false;
const now = () => document.timeline.currentTime || performance.now();
const setBeam = a => { bA = a; if (beam) beam.style.rotate = a.toFixed(3) + "deg"; };
function orbitAngle() { return orbitA + (now() - orbitT0) * W1MS; }
function stopOrbit() { if (orbitAnim) { bA = orbitAngle(); orbitAnim.cancel(); orbitAnim = null; } flares.forEach(a => a.cancel()); flares = []; }
function orbit(from) {
  stopOrbit(); setBeam(from);
  if (reduced || !beam || !cVis) { mode = "rest"; return; }
  mode = "orbit"; orbitA = from; orbitT0 = now();
  orbitAnim = beam.animate([{ rotate: from + "deg" }, { rotate: from + 360 + "deg" }], { duration: P1, iterations: Infinity });
  orbitAnim.startTime = orbitT0;
  const hold = 2 * WIN / HP;
  for (const id in NODES) {
    if (sel.includes(id)) continue;
    const n = NODES[id], first = ((((n.a - from) % 180) + 180) % 180) / W1MS, st = orbitT0 + first - WIN - HP;
    const o = { duration: HP, iterations: Infinity };
    const a1 = n.c.animate([{ offset: 0, scale: "1.875" }, { offset: hold, scale: "1.875", easing: E_IMPACT }, { offset: hold + .06, scale: "1.15" }, { offset: .18, scale: "1" }, { offset: 1, scale: "1" }], o);
    const a2 = n.tx.animate([{ offset: 0, fill: ICE }, { offset: hold, fill: ICE, easing: E_IMPACT }, { offset: .18, fill: ASH }, { offset: 1, fill: ASH }], o);
    a1.startTime = st; a2.startTime = st; flares.push(a1, a2);
  }
}
function clearSel() {
  sel.forEach(id => { const n = NODES[id]; n.c.getAnimations().forEach(a => a.cancel()); n.tx.getAnimations().forEach(a => a.cancel()); });
  sel = []; $$(".lk,.hl", csv).forEach(l => l.remove());
}
function link(n, dashed) {
  const NS = "http://www.w3.org/2000/svg", l = document.createElementNS(NS, "line"), h = document.createElementNS(NS, "circle");
  l.setAttribute("class", "lk"); l.setAttribute("x1", core[0]); l.setAttribute("y1", core[1]); l.setAttribute("x2", n.x); l.setAttribute("y2", n.y); l.setAttribute("stroke", n.col);
  const len = Math.hypot(n.x - core[0], n.y - core[1]);
  l.setAttribute("stroke-dasharray", dashed ? "3 5" : len + " " + len);
  h.setAttribute("class", "hl"); h.setAttribute("cx", n.x); h.setAttribute("cy", n.y); h.setAttribute("r", 11); h.setAttribute("stroke", n.col);
  n.g.parentNode.insertBefore(l, n.g.parentNode.firstChild.nextSibling); n.g.after(h);
  if (!reduced) {
    if (!dashed) l.animate([{ strokeDashoffset: len }, { strokeDashoffset: 0 }], { duration: 400, easing: E_SETTLE, fill: "both" });
    else l.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 400, easing: E_SETTLE, fill: "both" });
    h.animate([{ opacity: .9, scale: ".4" }, { opacity: .55, scale: "1" }], { duration: 620, easing: E_COOL, fill: "both" });
  } else h.style.opacity = .55;
  h.style.transformBox = "fill-box"; h.style.transformOrigin = "50% 50%";
}
function flare(n) {
  n.c.getAnimations().forEach(a => a.cancel()); n.tx.getAnimations().forEach(a => a.cancel());
  const f = { duration: reduced ? 0 : 620, easing: E_COOL, fill: "forwards" };
  n.c.animate([{ scale: "2.4", fill: "#FFFFFF" }, { offset: .06, scale: "2.4", fill: "#FFFFFF" }, { offset: .35, fill: "#FFE9F6" }, { scale: "1.9", fill: n.col }], f);
  n.tx.animate([{ fill: "#FFFFFF" }, { offset: .35, fill: "#FFE9F6" }, { fill: ICE }], f);
}
function shake(col) {
  if (reduced) return;
  const k = [[0, 0], [-3, 1.5], [2.5, -2], [-2, 1], [1.5, -.5], [-.75, .5], [.3, -.2], [0, 0]], o = [0, .07, .16, .27, .40, .55, .72, 1];
  cam.animate(k.map((p, i) => ({ offset: o[i], transform: "translate(" + p[0] + "px," + p[1] + "px)" })), { duration: 420 });
  if (col) cfig.animate([{ boxShadow: "inset 0 0 0 1px " + col }, { boxShadow: "inset 0 0 0 1px transparent" }], { duration: 600, easing: E_SETTLE });
}
function aimAt(ids, swing) {
  clearSel(); clearTimeout(idleT);
  const wasOrbit = mode === "orbit"; stopOrbit();
  sel = ids.slice(); if (ids.length === 1) centrePan(NODES[ids[0]].x);
  ids.forEach(id => { flare(NODES[id]); link(NODES[id], !swing); });
  if (swing && ids.length) {
    const a = NODES[ids[0]].a, d = ((((a - bA) % 180) + 270) % 180) - 90;
    tgt = bA + d;
    if (reduced) { setBeam(tgt); mode = "rest"; }
    else { if (wasOrbit) bV = W1MS; mode = "spring"; shake(); want(spring); }
  } else if (!reduced && mode !== "spring") { mode = "hold"; }
  idleT = setTimeout(resume, 4000);
}
let lastS = 0;
function spring(t) {
  if (mode !== "spring") return false;
  const dt = Math.min(32, lastS ? t - lastS : 16); lastS = t;
  const w = .018, z = .62; /* omega 18 rad/s, zeta .62, per ms */
  for (let i = 0; i < 4; i++) { const h = dt / 4, acc = -w * w * (bA - tgt) - 2 * z * w * bV; bV += acc * h; bA += bV * h; }
  setBeam(bA);
  if (Math.abs(bA - tgt) < .02 && Math.abs(bV) < .0005) { setBeam(tgt); bV = 0; lastS = 0; mode = "hold"; return false; }
  return true;
}
let rs = 0, rl = 0;
function resume() {
  clearSel(); lastSig = "";
  if (reduced || !cVis) { orbit(bA); return; }
  mode = "resume"; rs = 0; rl = 0; want(ramp);
}
function ramp(t) {
  if (mode !== "resume") return false;
  if (!rs) rs = rl = t;
  const p = Math.min(1, (t - rs) / 1100), v = W1MS * p * p * (3 - 2 * p); /* ease into the orbit speed */
  bA += v * Math.min(32, t - rl); rl = t; setBeam(bA);
  if (p >= 1) { orbit(bA); return false; }
  return true;
}
const cpan = $("#cpan");
function centrePan(x, jump) { if (cpan && cpan.scrollWidth > cpan.clientWidth) cpan.scrollTo({ left: x / 1280 * cpan.scrollWidth - cpan.clientWidth / 2, behavior: reduced || jump ? "auto" : "smooth" }); }
/* narrow screens pan the wide figures: fade only the edges that hide something */
const pans = $$(".pan").map(p => {
  const at = () => { const m = p.scrollWidth - p.clientWidth, x = p.scrollLeft; return m < 2 ? "none" : x < 2 ? "start" : x > m - 2 ? "end" : "mid"; };
  const f = () => { p.dataset.at = at(); };
  p.addEventListener("scroll", f, { passive: true }); addEventListener("resize", f); return at;
});
/* everything that reads layout waits for the first frame: read during parse, it forced a full layout
   before the fonts and images were known (a 100 ms long task); after the first paint it costs little */
function firstLayout() {
  setupDock(); centrePan(640, 1);
  const ats = pans.map(at => at()); $$(".pan").forEach((p, i) => { p.dataset.at = ats[i]; });
}
requestAnimationFrame(() => setTimeout(firstLayout, 0));
if (csv) {
  csv.setAttribute("aria-label", csv.getAttribute("aria-label") || "Provider constellation");
  seen(cfig, v => { cVis = v; if (v && mode !== "spring" && mode !== "hold") orbit(bA); else if (!v) { if (mode === "orbit") stopOrbit(); if (mode !== "hold") mode = "rest"; } });
}

RM.addEventListener && RM.addEventListener("change", e => { reduced = e.matches; setupDock(); if (reduced) { stopOrbit(); setBeam(bA); } else if (cVis) orbit(bA); });

/* the replay player and the Pilot sandbox load from assets/site/more.js when they come near the viewport
   or on the first touch of either; this keeps the inline script small. Same origin, no other requests. */
window.PC = { $, $$, D, ICE, ASH, clamp, want, seen, say, tabs, rm: () => reduced };
let moreOn = 0;
function more() { if (moreOn) return; moreOn = 1; mio.disconnect(); const sc = document.createElement("script"); sc.src = $("#more").dataset.src; document.body.append(sc); }
const mio = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting)) more(); }, { rootMargin: "900px 0px" });
["#player", "#sandbox"].forEach(s => { const el = $(s); mio.observe(el); el.addEventListener("pointerdown", more, { once: true }); el.addEventListener("focusin", more, { once: true }); });
})();
