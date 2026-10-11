# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Pulsar Stage web viewer: one self-contained HTML document.

Served live by Core at /stage?t=<view token> (EventSource on
/pulsar/stage/stream with auto-reconnect), and reused in static mode by the
HTML export (scenes embedded as escaped JSON). Inline CSS and JS only, no
external requests of any kind. Every piece of scene text reaches the DOM
through textContent / createTextNode; the page never assigns markup.
"""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any, List, Optional


def page(scenes: Optional[List[Any]] = None) -> str:
    """The viewer. With `scenes` (items or scene dicts) it renders them statically; without, it goes live."""
    if scenes is None:
        data = ""
        csp = CSP_LIVE
    else:
        from pulsar.stage.export import collect, embed_json
        items = collect(scenes)
        payload = {"mode": "static", "generated": time.strftime("%Y-%m-%d %H:%M"), "items": items}
        data = '<script type="application/json" id="pulsar-data">' + embed_json(payload) + "</script>"
        csp = CSP_STATIC
    return _TEMPLATE.replace("%%CSP%%", csp).replace("%%DATA%%", data)


def script_hash(template: Optional[str] = None) -> str:
    """The CSP source ('sha256-...') of the page's one inline script, the only script the page may run."""
    t = _TEMPLATE if template is None else template
    start = t.index("<script>") + len("<script>")
    body = t[start:t.index("</script>", start)]
    return "'sha256-%s'" % base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta http-equiv="Content-Security-Policy" content="%%CSP%%">
<meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#07060c">
<title>Pulsar Stage</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' x2='1'%3E%3Cstop offset='0' stop-color='%23FF6A00'/%3E%3Cstop offset='.5' stop-color='%23FF2E88'/%3E%3Cstop offset='1' stop-color='%2336E2FF'/%3E%3C/linearGradient%3E%3C/defs%3E%3Ccircle cx='32' cy='32' r='26' fill='none' stroke='url(%23g)' stroke-width='6'/%3E%3Ccircle cx='32' cy='32' r='11' fill='url(%23g)'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#07060c;--bg2:#0d0b16;--ink:#ECF6FF;--text:#d6dae9;--soft:#aaa6c2;--ash:#9692ac;--dust:#58546c;--night:#2c283c;
  --ember:#FF6A00;--flare:#FF2E88;--plasma:#9D4DFF;--beam:#36E2FF;--ok:#50FAAA;--warn:#FFC400;--bad:#FF465A;
  --spectrum:linear-gradient(90deg,#FF6A00 0%,#FF2E88 34%,#9D4DFF 67%,#36E2FF 100%);
  --loop:linear-gradient(90deg,#FF6A00 0%,#FF2E88 17%,#9D4DFF 33%,#36E2FF 50%,#9D4DFF 67%,#FF2E88 83%,#FF6A00 100%);
  --glass:rgba(20,17,34,.58);--glass2:rgba(255,255,255,.035);--line:rgba(255,255,255,.08);
  --sans:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Code",Menlo,Consolas,"Liberation Mono",monospace;
  --ease:cubic-bezier(.2,.8,.2,1);
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font:15px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
body{overflow:hidden}
#sky{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none}
.glow{position:fixed;inset:-20%;z-index:0;pointer-events:none;background:
  radial-gradient(40% 35% at 78% 8%,rgba(157,77,255,.16),transparent 70%),
  radial-gradient(35% 30% at 12% 92%,rgba(255,46,136,.10),transparent 70%),
  radial-gradient(30% 30% at 50% 50%,rgba(54,226,255,.05),transparent 70%)}
#app{position:relative;z-index:1;display:grid;grid-template-rows:auto 2px 1fr auto auto;height:100%;height:100dvh}
header#top{display:flex;align-items:center;gap:18px;padding:14px clamp(14px,3vw,34px);min-width:0}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;letter-spacing:.14em;font-size:13px;white-space:nowrap}
.mark{width:18px;height:18px;border-radius:50%;position:relative;flex:none;background:radial-gradient(circle at 50% 50%,#fff 0 18%,var(--beam) 19% 34%,transparent 35%);box-shadow:0 0 0 2px rgba(54,226,255,.55),0 0 18px rgba(54,226,255,.5)}
.mark::after{content:"";position:absolute;inset:-6px;border-radius:50%;border:1px solid rgba(54,226,255,.5);animation:ping 2.4s var(--ease) infinite}
.word{background:var(--loop);-webkit-background-clip:text;background-clip:text;color:transparent;background-size:200% 100%;animation:drift 8s linear infinite}
.crumb{display:flex;align-items:center;gap:10px;min-width:0;color:var(--ash);font-size:14px}
.crumb .sep{color:var(--night)}
.crumb .kind{font:600 11px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--plasma);border:1px solid rgba(157,77,255,.35);padding:4px 7px;border-radius:999px;white-space:nowrap}
.crumb .ttl{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.status{margin-left:auto;display:flex;align-items:center;gap:14px;font:500 12px/1 var(--mono);color:var(--dust);white-space:nowrap}
.status .pos{color:var(--ink);font-weight:700}
.live{display:inline-flex;align-items:center;gap:7px;padding:5px 10px;border-radius:999px;font-weight:800;letter-spacing:.12em;border:1px solid rgba(255,46,136,.4);color:var(--flare);background:rgba(255,46,136,.08)}
.live i{width:8px;height:8px;border-radius:50%;background:currentColor;position:relative}
.live.on i::after{content:"";position:absolute;inset:-4px;border-radius:50%;border:2px solid currentColor;animation:ping 1.6s var(--ease) infinite}
.live.wait{color:var(--warn);border-color:rgba(255,196,0,.4);background:rgba(255,196,0,.07)}
.live.off{color:var(--dust);border-color:var(--night);background:transparent}
.live.static{color:var(--beam);border-color:rgba(54,226,255,.4);background:rgba(54,226,255,.07)}
#rail{position:relative;background:rgba(255,255,255,.05);overflow:hidden}
#rail i{position:absolute;left:0;top:0;bottom:0;width:0;background:var(--spectrum);box-shadow:0 0 14px rgba(255,46,136,.6);transition:width .5s var(--ease)}
#stage{position:relative;overflow:hidden;min-height:0}
.view{position:absolute;inset:0;overflow:auto;padding:clamp(12px,2.6vw,36px) clamp(12px,3.4vw,48px) 28px;display:flex;flex-direction:column;align-items:center;scrollbar-width:thin;scrollbar-color:var(--night) transparent}
.view.enter{animation:enterR .5s var(--ease) both}
.view.enter.back{animation-name:enterL}
.view.leave{animation:leaveL .42s var(--ease) both;pointer-events:none}
.view.leave.back{animation-name:leaveR}
.view.glitch{animation:glitch .5s steps(1) both,enterR .5s var(--ease) both}
.view.leave.glitchout{animation:glitchOut .32s steps(2) both}
.frame{position:relative;width:100%;max-width:1240px;margin:auto 0;border-radius:22px;background:linear-gradient(180deg,rgba(255,255,255,.055),rgba(255,255,255,.015)),var(--glass);backdrop-filter:blur(18px) saturate(140%);-webkit-backdrop-filter:blur(18px) saturate(140%);box-shadow:0 30px 80px -30px rgba(0,0,0,.8),0 0 0 1px rgba(255,255,255,.04) inset;padding:clamp(20px,4vw,56px)}
.frame::before{content:"";position:absolute;inset:0;border-radius:inherit;padding:1px;background:linear-gradient(130deg,rgba(255,106,0,.7),rgba(255,46,136,.45) 30%,rgba(157,77,255,.35) 60%,rgba(54,226,255,.7));-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude;pointer-events:none}
.frame.slide{min-height:min(74vh,760px);display:flex;flex-direction:column;padding-bottom:calc(clamp(20px,4vw,56px) + 20px)}
.slidenum{position:absolute;right:22px;bottom:16px;font:600 12px/1 var(--mono);color:var(--dust);letter-spacing:.1em}
.h-title{margin:0 0 6px;font-weight:850;letter-spacing:-.025em;line-height:1.04;font-size:clamp(1.9rem,3.8vw,3.4rem);background:var(--loop);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:drift 12s linear infinite}
.h-sub{margin:0 0 10px;color:var(--soft);font-size:clamp(1rem,1.6vw,1.25rem);font-weight:500}
.rule{height:3px;width:min(320px,40%);border-radius:3px;background:var(--spectrum);margin:14px 0 26px;box-shadow:0 0 18px rgba(255,46,136,.45);transform-origin:left;animation:grow .9s var(--ease) both .15s}
.l-title{align-items:center;justify-content:center;text-align:center}
.l-title .h-title{font-size:clamp(2.6rem,8vw,7rem);line-height:.98;margin-bottom:18px}
.l-title .rule{margin:22px auto;transform-origin:center}
.l-title .md{max-width:60ch;margin:0 auto;color:var(--soft);font-size:clamp(1rem,1.5vw,1.2rem)}
.l-title .halo{position:absolute;left:50%;top:44%;width:min(70vw,780px);aspect-ratio:1;transform:translate(-50%,-50%);border-radius:50%;background:radial-gradient(closest-side,rgba(157,77,255,.18),rgba(255,46,136,.07) 55%,transparent 72%);pointer-events:none;z-index:-1}
.l-title .halo::before,.l-title .halo::after{content:"";position:absolute;inset:20%;border-radius:50%;border:1px solid rgba(54,226,255,.25);animation:wave 4.2s var(--ease) infinite}
.l-title .halo::after{animation-delay:2.1s}
.md{color:var(--text);font-size:clamp(1rem,1.35vw,1.15rem)}
.md p{margin:.4em 0 .8em}
.md h1,.md h2,.md h3,.md h4{color:var(--ink);margin:1.1em 0 .4em;line-height:1.2}
.md h1{font-size:1.7em;background:var(--spectrum);-webkit-background-clip:text;background-clip:text;color:transparent}
.md h2{font-size:1.35em}.md h3{font-size:1.12em;color:var(--soft)}.md h4{font-size:.9em;letter-spacing:.12em;text-transform:uppercase;color:var(--ash)}
.md ul,.md ol{margin:.3em 0 .9em;padding-left:1.4em}
.md ul{list-style:none;padding-left:1.5em}
.md li{margin:.42em 0;position:relative}
.md ul>li::before{content:"";position:absolute;left:-1.25em;top:.62em;width:.5em;height:.5em;border-radius:2px;transform:rotate(45deg);background:var(--c,var(--flare));box-shadow:0 0 10px var(--c,var(--flare))}
.md ol>li::marker{color:var(--beam);font-weight:800;font-family:var(--mono)}
.md strong{color:var(--ink);font-weight:750}
.md em{color:#c6b2ff;font-style:italic}
.md code{font:500 .88em var(--mono);color:#7edeff;background:rgba(54,226,255,.08);border:1px solid rgba(54,226,255,.18);padding:.08em .38em;border-radius:6px}
.md blockquote{margin:.8em 0;padding:.2em 0 .2em 1em;border-left:3px solid var(--plasma);color:var(--soft)}
.md hr{border:0;height:1px;background:var(--line);margin:1.2em 0}
.md .link{color:var(--beam);border-bottom:1px dashed rgba(54,226,255,.5)}
.stagger>*{animation:rise .55s var(--ease) both}
.l-bullets .md li{font-size:clamp(1.05rem,1.7vw,1.4rem);margin:.7em 0}
.split{display:grid;grid-template-columns:minmax(0,.9fr) minmax(0,1.1fr);gap:clamp(18px,3vw,40px);align-items:start;flex:1}
.l-quote{justify-content:center;align-items:center;text-align:center}
.qmark{font:900 clamp(5rem,12vw,9rem)/.6 Georgia,"Times New Roman",serif;background:var(--spectrum);-webkit-background-clip:text;background-clip:text;color:transparent;height:.55em;margin-bottom:.15em}
.qtext{font-size:clamp(1.4rem,2.8vw,2.5rem);line-height:1.3;color:var(--ink);font-weight:650;max-width:30ch;margin:0 auto;letter-spacing:-.01em}
.qwho{margin-top:26px;color:var(--soft);display:flex;align-items:center;gap:12px;justify-content:center;font-size:1.05rem}
.qwho::before{content:"";width:34px;height:3px;border-radius:3px;background:var(--spectrum)}
.l-stat{justify-content:center;align-items:center;text-align:center}
.stat-v{font-weight:900;letter-spacing:-.04em;line-height:.95;font-size:clamp(4rem,15vw,12rem);background:var(--spectrum);-webkit-background-clip:text;background-clip:text;color:transparent;font-variant-numeric:tabular-nums;filter:drop-shadow(0 10px 40px rgba(255,46,136,.25))}
.stat-v small{font-size:.32em;letter-spacing:0;margin-left:.12em}
.stat-l{margin-top:14px;font:700 clamp(.85rem,1.3vw,1.05rem)/1.2 var(--mono);letter-spacing:.32em;text-transform:uppercase;color:var(--ink)}
.stat-kicker{font:700 12px/1 var(--mono);letter-spacing:.3em;text-transform:uppercase;color:var(--ash);margin-bottom:22px}
.panel{position:relative;border-radius:16px;background:rgba(8,7,14,.55);border:1px solid var(--line);overflow:hidden}
.embed{margin-top:8px;flex:1;min-height:0}
.kicker{font:700 11px/1 var(--mono);letter-spacing:.24em;text-transform:uppercase;color:var(--ash);margin:0 0 12px}
/* code */
.code{font:500 13.5px/1.62 var(--mono)}
.code .bar{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.025)}
.code .dots{display:flex;gap:6px}.code .dots i{width:10px;height:10px;border-radius:50%;background:var(--night)}
.code .dots i:nth-child(1){background:#ff5f57}.code .dots i:nth-child(2){background:#febc2e}.code .dots i:nth-child(3){background:#28c840}
.code .dots,.code .lang,.code .copy{flex:none}
.code .fname{color:var(--ink);font-weight:700;min-width:0}
.code .lang{margin-left:auto;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--plasma);border:1px solid rgba(157,77,255,.35);padding:3px 8px;border-radius:999px}
.code .copy{background:transparent;border:1px solid var(--line);color:var(--ash);font:600 11px var(--mono);padding:4px 9px;border-radius:8px;cursor:pointer}
.code .copy:hover{color:var(--ink);border-color:var(--beam)}
.code pre{margin:0;padding:12px 0;overflow:auto;max-height:min(68vh,780px)}
.code .ln{display:grid;grid-template-columns:auto 1fr;white-space:pre;min-width:max-content;padding-right:18px}
.code .no{color:var(--dust);text-align:right;padding:0 14px 0 16px;user-select:none;border-right:1px solid rgba(255,255,255,.05);margin-right:14px;min-width:3.4em}
.code .ln.hl{background:linear-gradient(90deg,rgba(255,46,136,.16),rgba(255,46,136,.04) 60%,transparent);box-shadow:inset 3px 0 0 var(--flare)}
.code .ln.hl .no{color:var(--beam);font-weight:800}
.code.focus .ln:not(.hl) .src{opacity:.55}
.tk-kw{color:#ff5ca8;font-weight:700}.tk-str{color:#ffb86c}.tk-num{color:#c496ff}.tk-com{color:#706c88;font-style:italic}
.tk-fn{color:#6edeff}.tk-typ,.tk-bi{color:#7cf0d6}.tk-dec,.tk-var{color:#ba84ff}.tk-pun{color:#9692b2}.tk-key{color:#6edeff}.tk-lit{color:#ff8080}.tk-mac{color:#6edeff}
/* table */
.tablewrap{overflow:auto;max-height:min(70vh,800px);border-radius:14px;border:1px solid var(--line);background:rgba(8,7,14,.5)}
table{border-collapse:separate;border-spacing:0;width:100%;font-size:14px}
thead th{position:sticky;top:0;z-index:1;text-align:left;font:700 11.5px/1.2 var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--ink);background:rgba(16,14,28,.96);padding:13px 16px;white-space:nowrap}
thead th::after{content:"";position:absolute;left:0;right:0;bottom:0;height:2px;background:var(--spectrum);opacity:.85}
tbody td{padding:10px 16px;border-top:1px solid rgba(255,255,255,.045);color:var(--text);vertical-align:top;white-space:nowrap}
tbody tr:nth-child(even) td{background:rgba(255,255,255,.022)}
tbody tr:hover td{background:rgba(54,226,255,.06)}
td.num,th.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
td.first{color:var(--ink);font-weight:650}
td.good{color:var(--ok);font-weight:700}td.badv{color:var(--bad);font-weight:700}td.warnv{color:var(--warn)}
td.pos{color:var(--ok)}td.neg{color:#ff788c}
tbody tr{animation:rise .5s var(--ease) both}
/* Long unbroken tokens (URLs, paths) break inside their box instead of widening the card. break-word keeps an
   ordinary word whole (it does not lower the min-content width that grid, flex and table tracks are sized from);
   only text holding an overlong token gets .brk (set by markText), whose min-content no longer counts that token. */
.h-title,.h-sub,.md,.md p,.md li,.md h1,.md h2,.md h3,.md h4,.md blockquote,.qtext,.qwho,.stat-l,.stat-kicker,.kicker,.legend span,.spark-h .nm,.tile .lb,.tile .lb>span,.timeline .tt,.timeline .dd,.timeline .when,.stat-v,.tile .val,tbody td,thead th,.code .fname{overflow-wrap:break-word;min-width:0}
/* a step title and its detail keep their min-content (the longest word) as the floor of their grid tracks */
.steps .t,.steps .d{overflow-wrap:break-word}
.brk{overflow-wrap:anywhere!important}
/* A table cell or a timeline time keeps a short token (an id, a model name, a date, a price) on one line, and a
   table too wide for a phone scrolls sideways. Only free text (.prose, set by markText) and an overlong token
   (.brk) wrap there. */
tbody td.brk,thead th.brk,tbody td.prose,thead th.prose,.timeline .when.brk,.timeline .when.prose{white-space:normal}
tbody td.prose,thead th.prose{min-width:14ch}
/* What a text box paints stays near it: a stack of combining marks cannot streak over the lines around it.
   Text whose marks stack higher than its line gets a line height that holds them (.stack, set by fitStacks).
   A code line, a table cell and a dock pill clip at their own box: the line above holds other text. */
.h-title,.h-sub,.md p,.md li,.md h1,.md h2,.md h3,.md h4,.md blockquote,.qtext,.qwho,.stat-l,.stat-kicker,.kicker,.legend,.spark-h,.tile .lb,.tile .val,.timeline .tt,.timeline .dd,.timeline .when,.steps .t,.steps .d,.code .bar{clip-path:inset(-.5em -100vmax)}
.stat-v{clip-path:inset(-.8em -100vmax)}
.code .ln,tbody td,thead th,#dock button>*{clip-path:inset(0 -100vmax)}
.view .stack{line-height:2.3}
/* charts */
.chart svg{display:block;width:100%;height:auto;overflow:visible}
.chart .axis text,.chart .xl text{fill:var(--ash);font:500 12px var(--mono)}
.chart .grid line{stroke:rgba(255,255,255,.06);stroke-dasharray:3 6}
.chart .base{stroke:rgba(255,255,255,.18)}
.chart .vl{fill:var(--ink);font:700 12px var(--mono)}
.chart .draw{stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.3s var(--ease) forwards .1s}
.chart .bar{transform-box:fill-box;transform-origin:50% 100%;animation:barup .8s var(--ease) both}
.chart .bar.neg{transform-origin:50% 0}
.chart .hbar{transform-box:fill-box;transform-origin:0 50%;animation:barright .8s var(--ease) both}
.chart .hbar.neg{transform-origin:100% 50%}
.chart .area{opacity:0;animation:fade .9s ease forwards .6s}
.chart .pt{opacity:0;animation:pop .4s var(--ease) forwards;transform-box:fill-box;transform-origin:center}
.view.still *:not(.ic):not(.flow):not(.end){animation:none!important}
.view.still .draw{stroke-dashoffset:0}.view.still .area,.view.still .pt{opacity:1}
.chart .end{animation:pulse 2.2s ease-in-out infinite}
.legend{display:flex;flex-wrap:wrap;gap:16px;margin:0 0 12px;font:600 13px var(--mono);color:var(--soft)}
.legend span{display:inline-flex;align-items:center;gap:8px}.legend i{width:10px;height:10px;border-radius:3px}
.legend b{color:var(--ink)}
.sparks{display:grid;gap:22px}
.spark-h{display:flex;align-items:baseline;gap:14px;margin-bottom:6px}
.spark-h .nm{color:var(--ink);font-weight:700}.spark-h .mm{margin-left:auto;font:500 12px var(--mono);color:var(--dust)}
.spark-h .lv{font:800 1.6rem/1 var(--sans);background:var(--spectrum);-webkit-background-clip:text;background-clip:text;color:transparent}
/* diagram */
.diagram svg{display:block;width:100%;height:auto;max-height:70vh}
.diagram .node rect{fill:rgba(14,12,24,.92)}
.diagram .node text{fill:var(--ink);font:650 14px var(--sans)}
.diagram .node{animation:pop .55s var(--ease) both;transform-box:fill-box;transform-origin:center}
.diagram .edge{fill:none;stroke-width:1.8}
.diagram .flow{fill:none;stroke:#fff;stroke-width:2.2;stroke-linecap:round;stroke-dasharray:.012 .12;opacity:.75;animation:flow 2.4s linear infinite}
.diagram .elabel rect{fill:rgba(7,6,12,.92);stroke:rgba(255,255,255,.1)}
.diagram .elabel text{fill:var(--soft);font:600 12px var(--mono)}
/* metrics */
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px}
.tile{position:relative;border-radius:16px;padding:18px 18px 12px;background:rgba(8,7,14,.55);border:1px solid var(--line);overflow:hidden;animation:rise .6s var(--ease) both}
.tile::before{content:"";position:absolute;left:0;right:0;top:0;height:2px;background:var(--c,var(--spectrum));opacity:.9}
.tile .lb{display:flex;align-items:center;gap:10px;font:700 11px/1.2 var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--ash)}
.tile .dl{margin-left:auto;font:700 12px var(--mono);letter-spacing:0;padding:3px 8px;border-radius:999px}
.tile .dl.up{color:var(--ok);background:rgba(80,250,170,.1)}.tile .dl.down{color:var(--bad);background:rgba(255,70,90,.1)}.tile .dl.flat{color:var(--ash);background:rgba(255,255,255,.05)}
.tile .val{font:850 clamp(1.9rem,3.6vw,2.8rem)/1.1 var(--sans);letter-spacing:-.03em;margin:10px 0 6px;background:var(--spectrum);-webkit-background-clip:text;background-clip:text;color:transparent;font-variant-numeric:tabular-nums}
.tile .val small{font-size:.45em;letter-spacing:0;margin-left:.15em}
.tile svg{display:block;width:100%;height:46px}
/* timeline */
.timeline{position:relative;margin:4px 0 0;padding-left:0;list-style:none}
.timeline li{position:relative;display:grid;grid-template-columns:fit-content(34%) 28px minmax(0,1fr);gap:0 12px;padding:0 0 22px;animation:rise .55s var(--ease) both}
.timeline .when{font:600 12.5px/1.9 var(--mono);color:var(--ash);text-align:right;min-width:64px;white-space:nowrap}
.timeline .dot{position:relative;display:flex;justify-content:center}
.timeline .dot i{position:relative;z-index:1;margin-top:6px;width:14px;height:14px;border-radius:50%;background:var(--c);box-shadow:0 0 14px var(--c)}
.timeline li:not(:last-child) .dot::after{content:"";position:absolute;top:22px;bottom:-8px;width:2px;background:linear-gradient(var(--c),rgba(255,255,255,.06))}
.timeline .st-pending .dot i{background:transparent;border:2px solid var(--dust);box-shadow:none}
.timeline .st-active .dot i::after{content:"";position:absolute;inset:-6px;border-radius:50%;border:2px solid var(--beam);animation:ping 1.6s var(--ease) infinite}
.timeline .tt{color:var(--ink);font-weight:700;font-size:1.05rem}
.timeline .st-pending .tt{color:var(--soft);font-weight:600}
.timeline .dd{color:var(--soft);margin-top:2px}
/* progress */
.prog-h{display:flex;align-items:center;gap:16px;margin-bottom:22px}
.prog-bar{flex:1;height:10px;border-radius:999px;background:rgba(255,255,255,.06);overflow:hidden;position:relative}
.prog-bar i{position:absolute;left:0;top:0;bottom:0;border-radius:inherit;overflow:hidden;background:var(--spectrum);background-size:100vw 100%;transition:width .8s var(--ease);box-shadow:0 0 16px rgba(255,46,136,.5)}
.prog-bar i::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.55),transparent);transform:translateX(-100%);animation:shimmer 1.8s ease-in-out infinite}
.prog-n{font:800 14px var(--mono);color:var(--ink);white-space:nowrap}.prog-n span{color:var(--flare);margin-left:8px}.prog-n em{color:var(--bad);font-style:normal;margin-left:8px}
.steps{list-style:none;margin:0;padding:0}
.steps li{display:grid;grid-template-columns:30px 1fr fit-content(40%);align-items:center;gap:12px;padding:11px 14px;border-radius:12px;animation:rise .5s var(--ease) both}
.steps li+li{margin-top:4px}
.steps .ic{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font:900 13px/1 var(--sans)}
.steps .done .ic{background:rgba(80,250,170,.14);color:var(--ok)}
.steps .pending .ic{border:2px solid var(--night)}
.steps .failed .ic{background:rgba(255,70,90,.16);color:var(--bad);animation:shake .5s var(--ease) 1}
.steps .active{background:linear-gradient(90deg,rgba(54,226,255,.10),transparent);box-shadow:inset 2px 0 0 var(--beam)}
.steps .active .ic{border:2.5px solid rgba(54,226,255,.2);border-top-color:var(--beam);border-right-color:var(--plasma);animation:spin .9s linear infinite}
.steps .t{color:var(--ink);font-weight:650}.steps .pending .t{color:var(--ash);font-weight:500}.steps .failed .t{color:var(--bad)}
.steps .done .t{color:var(--soft)}
.steps .d{font:500 12.5px var(--mono);color:var(--dust)}.steps .failed .d{color:#ff8a98}
/* card */
.callout{position:relative;border-radius:18px;padding:24px 28px;background:linear-gradient(135deg,color-mix(in srgb,var(--c) 12%,transparent),rgba(8,7,14,.55) 55%);border:1px solid color-mix(in srgb,var(--c) 45%,transparent);box-shadow:0 0 40px -12px var(--c);max-width:880px;margin:0 auto;width:100%}
.callout .tag{display:inline-flex;align-items:center;gap:8px;font:800 11px/1 var(--mono);letter-spacing:.2em;color:var(--c);margin-bottom:12px}
.callout .tag i{width:22px;height:22px;border-radius:7px;display:grid;place-items:center;background:color-mix(in srgb,var(--c) 18%,transparent);font-style:normal;font-size:13px}
/* scene header */
.sh{margin-bottom:18px}
.sh .h-title{font-size:clamp(1.5rem,2.8vw,2.4rem)}
/* idle */
#idle{position:absolute;inset:0;display:grid;place-items:center;text-align:center;padding:24px}
#idle .beacon{position:relative;width:132px;height:132px;margin:0 auto 34px}
#idle .beacon i{position:absolute;inset:0;border-radius:50%;border:2px solid rgba(54,226,255,.55);animation:wave 3.2s var(--ease) infinite}
#idle .beacon i:nth-child(2){animation-delay:.8s;border-color:rgba(157,77,255,.55)}#idle .beacon i:nth-child(3){animation-delay:1.6s;border-color:rgba(255,46,136,.55)}#idle .beacon i:nth-child(4){animation-delay:2.4s;border-color:rgba(255,106,0,.5)}
#idle .beacon b{position:absolute;inset:44px;border-radius:50%;background:radial-gradient(circle,#fff 0 20%,var(--beam) 30%,rgba(157,77,255,.6) 60%,transparent 72%);box-shadow:0 0 50px rgba(54,226,255,.7);animation:core 2.2s ease-in-out infinite}
#idle h1{margin:0;font-weight:900;letter-spacing:.2em;font-size:clamp(1.8rem,5vw,3.6rem);background:var(--loop);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:drift 8s linear infinite}
#idle .await{margin:14px 0 26px;font:700 13px var(--mono);letter-spacing:.42em;color:var(--soft)}
#idle .await::after{content:"_";color:var(--beam);animation:blink 1s steps(1) infinite}
#idle .hint{color:var(--dust);max-width:52ch;margin:6px auto;font-size:14px}
#idle .hint code{font:500 13px var(--mono);color:var(--beam)}
#idle .state{font:600 12.5px var(--mono);color:var(--ash);margin-bottom:12px}
.banner{position:fixed;left:50%;top:70px;transform:translateX(-50%);z-index:5;background:rgba(40,10,20,.92);border:1px solid rgba(255,70,90,.5);color:#ffd3da;padding:10px 16px;border-radius:12px;font-size:14px;box-shadow:0 10px 40px rgba(0,0,0,.5)}
.banner code{font:600 13px var(--mono);color:#fff}
/* dock + footer */
#dock{display:flex;gap:8px;padding:6px clamp(12px,3vw,34px) 4px;overflow-x:auto;scrollbar-width:none}
#dock:empty{display:none}
#dock button{flex:none;display:inline-flex;align-items:center;gap:8px;max-width:260px;border-radius:999px;border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--ash);font:600 12px var(--sans);padding:6px 12px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#dock button b{font:700 10px var(--mono);letter-spacing:.12em;color:var(--plasma)}
#dock button.on{color:var(--ink);border-color:rgba(54,226,255,.6);background:rgba(54,226,255,.08);box-shadow:0 0 18px -6px var(--beam)}
#dock button:hover{color:var(--ink)}
footer#foot{display:flex;align-items:center;gap:18px;padding:8px clamp(12px,3vw,34px) 12px;font:500 12px var(--mono);color:var(--dust);white-space:nowrap;overflow:hidden}
footer kbd{font:700 11px var(--mono);color:var(--beam);background:rgba(54,226,255,.07);border:1px solid rgba(54,226,255,.22);border-bottom-width:2px;border-radius:6px;padding:2px 6px;margin-right:6px}
footer .navbtns{margin-left:auto;display:flex;gap:6px}
footer .navbtns button{border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--soft);border-radius:10px;width:36px;height:30px;cursor:pointer;font:700 15px var(--sans)}
footer .navbtns button:hover{color:var(--ink);border-color:var(--beam)}
footer .follow{color:var(--ok);font-weight:700}footer .follow.pinned{color:var(--warn)}
#toasts{position:fixed;right:20px;bottom:64px;z-index:6;display:flex;flex-direction:column;gap:8px;align-items:flex-end;pointer-events:none}
.toast{background:rgba(16,14,28,.94);border:1px solid var(--line);border-left:3px solid var(--c,var(--beam));color:var(--ink);padding:9px 14px;border-radius:12px;font:600 13px var(--sans);box-shadow:0 12px 40px rgba(0,0,0,.5);animation:toast 3.2s var(--ease) both}
#help,#notes{position:fixed;z-index:7;display:none}
#help.on{display:grid;inset:0;place-items:center;background:rgba(4,3,8,.6);backdrop-filter:blur(6px)}
#help .box{background:rgba(16,14,28,.96);border:1px solid var(--line);border-radius:18px;padding:22px 26px;min-width:min(440px,92vw)}
#help h3{margin:0 0 14px;font:800 12px var(--mono);letter-spacing:.3em;color:var(--ink)}
#help dl{display:grid;grid-template-columns:auto 1fr;gap:8px 18px;margin:0}
#help dt{font:700 12px var(--mono);color:var(--beam)}#help dd{margin:0;color:var(--soft);font-size:14px}
#notes.on{display:block;left:50%;bottom:70px;transform:translateX(-50%);width:min(860px,92vw);background:rgba(16,14,28,.95);border:1px solid rgba(157,77,255,.4);border-radius:16px;padding:14px 18px;color:var(--soft)}
#notes,.toast{overflow-wrap:anywhere}
.toast{max-width:min(560px,88vw)}
#notes b{display:block;font:800 11px var(--mono);letter-spacing:.24em;color:var(--plasma);margin-bottom:6px}
@keyframes ping{0%{transform:scale(.6);opacity:.9}100%{transform:scale(2.2);opacity:0}}
@keyframes wave{0%{transform:scale(.35);opacity:.9}100%{transform:scale(1.9);opacity:0}}
@keyframes core{0%,100%{transform:scale(1)}50%{transform:scale(1.18)}}
@keyframes drift{0%{background-position:0% 0}100%{background-position:200% 0}}
@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@keyframes fade{to{opacity:1}}
@keyframes pop{from{opacity:0;transform:scale(.9)}to{opacity:1;transform:none}}
@keyframes draw{to{stroke-dashoffset:0}}
@keyframes barup{from{transform:scaleY(0)}to{transform:scaleY(1)}}
@keyframes barright{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes flow{from{stroke-dashoffset:.132}to{stroke-dashoffset:0}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes shimmer{0%{transform:translateX(-100%)}60%,100%{transform:translateX(100%)}}
@keyframes shake{0%,100%{transform:none}20%{transform:translateX(-3px)}40%{transform:translateX(3px)}60%{transform:translateX(-2px)}80%{transform:translateX(2px)}}
@keyframes blink{50%{opacity:0}}
@keyframes toast{0%{opacity:0;transform:translateY(8px)}8%,85%{opacity:1;transform:none}100%{opacity:0;transform:translateY(-4px)}}
@keyframes enterR{from{opacity:0;transform:translateX(48px) scale(.985)}to{opacity:1;transform:none}}
@keyframes enterL{from{opacity:0;transform:translateX(-48px) scale(.985)}to{opacity:1;transform:none}}
@keyframes leaveL{to{opacity:0;transform:translateX(-48px) scale(.985)}}
@keyframes leaveR{to{opacity:0;transform:translateX(48px) scale(.985)}}
@keyframes glitch{0%{clip-path:inset(12% 0 64% 0);transform:translate(-14px,0);filter:hue-rotate(80deg) saturate(2)}12%{clip-path:inset(54% 0 18% 0);transform:translate(12px,0)}24%{clip-path:inset(30% 0 40% 0);transform:translate(-6px,0);filter:hue-rotate(-60deg)}36%{clip-path:inset(0 0 0 0);transform:none;filter:none}100%{clip-path:inset(0 0 0 0)}}
@keyframes glitchOut{0%{opacity:1;filter:hue-rotate(90deg) saturate(3);transform:translate(8px,0)}50%{opacity:.6;transform:translate(-10px,0);clip-path:inset(20% 0 30% 0)}100%{opacity:0}}
@media (max-width:760px){.split{grid-template-columns:1fr}.code .bar{flex-wrap:wrap}.code .fname.brk{order:9;flex:1 1 100%}.status .sid,.status .clock,footer .hints{display:none}.frame{border-radius:16px}.frame.slide{min-height:auto}header#top{gap:10px}.crumb .kind{display:none}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}.chart .draw{stroke-dashoffset:0}.chart .area,.chart .pt{opacity:1}}
@media print{body{overflow:visible;background:#07060c}#sky,.glow,header#top,#rail,#dock,footer#foot,#toasts{display:none}#app{display:block;height:auto}#stage{overflow:visible}.view{position:relative;inset:auto;overflow:visible;page-break-after:always;min-height:100vh}*{animation:none!important}}
</style>
</head>
<body>
<canvas id="sky" aria-hidden="true"></canvas>
<div class="glow" aria-hidden="true"></div>
<div id="app">
  <header id="top">
    <div class="brand"><span class="mark" aria-hidden="true"></span><span class="word">PULSAR STAGE</span></div>
    <div class="crumb"><span class="sep">/</span><span class="kind" id="kind"></span><span class="ttl" id="ttl"></span></div>
    <div class="status"><span class="live off" id="live"><i></i><b id="livetxt">CONNECTING</b></span><span class="sid" id="sid"></span><span class="pos" id="pos"></span><span class="clock" id="clock"></span></div>
  </header>
  <div id="rail"><i id="railfill"></i></div>
  <main id="stage" aria-live="polite"></main>
  <nav id="dock" aria-label="Scenes"></nav>
  <footer id="foot"><span class="hints"><kbd>&larr; &rarr;</kbd>slides &nbsp; <kbd>[ ]</kbd>scenes &nbsp; <kbd>f</kbd>follow &nbsp; <kbd>n</kbd>notes &nbsp; <kbd>?</kbd>help</span><span class="follow" id="follow">FOLLOW</span><span class="navbtns"><button id="prev" aria-label="Previous">&#8249;</button><button id="next" aria-label="Next">&#8250;</button></span></footer>
</div>
<div id="toasts" aria-live="polite"></div>
<div id="help" role="dialog" aria-label="Keyboard shortcuts"><div class="box"><h3>KEYS</h3><dl>
<dt>&larr; &rarr; space</dt><dd>previous / next slide</dd><dt>PgUp PgDn</dt><dd>previous / next slide</dd>
<dt>Home End</dt><dd>first / last slide</dd><dt>1-9</dt><dd>jump to slide</dd><dt>[ ]</dt><dd>previous / next scene</dd>
<dt>f</dt><dd>follow the newest scene</dd><dt>n</dt><dd>speaker notes</dd><dt>p</dt><dd>print every slide (PDF)</dd><dt>Esc</dt><dd>close</dd>
</dl></div></div>
<div id="notes"><b>NOTES</b><span id="notestxt"></span></div>
%%DATA%%
<script>
(function () {
"use strict";
var RM = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
var SPEC = [[255,106,0],[255,46,136],[157,77,255],[54,226,255]];
var SERIES = ["#36E2FF","#FF2E88","#FF6A00","#9D4DFF","#50FAAA","#FFC400","#FF8CC8","#78AAFF"];
var TONES = {info:"#36E2FF",success:"#50FAAA",warn:"#FFC400",error:"#FF465A",neutral:"#9692ac"};
var TONE_TAG = {info:["◉","INFO"],success:["✓","SUCCESS"],warn:["▲","WARNING"],error:["✗","ERROR"],neutral:["•","NOTE"]};
var KIND_LABEL = {deck:"deck",card:"card",markdown:"notes",code:"code",table:"table",chart:"chart",diagram:"diagram",metrics:"metrics",timeline:"timeline",progress:"progress"};
var SVGNS = "http://www.w3.org/2000/svg";
var uid = 0, QUIET = false, Q3 = "\x22\x22\x22";
/* maps keyed by scene text (a code language, a node id) never inherit: "constructor" or "__proto__" is a plain key */
function dict(src) { var d = Object.create(null); if (src) for (var k in src) if (Object.prototype.hasOwnProperty.call(src, k)) d[k] = src[k]; return d; }

/* ---------- dom helpers (text only ever goes through textContent) ---------- */
function el(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
function sv(tag, attrs, text) {
  var n = document.createElementNS(SVGNS, tag);
  if (attrs) for (var k in attrs) if (Object.prototype.hasOwnProperty.call(attrs, k)) n.setAttribute(k, String(attrs[k]));
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
function add(parent) { for (var i = 1; i < arguments.length; i++) if (arguments[i]) parent.appendChild(arguments[i]); return parent; }
function str(v) { return v === null || v === undefined ? "" : String(v); }
/* Text boxes holding an overlong token (a URL, a path) may break it anywhere (.brk); a table cell or a timeline
   time holding free text (.prose) wraps at spaces, while a short token there (an id, a date) stays whole.
   Combining marks do not count toward a token's length. Text that stacks combining marks is returned, so it can
   be measured once the view is in the page (fitStacks). Ordinary words keep wrapping at spaces. */
var LONGTOK = /\S{24,}/, STACK = null, MARKS = null, FORMAT = null;
try {
  STACK = new RegExp("[\\p{Mn}\\p{Me}](?:\\p{Cf}*[\\p{Mn}\\p{Me}]){2,}", "u"); MARKS = new RegExp("\\p{M}", "gu"); FORMAT = new RegExp("\\p{Cf}", "gu");
} catch (e) { STACK = null; MARKS = null; FORMAT = null; }
function markText(root) {
  var stacks = [];
  if (!root || !document.createTreeWalker) return stacks;
  var w = document.createTreeWalker(root, 4, null), n, p, bare, lim;
  while ((n = w.nextNode())) {
    p = n.parentNode;
    if (!p || !p.classList || p.namespaceURI === SVGNS) continue;
    bare = (MARKS ? n.data.replace(MARKS, "") : n.data).trim();
    /* free text: words longer than a date and time together; a timeline time shares its row with the title */
    lim = p.tagName === "TD" || p.tagName === "TH" ? 24 : p.classList.contains("when") ? 16 : 0;
    if (LONGTOK.test(bare)) p.classList.add("brk");
    else if (lim && bare.length > lim && /\s/.test(bare)) p.classList.add("prose");
    if (STACK && STACK.test(n.data)) stacks.push([p, n.data]);
  }
  return stacks;
}
/* A run of stacked marks may paint above or below its line. Each such text is measured against its own font and
   its box gets just the line height that holds the ink (.stack), so pointed Hebrew keeps its normal spacing while
   a tall Tibetan stack or a capped flood gets room. Without ink metrics, .stack alone sets a tall line. */
function fitStacks(list) {
  (list || []).forEach(function (it) {
    var p = it[0], m = null;
    if (!p.isConnected) return;
    /* the page stacks the marks on both sides of an invisible format character (a soft hyphen) on one base, so
       they are measured without it */
    var cs = getComputedStyle(p), text = FORMAT ? it[1].replace(FORMAT, "") : it[1];
    if (cs.textTransform === "uppercase") text = text.toUpperCase();
    if (!measureCtx) { var c = document.createElement("canvas"); measureCtx = c.getContext("2d"); }
    if (measureCtx) { measureCtx.font = [cs.fontStyle, cs.fontWeight, cs.fontSize, cs.fontFamily].join(" "); m = measureCtx.measureText(text); }
    var A = m ? m.fontBoundingBoxAscent : NaN, D = m ? m.fontBoundingBoxDescent : NaN;
    if (!(A > 0) || !(D >= 0) || !isFinite(m.actualBoundingBoxAscent) || !isFinite(m.actualBoundingBoxDescent)) { p.classList.add("stack"); return; }
    /* a line box of height L puts (L + A - D) / 2 above the baseline and (L - A + D) / 2 below it */
    var lh = parseFloat(cs.lineHeight), need = Math.max(2 * m.actualBoundingBoxAscent - A + D, 2 * m.actualBoundingBoxDescent + A - D) + 2;
    if (!(lh > 0)) lh = (A + D) * 1.2;
    /* a code line grows as a whole, so its number stays on the baseline of its text */
    var box = (p.closest && p.closest(".code .ln")) || p;
    if (need > lh + 0.5) { box.classList.add("stack"); box.style.lineHeight = Math.ceil(need) + "px"; }
  });
}
function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
function lerp(a, b, t) { return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]; }
function grad(t) {
  t = clamp(t, 0, 1); var seg = t * (SPEC.length - 1); var i = Math.min(Math.floor(seg), SPEC.length - 2);
  var c = lerp(SPEC[i], SPEC[i + 1], seg - i);
  return "rgb(" + Math.round(c[0]) + "," + Math.round(c[1]) + "," + Math.round(c[2]) + ")";
}
function spectrumDefs(defs, id, vertical) {
  var g = sv("linearGradient", vertical ? {id: id, x1: 0, y1: 1, x2: 0, y2: 0} : {id: id, x1: 0, y1: 0, x2: 1, y2: 0});
  [["0%", "#FF6A00"], ["34%", "#FF2E88"], ["67%", "#9D4DFF"], ["100%", "#36E2FF"]].forEach(function (s) { g.appendChild(sv("stop", {offset: s[0], "stop-color": s[1]})); });
  defs.appendChild(g); return id;
}
var measureCtx = null;
function textW(s, font) {
  if (!measureCtx) { var c = document.createElement("canvas"); measureCtx = c.getContext("2d"); }
  if (!measureCtx) return str(s).length * 8;
  measureCtx.font = font; return measureCtx.measureText(str(s)).width;
}

function clipBox(defs, id, x, y, w, h) {
  /* a clipPath over one box: a stack of marks in a chart label cannot paint over the plot or the next row */
  var cp = sv("clipPath", {id: id});
  cp.appendChild(sv("rect", {x: x, y: y, width: Math.max(0, w), height: Math.max(0, h)})); defs.appendChild(cp);
  return "url(#" + id + ")";
}

/* ---------- decrypt + count-up ---------- */
var GLYPHS = "█▓▒░╳╱╲◢◣◤◥01";
function decrypt(node, text, delay) {
  text = str(text); node.textContent = text;
  if (RM || QUIET || !text) return;
  var start = performance.now() + (delay || 0), dur = 520 + Math.min(500, text.length * 14);
  var order = []; for (var i = 0; i < text.length; i++) order.push(i / text.length * 0.8 + Math.random() * 0.12);
  function step(now) {
    if (!node.isConnected) return;
    var p = (now - start) / dur;
    if (p >= 1) { node.textContent = text; return; }
    var out = "";
    for (var j = 0; j < text.length; j++) {
      var ch = text[j];
      if (ch === " " || p >= order[j]) out += ch;
      else if (p < order[j] - 0.25) out += " ";
      else out += GLYPHS[(Math.random() * GLYPHS.length) | 0];
    }
    node.textContent = out; requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}
var NUM_PART = /^(\D*?)(-?\d[\d,]*\.?\d*|-?\.\d+)(.*)$/;
function splitValue(v) {
  v = str(v); var m = NUM_PART.exec(v);
  if (!m) return [v, ""];
  if (m[3] && /[A-Za-z]/.test(m[3])) return [m[1] + m[2], m[3].trim()];
  return [v, ""];
}
function countUp(node, value, delay) {
  value = str(value); var m = NUM_PART.exec(value);
  node.textContent = value;
  if (RM || QUIET || !m) return;
  var target = parseFloat(m[2].replace(/,/g, ""));
  if (!isFinite(target)) return;
  var dec = m[2].indexOf(".") >= 0 ? m[2].split(".")[1].length : 0, commas = m[2].indexOf(",") >= 0;
  var start = performance.now() + (delay || 0), dur = 1100;
  function fmt(x) {
    var s = x.toFixed(dec);
    if (commas) { var parts = s.split("."); parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ","); s = parts.join("."); }
    return m[1] + s + m[3];
  }
  function step(now) {
    if (!node.isConnected) return;
    var p = clamp((now - start) / dur, 0, 1), k = 1 - Math.pow(1 - p, 3);
    node.textContent = p >= 1 ? value : fmt(target * k);
    if (p < 1) requestAnimationFrame(step);
  }
  node.textContent = fmt(0); requestAnimationFrame(step);
}
function bigValue(cls, value, delay) {
  var parts = splitValue(value), n = el("div", cls), num = el("span");
  n.appendChild(num); countUp(num, parts[0], delay);
  if (parts[1]) n.appendChild(el("small", "", parts[1]));
  return n;
}

/* ---------- markdown-lite ---------- */
var INLINE = /(\*\*[^*\n]+?\*\*|__[^_\n]+?__|`[^`\n]+`|\[[^\]\n]+\]\([^)\s]*\)|(^|[^\w*])\*[^*\s](?:[^*\n]*?[^*\s])?\*(?![\w*]))/g;
function inline(parent, s) {
  s = str(s); var pos = 0, m; INLINE.lastIndex = 0;
  while ((m = INLINE.exec(s)) !== null) {
    var tok = m[0], lead = "";
    if (m[2] !== undefined && tok.charAt(0) !== "*") { lead = m[2]; tok = tok.slice(lead.length); }
    var at = m.index + lead.length;
    if (at > pos) parent.appendChild(document.createTextNode(s.slice(pos, at)));
    if (tok.slice(0, 2) === "**" || tok.slice(0, 2) === "__") parent.appendChild(el("strong", "", tok.slice(2, -2)));
    else if (tok.charAt(0) === "`") parent.appendChild(el("code", "", tok.slice(1, -1)));
    else if (tok.charAt(0) === "[") parent.appendChild(el("span", "link", tok.slice(1, tok.indexOf("]"))));
    else parent.appendChild(el("em", "", tok.slice(1, -1)));
    pos = m.index + m[0].length;
  }
  if (pos < s.length) parent.appendChild(document.createTextNode(s.slice(pos)));
  return parent;
}
function md(text, stagger) {
  var root = el("div", "md" + (stagger ? " stagger" : "")), lines = str(text).split("\n");
  var para = [], list = null, listKind = "", code = null, codeLang = "", items = 0, delayStep = stagger ? 70 : 0;
  function animate(n) { if (delayStep) n.style.animationDelay = (120 + items * delayStep) + "ms"; items++; }
  function flushP() { if (para.length) { var p = inline(el("p"), para.join(" ")); animate(p); root.appendChild(p); para = []; } }
  function closeList() { list = null; listKind = ""; }
  for (var i = 0; i < lines.length; i++) {
    var raw = lines[i], st = raw.trim(), m;
    if (code !== null) {
      if (st.indexOf("```") === 0) { root.appendChild(codePanel({lang: codeLang, text: code.join("\n")}, true)); code = null; }
      else code.push(raw);
      continue;
    }
    if (st.indexOf("```") === 0) { flushP(); closeList(); code = []; codeLang = st.slice(3).trim(); continue; }
    if (!st) { flushP(); closeList(); continue; }
    if (/^([-*_])(\s*\1){2,}$/.test(st)) { flushP(); closeList(); root.appendChild(el("hr")); continue; }
    if ((m = /^(#{1,4})\s+(.*)$/.exec(st))) { flushP(); closeList(); var h = inline(el("h" + m[1].length), m[2].replace(/#+\s*$/, "")); animate(h); root.appendChild(h); continue; }
    if ((m = /^(\s*)[-*+•]\s+(.*)$/.exec(raw)) || (m = /^(\s*)(\d{1,4})[.)]\s+(.*)$/.exec(raw))) {
      flushP();
      var ordered = m.length === 4 && /^\d/.test(m[2]);
      var kind = ordered ? "ol" : "ul";
      if (!list || listKind !== kind) { list = el(kind); listKind = kind; if (ordered) list.setAttribute("start", String(parseInt(m[2], 10) || 1)); root.appendChild(list); }
      var li = inline(el("li"), ordered ? m[3] : m[2]);
      if (!ordered) li.style.setProperty("--c", grad((list.children.length * 0.17) % 1));
      if (m[1].length >= 2) li.style.marginLeft = Math.min(3, Math.floor(m[1].length / 2)) * 1.2 + "em";
      animate(li); list.appendChild(li); continue;
    }
    if ((m = /^>\s?(.*)$/.exec(st))) { flushP(); closeList(); var q = inline(el("blockquote"), m[1]); animate(q); root.appendChild(q); continue; }
    if (list && /^\s/.test(raw) && list.lastChild) { list.lastChild.appendChild(document.createTextNode(" ")); inline(list.lastChild, st); continue; }
    closeList(); para.push(st);
  }
  if (code !== null) root.appendChild(codePanel({lang: codeLang, text: code.join("\n")}, true));
  flushP();
  return root;
}

/* ---------- code highlighting ---------- */
var ALIAS = dict({python:"py",py3:"py",python3:"py",javascript:"js",jsx:"js",mjs:"js",cjs:"js",node:"js",typescript:"ts",tsx:"ts",golang:"go",rs:"rust",bash:"sh",shell:"sh",zsh:"sh",console:"sh",fish:"sh",powershell:"sh",ps1:"sh",yml:"yaml",toml:"yaml",ini:"yaml",postgres:"sql",postgresql:"sql",mysql:"sql",sqlite:"sql",jsonc:"json",json5:"json"});
var KW = dict({
  py:"False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case self cls",
  js:"break case catch class const continue debugger default delete do else export extends finally for function if import in instanceof let new return super switch this throw try typeof var void while with yield async await of static get set from as",
  ts:"break case catch class const continue debugger default delete do else export extends finally for function if import in instanceof let new return super switch this throw try typeof var void while with yield async await of static get set from as interface type enum implements private public protected readonly declare namespace abstract keyof satisfies is infer",
  go:"break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var",
  rust:"as async await break const continue crate dyn else enum extern fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait type unsafe use where while",
  sh:"if then else elif fi for while until do done case esac function in return export local readonly set unset echo exit source alias cd test shift trap eval exec sudo",
  sql:"select from where join left right inner outer full cross on group by order having limit offset insert into values update set delete create alter drop table index view primary key foreign references not null and or as distinct union all case when then else end with returning default unique check exists between like in is asc desc begin commit rollback",
  generic:"if else for while do return break continue function def class import from export const let var new public private protected static void int float double bool string struct enum switch case default try catch finally throw throws interface package fn func match use mod impl trait"
});
var LIT = dict({js:"true false null undefined NaN Infinity",ts:"true false null undefined NaN Infinity never unknown any string number boolean void object",go:"nil true false iota",rust:"true false Some None Ok Err",sh:"true false",sql:"true false null",generic:"true false null nil None True False",json:"true false null",yaml:"true false yes no null"});
var BI = dict({py:"print len range str int float dict list set tuple bool type isinstance super open enumerate zip map filter sorted min max sum any all repr object Exception ValueError TypeError KeyError RuntimeError",go:"string int int64 int32 uint8 byte rune error bool float64 float32 any make len cap append new panic",rust:"i8 i16 i32 i64 u8 u16 u32 u64 usize isize f32 f64 bool char str String Vec Option Result Box"});
var rxCache = dict();
function langOf(l) { l = str(l).trim().toLowerCase(); if (KW[l] || l === "json" || l === "yaml") return l; return ALIAS[l] || "generic"; }
function words(s) { return s.split(" ").sort(function (a, b) { return b.length - a.length; }).join("|"); }
function langRx(lang) {
  if (rxCache[lang]) return rxCache[lang];
  var parts = [];
  if (lang === "py" || lang === "sh" || lang === "yaml") parts.push(["com", "#.*$"]);
  if (lang === "generic") parts.push(["com", "(?://|#).*$"]);
  if (["js","ts","go","rust","generic","json"].indexOf(lang) >= 0) { parts.push(["bcom", "/\\*[\\s\\S]*?(?:\\*/|$)"]); if (lang !== "generic") parts.push(["com", "//.*$"]); }
  if (lang === "sql") { parts.push(["com", "--.*$"]); parts.push(["bcom", "/\\*[\\s\\S]*?(?:\\*/|$)"]); }
  if (lang === "py") { parts.push(["tri", "[rRbBfFuU]{0,2}(?:\"\"\"[\\s\\S]*?(?:\"\"\"|$)|'''[\\s\\S]*?(?:'''|$))"]); parts.push(["dec", "^\\s*@[\\w.]+"]); }
  if (lang === "json") parts.push(["key", "\"(?:[^\"\\\\]|\\\\.)*\"(?=\\s*:)"]);
  if (lang === "yaml") parts.push(["key", "^\\s*(?:-\\s+)?[^\\s#:\"'][^#:]*?(?=\\s*:(?:\\s|$))"]);
  var q = "\"(?:[^\"\\\\]|\\\\.)*\"?|'(?:[^'\\\\]|\\\\.)*'?";
  if (lang === "js" || lang === "ts" || lang === "go") q += "|`[^`]*`?";
  parts.push(["str", q]);
  if (lang === "sh") parts.push(["var", "\\$\\{[^}]*\\}|\\$[A-Za-z_][A-Za-z0-9_]*|\\$[0-9@#?$!*-]"]);
  parts.push(["num", "\\b(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|\\d[\\d_]*(?:\\.\\d[\\d_]*)?(?:[eE][+-]?\\d+)?)\\b"]);
  if (KW[lang]) parts.push(["kw", "\\b(?:" + words(KW[lang]) + ")\\b"]);
  if (LIT[lang]) parts.push(["lit", "\\b(?:" + words(LIT[lang]) + ")\\b"]);
  if (BI[lang]) parts.push(["bi", "\\b(?:" + words(BI[lang]) + ")\\b"]);
  if (lang === "rust") parts.push(["mac", "\\b[a-z_][a-z0-9_]*!"]);
  if (["json","yaml","sql"].indexOf(lang) < 0) parts.push(["fn", "\\b[A-Za-z_]\\w*(?=\\s*\\()"]);
  if (["ts","go","rust","py","js","generic"].indexOf(lang) >= 0) parts.push(["typ", "\\b[A-Z][A-Za-z0-9_]*\\b"]);
  parts.push(["pun", "[{}\\[\\]();,.:<>=+\\-*/%!&|^~?@]+"]);
  var rx = new RegExp(parts.map(function (p) { return "(" + p[1] + ")"; }).join("|"), "g" + (lang === "sql" ? "i" : ""));
  rxCache[lang] = {rx: rx, names: parts.map(function (p) { return p[0]; })};
  return rxCache[lang];
}
function highlight(text, lang) {
  lang = langOf(lang); var spec = langRx(lang), out = [], state = "";
  str(text).split("\n").forEach(function (line) {
    var segs = [], pos = 0;
    if (state) {
      var endTok = state === "bcom" ? "*/" : state, j = line.indexOf(endTok), cls = state === "bcom" ? "com" : "str";
      if (j < 0) { out.push([[line, cls]]); return; }
      segs.push([line.slice(0, j + endTok.length), cls]); pos = j + endTok.length; state = "";
    }
    spec.rx.lastIndex = pos; var m;
    while ((m = spec.rx.exec(line)) !== null) {
      if (m[0] === "") { spec.rx.lastIndex++; continue; }
      if (m.index > pos) segs.push([line.slice(pos, m.index), ""]);
      var name = "pun";
      for (var g = 1; g < m.length; g++) if (m[g] !== undefined) { name = spec.names[g - 1]; break; }
      var tok = m[0];
      if (name === "bcom" && tok.slice(-2) !== "*/") state = "bcom";
      if (name === "tri") { var qq = tok.indexOf(Q3) >= 0 ? Q3 : "\x27\x27\x27"; var body = tok.slice(tok.indexOf(qq) + 3); if (body.slice(-3) !== qq) state = qq; }
      segs.push([tok, name === "bcom" ? "com" : name === "tri" ? "str" : name]);
      pos = m.index + tok.length;
    }
    if (pos < line.length) segs.push([line.slice(pos), ""]);
    out.push(segs);
  });
  return out;
}
function codePanel(code, inMd) {
  code = code || {}; var text = str(code.text).replace(/\s+$/, ""), hl = {}, anyHl = false;
  (code.highlight || []).forEach(function (n) { n = parseInt(n, 10); if (n > 0) { hl[n] = true; anyHl = true; } });
  var wrap = el("div", "panel code" + (anyHl ? " focus" : "")), bar = el("div", "bar");
  add(bar, add(el("span", "dots"), el("i"), el("i"), el("i")));
  if (code.filename) { var fname = el("span", "fname", code.filename); fname.title = str(code.filename); bar.appendChild(fname); }
  var lang = str(code.lang);
  if (lang) bar.appendChild(el("span", "lang", lang));
  if (!inMd && navigator.clipboard) {
    var btn = el("button", "copy", "copy"); btn.type = "button";
    btn.addEventListener("click", function () { navigator.clipboard.writeText(text).then(function () { btn.textContent = "copied"; setTimeout(function () { btn.textContent = "copy"; }, 1400); }, function () {}); });
    if (!lang) btn.style.marginLeft = "auto";
    bar.appendChild(btn);
  }
  if (!inMd) wrap.appendChild(bar);
  var pre = el("pre"), first = null;
  highlight(text, lang).forEach(function (segs, i) {
    var ln = el("div", "ln" + (hl[i + 1] ? " hl" : "")), src = el("span", "src");
    ln.appendChild(el("span", "no", i + 1));
    segs.forEach(function (s) { src.appendChild(s[1] ? el("span", "tk-" + s[1], s[0]) : document.createTextNode(s[0])); });
    if (!segs.length) src.appendChild(document.createTextNode(" "));
    ln.appendChild(src); pre.appendChild(ln);
    if (hl[i + 1] && !first) first = ln;
  });
  wrap.appendChild(pre);
  if (first) setTimeout(function () { if (first.isConnected) pre.scrollTop = Math.max(0, first.offsetTop - 60); }, 30);
  return wrap;
}

/* ---------- numbers + axes ---------- */
function niceTicks(lo, hi, n) {
  if (hi < lo) { var t = lo; lo = hi; hi = t; }
  if (hi === lo) { if (hi === 0) return [0, 1, 0.25]; var pad = Math.abs(hi) * 0.1; lo -= pad; hi += pad; }
  if (!isFinite(lo) || !isFinite(hi)) return [0, 1, 0.25];
  var raw = (hi / 2 - lo / 2) / Math.max(1, n - 1) * 2, mag = Math.pow(10, Math.floor(Math.log10(raw))), step = mag;
  if (!(raw > 0) || !isFinite(raw) || !(mag > 0) || !isFinite(mag)) return [lo, hi > lo ? hi : lo + 1, hi > lo ? (hi - lo) || 1 : 1];
  var ms = [1, 2, 2.5, 5, 10];
  for (var i = 0; i < ms.length; i++) { step = ms[i] * mag; if (step >= raw) break; }
  var nlo = Math.floor(lo / step + 1e-9) * step, nhi = Math.ceil(hi / step - 1e-9) * step;
  if (nhi <= nlo) nhi = nlo + step;
  return [nlo, nhi, step];
}
function tickList(lo, hi, step) {
  /* lo + k * step (not a running sum, which drifts) with a near-zero tick snapped to 0; never more than 64 */
  var out = [];
  for (var k = 0; k < 64; k++) { var v = lo + k * step; if (!(v <= hi + step / 2)) break; if (Math.abs(v) < Math.abs(step) * 1e-9) v = 0; out.push(v); }
  return out;
}
function trim(s) { return s.indexOf(".") >= 0 ? s.replace(/0+$/, "").replace(/\.$/, "") : s; }
function trimExp(s) { return s.replace(/\.?0+e/, "e"); }
function expOf(v) { return parseInt(Math.abs(v).toExponential().split("e")[1], 10); }
function withUnit(s, unit) {
  unit = str(unit);
  if (!unit) return s;
  if (/^[$€£¥]$/.test(unit)) return s.charAt(0) === "-" ? "-" + unit + s.slice(1) : unit + s;
  return s + (unit.length <= 2 || unit === "ms" ? "" : " ") + unit;
}
function fmtNum(v, unit) {
  /* T keeps 5 or more significant digits up to 1e18, so close values stay apart; past that, 3 in exponent form */
  var a = Math.abs(v), s;
  if (a >= 1e18) s = isFinite(v) ? trimExp(v.toExponential(2)) : String(v);
  else if (a >= 1e12) s = trim((v / 1e12).toFixed(1)) + "T"; else if (a >= 1e9) s = trim((v / 1e9).toFixed(1)) + "B";
  else if (a >= 1e6) s = trim((v / 1e6).toFixed(1)) + "M"; else if (a >= 1e4) s = trim((v / 1e3).toFixed(1)) + "k";
  else if (a >= 100 || v === Math.round(v)) s = String(Math.round(v)); else if (a >= 1) s = trim(v.toFixed(2));
  else if (a === 0) s = "0"; else s = a >= 0.001 ? trim(v.toFixed(3)) : v.toExponential(1);
  return withUnit(s, unit);
}
function decimalsOf(x) {
  /* digits after the point that write x (a tick step in display units) exactly */
  for (var d = 0; d < 12; d++) { var r = x * Math.pow(10, d); if (Math.abs(r - Math.round(r)) <= 1e-6 * Math.max(1, Math.abs(r))) return d; }
  return 12;
}
function axisFmt(lo, hi, step, unit) {
  /* every tick of one axis in one format (the scale of its largest tick) with the digits its step needs, so two
     ticks never read the same */
  var M = Math.max(Math.abs(lo), Math.abs(hi)), sc = 1, suf = "", exp = M >= 1e18 || (M > 0 && M < 1e-3);
  if (!(step > 0) || !isFinite(step) || !isFinite(M)) return function (v) { return fmtNum(v, unit); };
  if (!exp) { if (M >= 1e12) { sc = 1e12; suf = "T"; } else if (M >= 1e9) { sc = 1e9; suf = "B"; } else if (M >= 1e6) { sc = 1e6; suf = "M"; } else if (M >= 1e4) { sc = 1e3; suf = "k"; } }
  var es = expOf(step), sd = decimalsOf(step / Math.pow(10, es)), fd = Math.min(12, decimalsOf(step / sc));
  return function (v) {
    if (v === 0) return withUnit("0", unit);
    if (exp) return withUnit(trimExp(v.toExponential(clamp(expOf(v) - es + sd, 0, 20))), unit);
    return withUnit(trim((v / sc).toFixed(fd)) + suf, unit);
  };
}

/* ---------- charts ---------- */
function chartWidth() { var st = document.getElementById("stage"); return clamp((st ? st.clientWidth : 1000) - 140, 300, 1160); }
function legend(series, colors, unit) {
  var lg = el("div", "legend");
  series.forEach(function (s, i) {
    var it = el("span"), sw = el("i"); sw.style.background = colors[i % colors.length];
    var vals = s.values || [];
    add(it, sw, el("span", "", s.name || ("series " + (i + 1))), vals.length ? el("b", "", fmtNum(+vals[vals.length - 1], unit)) : null);
    lg.appendChild(it);
  });
  return lg;
}
function lineChart(sc) {
  var series = (sc.series || []).filter(function (s) { return s.values && s.values.length; });
  var box = el("div", "chart"), multi = series.length > 1, unit = sc.unit;
  if (multi || (series[0] && series[0].name)) box.appendChild(legend(series, multi ? SERIES : ["#36E2FF"], unit));
  var W = chartWidth(), H = Math.round(clamp(W * 0.42, 220, 430)), L = 56, R = 18, T = 14, B = 34;
  var all = []; series.forEach(function (s) { s.values.forEach(function (v) { all.push(+v); }); });
  var nt = niceTicks(Math.min.apply(null, all), Math.max.apply(null, all), 6), lo = nt[0], hi = nt[1], step = nt[2];
  var n = Math.max.apply(null, series.map(function (s) { return s.values.length; }));
  var pw = W - L - R, ph = H - T - B;
  function X(i) { return L + (n > 1 ? i * pw / (n - 1) : pw / 2); }
  function Y(v) { return T + (1 - (v - lo) / (hi - lo)) * ph; }
  var svg = sv("svg", {viewBox: "0 0 " + W + " " + H, role: "img"}), defs = sv("defs"), gid = "g" + (++uid), aid = "a" + uid;
  spectrumDefs(defs, gid, false);
  var ag = sv("linearGradient", {id: aid, x1: 0, y1: 0, x2: 0, y2: 1});
  ag.appendChild(sv("stop", {offset: "0%", "stop-color": "#9D4DFF", "stop-opacity": ".38"}));
  ag.appendChild(sv("stop", {offset: "100%", "stop-color": "#36E2FF", "stop-opacity": "0"}));
  defs.appendChild(ag);
  var fid = "f" + uid, f = sv("filter", {id: fid, x: "-10%", y: "-30%", width: "120%", height: "160%"});
  f.appendChild(sv("feGaussianBlur", {stdDeviation: 4, result: "b"}));
  var mg = sv("feMerge"); mg.appendChild(sv("feMergeNode", {"in": "b"})); mg.appendChild(sv("feMergeNode", {"in": "SourceGraphic"})); f.appendChild(mg);
  defs.appendChild(f); svg.appendChild(defs);
  var grid = sv("g", {"class": "grid"}), axis = sv("g", {"class": "axis"}), af = axisFmt(lo, hi, step, unit);
  tickList(lo, hi, step).forEach(function (v) {
    var y = Y(v); grid.appendChild(sv("line", {x1: L, x2: W - R, y1: y, y2: y}));
    axis.appendChild(sv("text", {x: L - 10, y: y + 4, "text-anchor": "end"}, af(v)));
  });
  add(svg, grid, axis, sv("line", {"class": "base", x1: L, x2: W - R, y1: T + ph, y2: T + ph}));
  series.forEach(function (s, si) {
    var d = "", pts = s.values.map(function (val, i) { return [X(i), Y(+val)]; });
    pts.forEach(function (p, i) { d += (i ? " L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1); });
    var color = multi ? SERIES[si % SERIES.length] : "url(#" + gid + ")";
    if (!multi && pts.length > 1) {
      svg.appendChild(sv("path", {"class": "area", d: d + " L" + pts[pts.length - 1][0].toFixed(1) + " " + (T + ph) + " L" + pts[0][0].toFixed(1) + " " + (T + ph) + " Z", fill: "url(#" + aid + ")"}));
    }
    svg.appendChild(sv("path", {"class": "draw", d: d, fill: "none", stroke: color, "stroke-width": 3, "stroke-linejoin": "round", "stroke-linecap": "round", pathLength: 1, filter: "url(#" + fid + ")"}));
    if (pts.length <= 60) pts.forEach(function (p, i) {
      var c = sv("circle", {"class": "pt", cx: p[0], cy: p[1], r: 3.4, fill: multi ? SERIES[si % SERIES.length] : grad(i / Math.max(1, pts.length - 1)), stroke: "#07060c", "stroke-width": 1.5});
      c.style.animationDelay = (300 + i * 1100 / pts.length) + "ms";
      c.appendChild(sv("title", null, (str((sc.labels || [])[i]) ? str(sc.labels[i]) + ": " : "") + fmtNum(+s.values[i], unit)));
      svg.appendChild(c);
    });
    var last = pts[pts.length - 1];
    svg.appendChild(sv("circle", {"class": "end", cx: last[0], cy: last[1], r: 6, fill: "none", stroke: multi ? SERIES[si % SERIES.length] : "#36E2FF", "stroke-width": 2}));
  });
  var labels = (sc.labels || []).slice(0, n), lastEnd = -1e9, minGap = 10;
  var xl = sv("g", {"class": "xl", "clip-path": clipBox(defs, "xc" + uid, -W, T + ph + 2, W * 3, B + 2)});
  labels.forEach(function (lab, i) {
    var t = str(lab), w = t.length * 7.4, x = X(i);
    if (x - w / 2 < lastEnd + minGap && i !== labels.length - 1) return;
    if (i === labels.length - 1 && x - w / 2 < lastEnd + minGap) return;
    xl.appendChild(sv("text", {x: x, y: H - 10, "text-anchor": i === 0 && n > 1 ? "start" : (i === n - 1 && n > 1 ? "end" : "middle")}, t));
    lastEnd = x + w / 2;
  });
  svg.appendChild(xl);
  box.appendChild(svg);
  return box;
}
function barChart(sc) {
  var series = (sc.series || []).filter(function (s) { return s.values && s.values.length; }), unit = sc.unit;
  var box = el("div", "chart"), multi = series.length > 1;
  if (multi) box.appendChild(legend(series, SERIES, unit));
  var n = Math.max.apply(null, series.map(function (s) { return s.values.length; })), ns = series.length;
  var labels = []; for (var i = 0; i < n; i++) labels.push(str((sc.labels || [])[i]) || String(i + 1));
  var longest = Math.max.apply(null, labels.map(function (l) { return l.length; }));
  var all = []; series.forEach(function (s) { s.values.forEach(function (v) { all.push(+v); }); });
  var lo = Math.min(0, Math.min.apply(null, all)), hi = Math.max(0, Math.max.apply(null, all));
  var W = chartWidth(), defs = sv("defs"), gid = "bg" + (++uid);
  var vertical = n * ns <= 24 && (longest * 7.2 <= (W - 80) / n || n <= 4);
  var colors = [];
  for (i = 0; i < n; i++) colors.push(grad(n > 1 ? i / (n - 1) : 0.4));
  function gradFor(c, idx, horiz) {
    var id = gid + "_" + idx, g = sv("linearGradient", horiz ? {id: id, x1: 0, y1: 0, x2: 1, y2: 0} : {id: id, x1: 0, y1: 1, x2: 0, y2: 0});
    g.appendChild(sv("stop", {offset: "0%", "stop-color": c, "stop-opacity": ".55"}));
    g.appendChild(sv("stop", {offset: "100%", "stop-color": c, "stop-opacity": "1"}));
    defs.appendChild(g); return "url(#" + id + ")";
  }
  var svg;
  if (vertical) {
    var H = Math.round(clamp(W * 0.42, 240, 430)), L = 56, R = 14, T = 22, B = 34, pw = W - L - R, ph = H - T - B;
    var nt = niceTicks(lo, hi, 6), a = nt[0], b = nt[1], step = nt[2];
    svg = sv("svg", {viewBox: "0 0 " + W + " " + H, role: "img"}); svg.appendChild(defs);
    function Y(v) { return T + (1 - (v - a) / (b - a)) * ph; }
    var grid = sv("g", {"class": "grid"}), axis = sv("g", {"class": "axis"}), af = axisFmt(a, b, step, unit);
    tickList(a, b, step).forEach(function (v) { grid.appendChild(sv("line", {x1: L, x2: W - R, y1: Y(v), y2: Y(v)})); axis.appendChild(sv("text", {x: L - 10, y: Y(v) + 4, "text-anchor": "end"}, af(v))); });
    add(svg, grid, axis);
    var slot = pw / n, bw = Math.min(64, (slot * 0.72) / ns), z = Y(0), xg = sv("g", {"clip-path": clipBox(defs, gid + "_x", -W, H - B + 2, W * 3, B + 2)});
    for (i = 0; i < n; i++) {
      for (var si = 0; si < ns; si++) {
        var val = +(series[si].values[i]); if (!isFinite(val)) continue;
        var x = L + slot * i + (slot - bw * ns) / 2 + si * bw, y = Y(val), top = Math.min(y, z), h = Math.max(1, Math.abs(z - y));
        var c = multi ? SERIES[si % SERIES.length] : colors[i];
        var r = sv("rect", {"class": "bar" + (val < 0 ? " neg" : ""), x: x + 1, y: top, width: Math.max(2, bw - 2), height: h, rx: Math.min(7, bw / 3), fill: gradFor(c, i + "_" + si, false)});
        r.style.animationDelay = (i * 45 + si * 20) + "ms";
        r.appendChild(sv("title", null, labels[i] + ": " + fmtNum(val, unit)));
        svg.appendChild(r);
        if (!multi || ns <= 2) svg.appendChild(sv("text", {"class": "vl pt", x: x + bw / 2, y: val >= 0 ? top - 7 : top + h + 15, "text-anchor": "middle", style: "animation-delay:" + (500 + i * 45) + "ms"}, fmtNum(val, unit)));
      }
      var lab = labels[i], maxc = Math.max(3, Math.floor(slot / 7.4));
      if (lab.length > maxc) lab = lab.slice(0, maxc - 1) + "…";
      xg.appendChild(sv("text", {"class": "axis", x: L + slot * i + slot / 2, y: H - 10, "text-anchor": "middle", fill: "#9692ac", style: "font:500 12px var(--mono)"}, lab));
    }
    svg.appendChild(xg);
    svg.appendChild(sv("line", {"class": "base", x1: L, x2: W - R, y1: z, y2: z}));
  } else {
    var rowH = ns > 1 ? 18 : 26, gap = ns > 1 ? 14 : 8, LW = Math.min(240, Math.max(60, longest * 7.6 + 10)), VW = 70;
    var Hh = n * (rowH * ns + gap) + 10, pw2 = W - LW - VW - 12;
    svg = sv("svg", {viewBox: "0 0 " + W + " " + Hh, role: "img"}); svg.appendChild(defs);
    var span = (hi - lo) || 1, zx = LW + 6 + (0 - lo) / span * pw2;
    for (i = 0; i < n; i++) {
      var y0 = 5 + i * (rowH * ns + gap);
      var labt = labels[i], maxc2 = Math.floor((LW - 12) / 7.4);
      if (labt.length > maxc2) labt = labt.slice(0, maxc2 - 1) + "…";
      svg.appendChild(sv("text", {x: LW - 6, y: y0 + (rowH * ns) / 2 + 4, "text-anchor": "end", fill: "#d6dae9", style: "font:600 13px var(--sans)",
                                  "clip-path": clipBox(defs, gid + "_l" + i, 0, y0 - gap / 2, LW, rowH * ns + gap)}, labt));
      for (si = 0; si < ns; si++) {
        val = +(series[si].values[i]); if (!isFinite(val)) continue;
        var len = Math.abs(val) / span * pw2, xx = val >= 0 ? zx : zx - len, yy = y0 + si * rowH;
        var c2 = multi ? SERIES[si % SERIES.length] : grad(clamp((xx + len - LW) / pw2, 0, 1));
        var hb = sv("rect", {"class": "hbar" + (val < 0 ? " neg" : ""), x: xx, y: yy + 2, width: Math.max(2, len), height: rowH - 5, rx: 6, fill: gradFor(c2, i + "_" + si, true)});
        hb.style.animationDelay = (i * 40) + "ms";
        hb.appendChild(sv("title", null, labels[i] + ": " + fmtNum(val, unit)));
        svg.appendChild(hb);
        svg.appendChild(sv("text", {"class": "vl pt", x: val >= 0 ? xx + len + 8 : zx + 8, y: yy + rowH / 2 + 3, style: "animation-delay:" + (450 + i * 40) + "ms"}, fmtNum(val, unit)));
      }
    }
    svg.appendChild(sv("line", {"class": "base", x1: zx, x2: zx, y1: 0, y2: Hh}));
  }
  box.appendChild(svg);
  return box;
}
function sparkSvg(values, h, id) {
  var vals = (values || []).map(Number).filter(isFinite), W = 300;
  var svg = sv("svg", {viewBox: "0 0 " + W + " " + h, preserveAspectRatio: "none"});
  if (vals.length < 2) return svg;
  var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals), span = hi - lo;
  if (!isFinite(span) || !(span > 0)) span = isFinite(span) ? 1 : hi / 2 - lo / 2;
  if (!isFinite(span) || !(span > 0)) return svg;
  var defs = sv("defs"); spectrumDefs(defs, id, false);
  var ag = sv("linearGradient", {id: id + "a", x1: 0, y1: 0, x2: 0, y2: 1});
  ag.appendChild(sv("stop", {offset: "0%", "stop-color": "#FF2E88", "stop-opacity": ".35"}));
  ag.appendChild(sv("stop", {offset: "100%", "stop-color": "#9D4DFF", "stop-opacity": "0"}));
  defs.appendChild(ag); svg.appendChild(defs);
  var d = "";
  vals.forEach(function (v, i) { d += (i ? " L" : "M") + (i * W / (vals.length - 1)).toFixed(1) + " " + (h - 3 - (v - lo) / span * (h - 8)).toFixed(1); });
  svg.appendChild(sv("path", {"class": "area", d: d + " L" + W + " " + h + " L0 " + h + " Z", fill: "url(#" + id + "a)"}));
  svg.appendChild(sv("path", {"class": "draw", d: d, fill: "none", stroke: "url(#" + id + ")", "stroke-width": 2.4, pathLength: 1, "vector-effect": "non-scaling-stroke"}));
  return svg;
}
function sparkChart(sc) {
  var box = el("div", "chart sparks");
  (sc.series || []).forEach(function (s, i) {
    var vals = (s.values || []).map(Number); if (!vals.length) return;
    var row = el("div"), head = el("div", "spark-h");
    add(head, el("span", "nm", s.name || ("series " + (i + 1))), el("span", "mm", "min " + fmtNum(Math.min.apply(null, vals), sc.unit) + "  max " + fmtNum(Math.max.apply(null, vals), sc.unit)), el("span", "lv", fmtNum(vals[vals.length - 1], sc.unit)));
    var svg = sparkSvg(vals, 70, "sp" + (++uid)); svg.style.width = "100%"; svg.style.height = "70px"; svg.style.display = "block";
    add(row, head, svg); box.appendChild(row);
  });
  return box;
}
function chart(sc) { return sc.chart === "line" ? lineChart(sc) : sc.chart === "spark" ? sparkChart(sc) : barChart(sc); }

/* ---------- diagram: layered layout (longest-path ranks, barycenter order) ---------- */
function layered(nodes, edges) {
  var ids = nodes.map(function (n) { return n.id; }), known = dict(), succ = dict();
  ids.forEach(function (i, k) { known[i] = k; succ[i] = []; });
  edges.forEach(function (e, k) { if (e.from in known && e.to in known && e.from !== e.to) succ[e.from].push([e.to, k]); });
  var state = dict(), back = dict();
  ids.forEach(function (root) {
    if (state[root]) return; state[root] = 1; var stack = [[root, 0]];
    while (stack.length) {
      var top = stack[stack.length - 1], list = succ[top[0]];
      if (top[1] >= list.length) { state[top[0]] = 2; stack.pop(); continue; }
      var nx = list[top[1]++], st = state[nx[0]] || 0;
      if (st === 1) back[nx[1]] = true; else if (st === 0) { state[nx[0]] = 1; stack.push([nx[0], 0]); }
    }
  });
  var dag = [], seen = dict();
  edges.forEach(function (e, k) {
    if (!(e.from in known) || !(e.to in known) || e.from === e.to) return;
    var u = back[k] ? e.to : e.from, v = back[k] ? e.from : e.to, key = u + "\u0000" + v;
    if (seen[key]) return; seen[key] = 1; dag.push({u: u, v: v, k: k, rev: !!back[k]});
  });
  var indeg = dict(), out = dict(), rank = dict();
  ids.forEach(function (i) { indeg[i] = 0; out[i] = []; rank[i] = 0; });
  dag.forEach(function (d) { out[d.u].push(d.v); indeg[d.v]++; });
  var q = ids.filter(function (i) { return indeg[i] === 0; });
  for (var qi = 0; qi < q.length; qi++) out[q[qi]].forEach(function (m) { rank[m] = Math.max(rank[m], rank[q[qi]] + 1); if (--indeg[m] === 0) q.push(m); });
  var nl = 1; ids.forEach(function (i) { nl = Math.max(nl, rank[i] + 1); });
  var layers = []; for (var r = 0; r < nl; r++) layers.push([]);
  var rankOf = dict(), segs = [], chains = [];
  ids.forEach(function (i) { rankOf[i] = rank[i]; layers[rank[i]].push(i); });
  dag.forEach(function (d) {
    var chain = [d.u];
    for (var rr = rank[d.u] + 1; rr < rank[d.v]; rr++) { var dm = "\u0001" + d.k + ":" + rr; rankOf[dm] = rr; layers[rr].push(dm); chain.push(dm); }
    chain.push(d.v); chains.push({d: d, chain: chain});
    for (var c = 0; c + 1 < chain.length; c++) segs.push([chain[c], chain[c + 1]]);
  });
  var preds = dict(), succs = dict();
  segs.forEach(function (s) { (succs[s[0]] = succs[s[0]] || []).push(s[1]); (preds[s[1]] = preds[s[1]] || []).push(s[0]); });
  function crossings(ly) {
    var total = 0;
    for (var r2 = 0; r2 + 1 < ly.length; r2++) {
      var pa = dict(), pb = dict(); ly[r2].forEach(function (z, i) { pa[z] = i; }); ly[r2 + 1].forEach(function (z, i) { pb[z] = i; });
      var pairs = segs.filter(function (s) { return s[0] in pa && s[1] in pb; }).map(function (s) { return [pa[s[0]], pb[s[1]]]; });
      for (var i = 0; i < pairs.length; i++) for (var j = i + 1; j < pairs.length; j++) if ((pairs[i][0] - pairs[j][0]) * (pairs[i][1] - pairs[j][1]) < 0) total++;
    }
    return total;
  }
  var best = layers.map(function (l) { return l.slice(); }), bestC = crossings(best), cur = layers.map(function (l) { return l.slice(); });
  for (var it = 0; it < 8 && bestC > 0; it++) {
    var down = it % 2 === 0, order = [];
    for (r = 0; r < cur.length; r++) order.push(r);
    if (!down) order.reverse();
    order.forEach(function (r3) {
      var ref = down ? cur[r3 - 1] : cur[r3 + 1]; if (!ref) return;
      var pos = dict(); ref.forEach(function (z, i) { pos[z] = i; });
      var nb = down ? preds : succs, base = cur[r3].slice();
      cur[r3].sort(function (x, y) {
        function bc(z) { var ps = (nb[z] || []).filter(function (p) { return p in pos; }).map(function (p) { return pos[p]; }); return ps.length ? ps.reduce(function (a, b) { return a + b; }, 0) / ps.length : base.indexOf(z); }
        return bc(x) - bc(y);
      });
    });
    var c2 = crossings(cur);
    if (c2 < bestC) { bestC = c2; best = cur.map(function (l) { return l.slice(); }); }
  }
  return {layers: best, segs: segs, chains: chains, rankOf: rankOf, nl: nl};
}
function wrapLabel(s, max) {
  /* words wrap at spaces; a word longer than a line (a path, a URL) is cut into line-sized pieces */
  var words2 = [], lines = [], cur = "";
  str(s).split(/\s+/).forEach(function (w) {
    var cps = Array.from ? Array.from(w) : w.split("");
    if (cps.length <= max) { if (w) words2.push(w); return; }
    for (var i = 0; i < cps.length; i += max) words2.push(cps.slice(i, i + max).join(""));
  });
  words2.forEach(function (w) { if (cur && (cur + " " + w).length > max) { lines.push(cur); cur = w; } else cur = cur ? cur + " " + w : w; });
  if (cur) lines.push(cur);
  if (lines.length > 3) { lines = lines.slice(0, 3); lines[2] = lines[2].slice(0, max - 1) + "…"; }
  return lines.length ? lines : [""];
}
function fitText(s, font, maxW) {
  /* s, shortened with an ellipsis until it measures at most maxW */
  s = str(s);
  if (textW(s, font) <= maxW) return s;
  var cps = Array.from ? Array.from(s) : s.split(""), lo = 0, hi = cps.length;
  while (lo < hi) { var mid = (lo + hi + 1) >> 1; if (textW(cps.slice(0, mid).join("") + "…", font) <= maxW) lo = mid; else hi = mid - 1; }
  return cps.slice(0, lo).join("").replace(/\s+$/, "") + "…";
}
function diagram(sc) {
  var nodes = sc.nodes || [], edges = sc.edges || [], box = el("div", "chart diagram");
  if (!nodes.length) return box;
  var G = layered(nodes, edges), info = dict(), FONT = "650 14px " + getComputedStyle(document.body).fontFamily;
  var MONO = getComputedStyle(document.documentElement).getPropertyValue("--mono").trim() || "monospace";
  var LFONT = "600 12px " + MONO, NODE_MAX = 260, LABEL_MAX = 320;
  nodes.forEach(function (n) { info[n.id] = n; });
  /* edge labels are cut to LABEL_MAX, and the gap between LR columns is as wide as the widest one, so a label
     sits between two columns and never covers a node */
  var elab = {}, maxLw = 0;
  edges.forEach(function (e, k) {
    if (!e || !e.label) return;
    var t = fitText(e.label, LFONT, LABEL_MAX - 16);
    elab[k] = [t, textW(t, LFONT) + 16]; maxLw = Math.max(maxLw, elab[k][1]);
  });
  /* an edge that skips ranks carries its label on its middle dummy; top to bottom, that dummy is as wide as the
     label, so the label gets its own slot in the rank instead of covering the node beside it */
  var host = {}, inGap = [];
  G.chains.forEach(function (c) {
    var dm = c.chain.slice(1, -1);
    if (!elab[c.d.k]) return;
    if (dm.length % 2 === 1) host[dm[(dm.length - 1) / 2]] = Math.ceil(elab[c.d.k][1]) + 12;
    else { var r0 = G.rankOf[c.chain[dm.length / 2]]; inGap[r0] = (inGap[r0] || 0) + 1; }   /* sits between two ranks */
  });
  var avail = chartWidth(), dir = sc.direction === "TB" ? "TB" : "LR";
  function geom(dir2) {
    var size = dict(), LR = dir2 === "LR";
    Object.keys(G.rankOf).forEach(function (z) {
      if (z.charAt(0) === "\u0001") { size[z] = [host[z] && !LR ? host[z] : 2, 2, []]; return; }
      var lines = wrapLabel(info[z].label || z, 22).map(function (l) { return fitText(l, FONT, NODE_MAX); }), w = 0;
      lines.forEach(function (l) { w = Math.max(w, textW(l, FONT)); });
      size[z] = [Math.ceil(w) + 36, lines.length * 19 + 24, lines];
    });
    var pos = dict(), W = 0, H = 0, i, r;
    var labelGap = maxLw ? Math.max(70, Math.ceil(maxLw) + 24 - 74) : 0;
    if (LR) {
      var colw = G.layers.map(function (ly) { return Math.max.apply(null, ly.map(function (z) { return size[z][0]; }).concat([2])); });
      var gap = 74 + labelGap, x = 0, colx = [];
      for (r = 0; r < G.nl; r++) { colx.push(x); x += colw[r] + gap; }
      W = x - gap;
      var stacks = G.layers.map(function (ly) { return ly.reduce(function (a, z) { return a + size[z][1]; }, 0) + 26 * Math.max(0, ly.length - 1); });
      H = Math.max.apply(null, stacks.concat([40]));
      var ys = dict();
      G.layers.forEach(function (ly, r4) { var y = (H - stacks[r4]) / 2; ly.forEach(function (z) { ys[z] = y; y += size[z][1] + 26; }); });
      for (var sw = 0; sw < 4; sw++) {
        var seq = []; for (r = 0; r < G.nl; r++) seq.push(r); if (sw % 2) seq.reverse();
        seq.forEach(function (r5) {
          var ly = G.layers[r5], want = ly.map(function (z) {
            var cs = G.segs.filter(function (s) { return s[0] === z || s[1] === z; }).map(function (s) { var o = s[0] === z ? s[1] : s[0]; return G.rankOf[o] !== r5 ? ys[o] + size[o][1] / 2 : null; }).filter(function (v) { return v !== null; });
            return (cs.length ? cs.reduce(function (a, b) { return a + b; }, 0) / cs.length : ys[z] + size[z][1] / 2) - size[z][1] / 2;
          });
          var prev = -26, placed = [];
          ly.forEach(function (z, k) { var y = Math.max(want[k], prev + 26, 0); placed.push(y); prev = y + size[z][1]; });
          var lim = H; for (k = ly.length - 1; k >= 0; k--) { if (placed[k] + size[ly[k]][1] > lim) placed[k] = lim - size[ly[k]][1]; lim = placed[k] - 26; }
          ly.forEach(function (z, k2) { ys[z] = Math.max(0, placed[k2]); });
        });
      }
      var k;
      G.layers.forEach(function (ly, r6) { ly.forEach(function (z) { pos[z] = [colx[r6] + (colw[r6] - size[z][0]) / 2, ys[z]]; }); });
    } else {
      var rowh = G.layers.map(function (ly) { return Math.max.apply(null, ly.map(function (z) { return size[z][1]; }).concat([2])); });
      /* top to bottom, a label sits in the gap between two ranks: that gap holds one row of labels, or two when
         several labels share it (they slide apart along their edges) */
      var y2 = 0, rowy = [], gapy = 0;
      for (r = 0; r < G.nl; r++) { gapy = 64 + (inGap[r] ? (inGap[r] > 1 ? 44 : 26) : 0); rowy.push(y2); y2 += rowh[r] + gapy; }
      H = y2 - gapy;
      var widths = G.layers.map(function (ly) { return ly.reduce(function (a, z) { return a + size[z][0]; }, 0) + 30 * Math.max(0, ly.length - 1); });
      W = Math.max.apply(null, widths.concat([60]));
      var xs = dict();
      G.layers.forEach(function (ly, r7) { var xq = (W - widths[r7]) / 2; ly.forEach(function (z) { xs[z] = xq; xq += size[z][0] + 30; }); });
      for (var sw2 = 0; sw2 < 4; sw2++) {
        var seq2 = []; for (r = 0; r < G.nl; r++) seq2.push(r); if (sw2 % 2) seq2.reverse();
        seq2.forEach(function (r8) {
          var ly = G.layers[r8], want = ly.map(function (z) {
            var cs = G.segs.filter(function (s) { return s[0] === z || s[1] === z; }).map(function (s) { var o = s[0] === z ? s[1] : s[0]; return G.rankOf[o] !== r8 ? xs[o] + size[o][0] / 2 : null; }).filter(function (v) { return v !== null; });
            return (cs.length ? cs.reduce(function (a, b) { return a + b; }, 0) / cs.length : xs[z] + size[z][0] / 2) - size[z][0] / 2;
          });
          var prev = -30, placed = [];
          ly.forEach(function (z, q2) { var xq = Math.max(want[q2], prev + 30, 0); placed.push(xq); prev = xq + size[z][0]; });
          var lim = W; for (var q3 = ly.length - 1; q3 >= 0; q3--) { if (placed[q3] + size[ly[q3]][0] > lim) placed[q3] = lim - size[ly[q3]][0]; lim = placed[q3] - 30; }
          ly.forEach(function (z, q4) { xs[z] = Math.max(0, placed[q4]); });
        });
      }
      G.layers.forEach(function (ly, r9) { ly.forEach(function (z) { pos[z] = [xs[z], rowy[r9] + (rowh[r9] - size[z][1]) / 2]; }); });
    }
    return {pos: pos, size: size, W: W, H: H, LR: LR};
  }
  var g1 = geom(dir), maxH = Math.max(260, window.innerHeight * 0.62);
  var s1 = Math.min(1, avail / g1.W, maxH / g1.H);
  var g2 = geom(dir === "LR" ? "TB" : "LR"), s2 = Math.min(1, avail / g2.W, maxH / g2.H);
  var g = s2 > s1 * 1.35 ? g2 : g1;
  var pad = 24, VW = g.W + pad * 2, VH = g.H + pad * 2;
  var svg = sv("svg", {viewBox: (-pad) + " " + (-pad) + " " + VW + " " + VH, role: "img"});
  svg.style.maxWidth = Math.ceil(VW * 1.35) + "px"; svg.style.margin = "0 auto";
  var defs = sv("defs"), mk = "m" + (++uid), gid = "dg" + uid;
  spectrumDefs(defs, gid, false);
  var marker = sv("marker", {id: mk, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse"});
  marker.appendChild(sv("path", {d: "M0 0 L10 5 L0 10 z", fill: "#ECF6FF"}));
  defs.appendChild(marker); svg.appendChild(defs);
  function color(z) { var t = info[z] && TONES[info[z].tone]; return t || grad(G.nl > 1 ? G.rankOf[z] / (G.nl - 1) * 0.9 + 0.05 : 0.5); }
  function port(z, out) {
    var p = g.pos[z], s = g.size[z];
    if (g.LR) return out ? [p[0] + s[0], p[1] + s[1] / 2] : [p[0], p[1] + s[1] / 2];
    return out ? [p[0] + s[0] / 2, p[1] + s[1]] : [p[0] + s[0] / 2, p[1]];
  }
  var edgeLayer = sv("g"), flowLayer = sv("g"), labelLayer = sv("g"), nodeLayer = sv("g"), nclips = 0;
  var bb = [0, 0, g.W, g.H];   /* drawing bounds: nodes, then every edge label (a wide one may pass the nodes) */
  /* An edge label never covers a node or another label, whatever the direction: it starts at the middle of its
     edge, slides along the edge to the nearest free spot, and only then steps aside from it. */
  var taken = [];
  Object.keys(g.pos).forEach(function (z) {
    if (z.charAt(0) === "\u0001") return;
    var p = g.pos[z], s = g.size[z]; taken.push([p[0], p[1], p[0] + s[0], p[1] + s[1]]);
  });
  function bez(a, b, t) {
    var c1, c2, u = 1 - t;
    if (g.LR) { var mx = (a[0] + b[0]) / 2; c1 = [mx, a[1]]; c2 = [mx, b[1]]; } else { var my = (a[1] + b[1]) / 2; c1 = [a[0], my]; c2 = [b[0], my]; }
    return [u * u * u * a[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t * t * t * b[0],
            u * u * u * a[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t * t * t * b[1]];
  }
  function placeLabel(pts, p0, lw) {
    var cands = [[p0[0], p0[1], 0]], k, j;
    for (k = 0; k + 1 < pts.length; k++) for (j = 1; j < 20; j++) { var q = bez(pts[k], pts[k + 1], j / 20); cands.push([q[0], q[1], 0]); }
    var on = cands.length, dx = g.LR ? 0 : lw / 2 + 14, dy = g.LR ? 34 : 0;
    for (k = 0; k < on; k += 2) for (j = 1; j <= 3; j++) {
      cands.push([cands[k][0] + dx * j, cands[k][1] + dy * j, 40 * j]); cands.push([cands[k][0] - dx * j, cands[k][1] - dy * j, 40 * j]);
    }
    var best = null, bestCost = Infinity;
    cands.forEach(function (c) {
      var r = [c[0] - lw / 2 - 4, c[1] - 15, c[0] + lw / 2 + 4, c[1] + 15], hit = 0;
      taken.forEach(function (t) {
        var ix = Math.min(r[2], t[2]) - Math.max(r[0], t[0]), iy = Math.min(r[3], t[3]) - Math.max(r[1], t[1]);
        if (ix > 0 && iy > 0) hit += ix * iy;
      });
      var cost = hit * 1000 + Math.sqrt((c[0] - p0[0]) * (c[0] - p0[0]) + (c[1] - p0[1]) * (c[1] - p0[1])) + c[2];
      if (cost < bestCost) { bestCost = cost; best = c; }
    });
    taken.push([best[0] - lw / 2, best[1] - 11, best[0] + lw / 2, best[1] + 11]);
    return best;
  }
  G.chains.forEach(function (c) {
    var pts = [], ch = c.chain;
    for (var i = 0; i < ch.length; i++) {
      if (i > 0) pts.push(port(ch[i], false));
      if (i < ch.length - 1) pts.push(port(ch[i], true));
    }
    var d = "M" + pts[0][0].toFixed(1) + " " + pts[0][1].toFixed(1);
    for (i = 1; i < pts.length; i++) {
      var a = pts[i - 1], b = pts[i];
      if (g.LR) { var mx = (a[0] + b[0]) / 2; d += " C" + mx.toFixed(1) + " " + a[1].toFixed(1) + " " + mx.toFixed(1) + " " + b[1].toFixed(1) + " " + b[0].toFixed(1) + " " + b[1].toFixed(1); }
      else { var my = (a[1] + b[1]) / 2; d += " C" + a[0].toFixed(1) + " " + my.toFixed(1) + " " + b[0].toFixed(1) + " " + my.toFixed(1) + " " + b[0].toFixed(1) + " " + b[1].toFixed(1); }
    }
    var e = edges[c.d.k], stroke = color(c.d.u);
    var attrs = {"class": "edge draw", d: d, stroke: stroke, pathLength: 1};
    if (c.d.rev) attrs["marker-start"] = "url(#" + mk + ")"; else attrs["marker-end"] = "url(#" + mk + ")";
    var path = sv("path", attrs);
    path.style.animationDelay = (G.rankOf[c.d.u] * 140 + 160) + "ms";
    edgeLayer.appendChild(path);
    var fl = sv("path", {"class": "flow", d: d, pathLength: 1}); fl.style.animationDelay = (Math.random() * -2.4).toFixed(2) + "s";
    flowLayer.appendChild(fl);
    if (e && e.label && elab[c.d.k]) {
      var mid = pts[Math.floor(pts.length / 2) - (pts.length % 2 === 0 ? 1 : 0)], mid2 = pts[Math.min(pts.length - 1, Math.floor(pts.length / 2))];
      var lw = elab[c.d.k][1], at = placeLabel(pts, [(mid[0] + mid2[0]) / 2, (mid[1] + mid2[1]) / 2], lw), lx = at[0], ly = at[1];
      bb = [Math.min(bb[0], lx - lw / 2), Math.min(bb[1], ly - 11), Math.max(bb[2], lx + lw / 2), Math.max(bb[3], ly + 11)];
      var lg = sv("g", {"class": "elabel pt"}); lg.style.animationDelay = (G.rankOf[c.d.u] * 140 + 700) + "ms";
      var lcid = "lc" + uid + "_" + c.d.k, lclip = sv("clipPath", {id: lcid});
      lclip.appendChild(sv("rect", {x: lx - lw / 2, y: ly - 11, width: lw, height: 22})); defs.appendChild(lclip);
      var ltxt = sv("text", {x: lx, y: ly + 4, "text-anchor": "middle", "clip-path": "url(#" + lcid + ")"}, elab[c.d.k][0]);
      add(lg, sv("rect", {x: lx - lw / 2, y: ly - 11, width: lw, height: 22, rx: 11}), ltxt, sv("title", null, str(e.label)));
      labelLayer.appendChild(lg);
    }
  });
  Object.keys(g.pos).forEach(function (z) {
    if (z.charAt(0) === "\u0001") return;
    var p = g.pos[z], s = g.size[z], c = color(z), ng = sv("g", {"class": "node"});
    ng.style.animationDelay = (G.rankOf[z] * 140) + "ms";
    add(ng, sv("rect", {x: p[0], y: p[1], width: s[0], height: s[1], rx: 12, stroke: c, "stroke-width": 1.6, style: "filter:drop-shadow(0 0 10px " + c + "55)"}));
    /* a label never paints outside its node */
    var ncid = "nc" + uid + "_" + (++nclips), nclip = sv("clipPath", {id: ncid}), tg = sv("g", {"clip-path": "url(#" + ncid + ")"});
    nclip.appendChild(sv("rect", {x: p[0] + 2, y: p[1] + 2, width: Math.max(0, s[0] - 4), height: Math.max(0, s[1] - 4)})); defs.appendChild(nclip);
    s[2].forEach(function (ln, i) { tg.appendChild(sv("text", {x: p[0] + s[0] / 2, y: p[1] + 12 + 15 + i * 19, "text-anchor": "middle"}, ln)); });
    ng.appendChild(tg);
    ng.appendChild(sv("title", null, str(info[z].label || z)));
    nodeLayer.appendChild(ng);
  });
  add(svg, edgeLayer, RM ? null : flowLayer, nodeLayer, labelLayer);
  if (bb[0] < 0 || bb[1] < 0 || bb[2] > g.W || bb[3] > g.H) {
    VW = bb[2] - bb[0] + pad * 2; VH = bb[3] - bb[1] + pad * 2;
    svg.setAttribute("viewBox", (bb[0] - pad) + " " + (bb[1] - pad) + " " + VW + " " + VH);
    svg.style.maxWidth = Math.ceil(VW * 1.35) + "px";
  }
  box.appendChild(svg);
  return box;
}

/* ---------- other kinds ---------- */
function table(sc) {
  var cols = (sc.columns || []).map(str), rows = (sc.rows || []).map(function (r) { return (r || []).map(str); });
  var ncol = Math.max(cols.length, rows.reduce(function (a, r) { return Math.max(a, r.length); }, 0));
  var num = [];
  for (var c = 0; c < ncol; c++) {
    var vals = rows.map(function (r) { return (r[c] || "").trim(); }).filter(Boolean);
    num.push(vals.length > 0 && vals.every(function (v) { return /^[\s$€£¥+\-−~≈<>]*[\d,]*\.?\d+(?:[eE][+-]?\d+)?\s*(%|[kKmMbBtT]|ms|s|x|×|GB|MB|KB|TB|tok\/s)?\s*$/.test(v); }));
  }
  var wrap = el("div", "tablewrap"), t = el("table");
  if (cols.some(function (x) { return x.trim(); })) {
    var thead = el("thead"), tr = el("tr");
    for (c = 0; c < ncol; c++) tr.appendChild(el("th", num[c] ? "num" : "", cols[c] || ""));
    thead.appendChild(tr); t.appendChild(thead);
  }
  var tb = el("tbody"), GOOD = /^(ok|pass|passed|done|yes|true|up|healthy|success|green|online|ready|✓)$/i, BADR = /^(fail|failed|error|no|false|down|red|offline|broken|critical|✗)$/i, WARNR = /^(warn|warning|pending|partial|degraded|slow|yellow|flaky|beta)$/i;
  rows.forEach(function (r, i) {
    var row = el("tr"); row.style.animationDelay = Math.min(900, i * 28) + "ms";
    for (var c2 = 0; c2 < ncol; c2++) {
      var v = (r[c2] || ""), tv = v.trim(), cls = [];
      if (num[c2]) { cls.push("num"); if (/^\+/.test(tv)) cls.push("pos"); else if (/^[-−]/.test(tv)) cls.push("neg"); }
      else if (c2 === 0) cls.push("first");
      if (GOOD.test(tv)) cls.push("good"); else if (BADR.test(tv)) cls.push("badv"); else if (WARNR.test(tv)) cls.push("warnv");
      row.appendChild(el("td", cls.join(" "), v));
    }
    tb.appendChild(row);
  });
  t.appendChild(tb); wrap.appendChild(t);
  return wrap;
}
function metrics(sc) {
  var grid = el("div", "metrics");
  (sc.metrics || []).forEach(function (m, i) {
    var tile = el("div", "tile"), lb = el("div", "lb"), n = (sc.metrics || []).length;
    tile.style.setProperty("--c", "linear-gradient(90deg," + grad(i / Math.max(1, n - 1) * 0.9) + ",transparent)");
    tile.style.animationDelay = (i * 70) + "ms";
    lb.appendChild(el("span", "", m.label || ""));
    var d = str(m.delta).trim();
    if (d) {
      var up = /^[+▲↑]/.test(d), down = /^[-▼↓−]/.test(d);
      lb.appendChild(el("span", "dl " + (up ? "up" : down ? "down" : "flat"), (up ? "▲ " : down ? "▼ " : "") + d.replace(/^[+\-▲▼↑↓−]\s*/, "")));
    }
    add(tile, lb, bigValue("val", m.value || "", 120 + i * 70));
    if (m.trend && m.trend.length > 1) tile.appendChild(sparkSvg(m.trend, 46, "mt" + (++uid)));
    grid.appendChild(tile);
  });
  return grid;
}
var STATUS_COLOR = {done: "#50FAAA", active: "#36E2FF", failed: "#FF465A", pending: "#58546c"};
function timeline(sc) {
  var ol = el("ol", "timeline"), evs = sc.events || [];
  evs.forEach(function (e, i) {
    var st = STATUS_COLOR[e.status] ? e.status : "", li = el("li", st ? "st-" + st : "");
    li.style.setProperty("--c", st ? STATUS_COLOR[st] : grad(i / Math.max(1, evs.length - 1) * 0.9));
    li.style.animationDelay = (i * 80) + "ms";
    var body = el("div");
    add(body, el("div", "tt", e.title || ""), e.detail ? el("div", "dd", e.detail) : null);
    add(li, el("div", "when", e.when || ""), add(el("div", "dot"), el("i")), body);
    ol.appendChild(li);
  });
  return ol;
}
function progress(sc) {
  var steps = sc.steps || [], done = 0, failed = 0, active = 0;
  steps.forEach(function (s) { if (s.status === "done") done++; else if (s.status === "failed") failed++; else if (s.status === "active") active++; });
  var box = el("div"), head = el("div", "prog-h"), bar = el("div", "prog-bar"), fill = el("i");
  var pct = steps.length ? Math.round(100 * done / steps.length) : 0;
  fill.style.width = steps.length ? (100 * (done + 0.5 * active) / steps.length) + "%" : "0%";
  bar.appendChild(fill);
  var n = el("div", "prog-n", done + "/" + steps.length); n.appendChild(el("span", "", pct + "%"));
  if (failed) n.appendChild(el("em", "", failed + " failed"));
  add(head, bar, n);
  var ul = el("ul", "steps");
  steps.forEach(function (s, i) {
    var st = STATUS_COLOR[s.status] ? s.status : "pending", li = el("li", st);
    li.style.animationDelay = (i * 60) + "ms";
    add(li, el("span", "ic", st === "done" ? "✓" : st === "failed" ? "✗" : ""), el("span", "t", s.title || ""), s.detail ? el("span", "d", s.detail) : el("span"));
    ul.appendChild(li);
  });
  add(box, head, ul);
  return box;
}
function card(sc) {
  var tone = TONES[sc.tone] ? sc.tone : "info", box = el("div", "callout"), tag = el("div", "tag"), t = TONE_TAG[tone];
  box.style.setProperty("--c", TONES[tone]);
  add(tag, el("i", "", t[0]), el("span", "", t[1]));
  add(box, tag, md(sc.body || "", true));
  return box;
}
function kindBody(sc) {
  switch (sc.kind) {
    case "card": return card(sc);
    case "markdown": var m = md(sc.body || "", true); m.style.maxWidth = "78ch"; return m;
    case "code": return codePanel(sc.code || {});
    case "table": return table(sc);
    case "chart": return chart(sc);
    case "diagram": return diagram(sc);
    case "metrics": return metrics(sc);
    case "timeline": return timeline(sc);
    case "progress": return progress(sc);
  }
  return el("div", "md", "Unsupported scene kind");
}

/* ---------- slides ---------- */
function titleBlock(text, sub, cls) {
  var f = document.createDocumentFragment();
  if (text) { var h = el("h1", "h-title " + (cls || "")); decrypt(h, text, 60); f.appendChild(h); }
  if (sub) { var s = el("p", "h-sub"); decrypt(s, sub, 260); f.appendChild(s); }
  if (text || sub) f.appendChild(el("div", "rule"));
  return f;
}
function sideOf(sl) {
  if (sl.code) return codePanel(sl.code);
  if (sl.embed) { var wrap = el("div", "embed"); if (sl.embed.title) wrap.appendChild(el("div", "kicker", sl.embed.title)); wrap.appendChild(kindBody(sl.embed)); return wrap; }
  if (sl.stat) { var s = el("div", "l-stat"); add(s, bigValue("stat-v", sl.stat.value, 200), sl.stat.label ? el("div", "stat-l", sl.stat.label) : null); s.style.textAlign = "center"; s.style.margin = "auto"; return s; }
  return null;
}
function slideView(sc, i) {
  var slides = sc.slides || [], sl = slides[i] || {}, layout = sl.layout || "auto";
  var frame = el("section", "frame slide l-" + layout);
  frame.setAttribute("aria-roledescription", "slide");
  if (layout === "title") {
    frame.appendChild(el("div", "halo"));
    frame.appendChild(titleBlock(sl.title, sl.subtitle));
    if (sl.body) frame.appendChild(md(sl.body, true));
  } else if (layout === "quote") {
    var qt = el("div", "qmark", "“"), body = el("div", "qtext");
    decrypt(body, sl.body || sl.title || "", 120);
    add(frame, qt, body);
    var who = sl.subtitle || (sl.body ? sl.title : "");
    if (who) frame.appendChild(el("div", "qwho", who));
  } else if (layout === "stat" && sl.stat) {
    if (sl.title) frame.appendChild(el("div", "stat-kicker", sl.title));
    add(frame, bigValue("stat-v", sl.stat.value, 150), sl.stat.label ? el("div", "stat-l", sl.stat.label) : null);
    if (sl.subtitle) frame.appendChild(el("p", "h-sub", sl.subtitle));
    if (sl.body) { var bm = md(sl.body, true); bm.style.marginTop = "18px"; bm.style.color = "var(--soft)"; frame.appendChild(bm); }
  } else {
    frame.appendChild(titleBlock(sl.title, sl.subtitle));
    var side = sideOf(sl);
    if (layout === "split") {
      var grid = el("div", "split");
      add(grid, md(sl.body || "", true), side || el("div"));
      frame.appendChild(grid);
    } else {
      if (sl.body) frame.appendChild(md(sl.body, true));
      if (side) frame.appendChild(side);
    }
  }
  if (slides.length > 1) frame.appendChild(el("div", "slidenum", String(i + 1).padStart(2, "0") + " / " + String(slides.length).padStart(2, "0")));
  return frame;
}
function sceneView(sc) {
  var frame = el("section", "frame");
  if (sc.title || sc.subtitle) { var hd = el("div", "sh"); hd.appendChild(titleBlock(sc.title, sc.subtitle)); frame.appendChild(hd); }
  frame.appendChild(kindBody(sc));
  return frame;
}

/* ---------- state ---------- */
var dataEl = document.getElementById("pulsar-data"), STATIC = !!dataEl, DATA = null;
if (STATIC) { try { DATA = JSON.parse(dataEl.textContent || "{}"); } catch (e) { DATA = {items: []}; } }
var items = [], cur = -1, slide = 0, follow = true, current = null, unseen = 0;
var stage = document.getElementById("stage");
function scene() { return items[cur] ? items[cur].scene : null; }
function nslides() { var s = scene(); return s && s.kind === "deck" ? (s.slides || []).length : 1; }
function upsert(it, announce) {
  if (!it || typeof it !== "object" || !it.scene || typeof it.scene !== "object") return;
  var id = str(it.scene.id);
  for (var i = 0; i < items.length; i++) if (str(items[i].scene.id) === id) {
    items[i] = it;
    if (i === cur) { slide = Math.min(slide, nslides() - 1); render(0, true); }
    dock(); return;
  }
  items.push(it);
  if (items.length > 80) { items.shift(); cur = Math.max(-1, cur - 1); }
  if (!announce) return;
  if (follow || cur < 0) go(items.length - 1, 0, "scene");
  else { unseen++; toast("new: " + (it.scene.title || it.scene.kind) + "   ] to view", "#36E2FF"); dock(); }
}
function go(idx, sl, how, dir) {
  if (!items.length) return;
  idx = clamp(idx, 0, items.length - 1);
  if (idx === cur && sl === slide && current) return;
  cur = idx; slide = clamp(sl || 0, 0, nslides() - 1);
  if (cur === items.length - 1) unseen = 0;
  render(dir || 1, false, how);
}
function step(d) {
  if (cur < 0) return;
  if (slide + d >= 0 && slide + d < nslides()) go(cur, slide + d, "slide", d);
  else if (cur + d >= 0 && cur + d < items.length) go(cur + d, d > 0 ? 0 : 0, "scene", d);
  else bump();
}
function bump() { if (!current || RM) return; current.animate && current.animate([{transform: "translateX(0)"}, {transform: "translateX(-8px)"}, {transform: "translateX(6px)"}, {transform: "translateX(0)"}], {duration: 260}); }
function render(dir, inPlace, how) {
  var sc = scene(), idle = document.getElementById("idle");
  if (!sc) { if (current) { current.remove(); current = null; } showIdle(); chrome(); return; }
  if (idle) idle.remove();
  var v = el("div", "view" + (inPlace && current ? " still" : ""));
  QUIET = !!(inPlace && current);
  try { v.appendChild(sc.kind === "deck" ? slideView(sc, slide) : sceneView(sc)); }
  catch (e) { v.appendChild(add(el("section", "frame"), el("div", "md", "This scene could not be rendered."))); }
  finally { QUIET = false; }
  var stacks = markText(v);
  if (inPlace && current) {
    var oldFill = current.querySelector(".prog-bar i"), newFill = v.querySelector(".prog-bar i"), top0 = current.scrollTop;
    if (oldFill && newFill) { var target = newFill.style.width; newFill.style.width = oldFill.style.width; requestAnimationFrame(function () { requestAnimationFrame(function () { newFill.style.width = target; }); }); }
    stage.replaceChild(v, current); current = v; fitStacks(stacks); v.scrollTop = top0; chrome(); return;
  }
  if (current && !RM) {
    var old = current;
    old.classList.add("leave");
    if (how === "scene") old.classList.add("glitchout"); else if (dir < 0) old.classList.add("back");
    setTimeout(function () { old.remove(); }, 450);
    v.classList.add(how === "scene" ? "glitch" : "enter");
    if (dir < 0 && how !== "scene") v.classList.add("back");
  } else if (current) current.remove();
  stage.appendChild(v); current = v; fitStacks(stacks); chrome();
  try { history.replaceState(null, "", location.pathname + location.search + "#" + (cur + 1) + "." + (slide + 1)); } catch (e) {}
}
function readHash() { var m = /^#(\d+)(?:\.(\d+))?$/.exec(location.hash); return m ? [parseInt(m[1], 10) - 1, m[2] ? parseInt(m[2], 10) - 1 : 0] : null; }
function showIdle() {
  if (document.getElementById("idle")) return;
  var d = el("div"); d.id = "idle";
  var inner = el("div"), beacon = el("div", "beacon");
  add(beacon, el("i"), el("i"), el("i"), el("i"), el("b"));
  var st = el("div", "state"); st.id = "idlestate";
  st.textContent = STATIC ? "This export has no scenes." : "connecting to Pulsar Core";
  var hint1 = el("div", "hint"), hint2 = el("div", "hint");
  hint1.appendChild(document.createTextNode("Ask Claude to present something, for example "));
  hint1.appendChild(el("code", "", "\"walk me through this repo on Pulsar Stage\""));
  hint2.textContent = "Decks, charts, diagrams and live progress appear here the moment the model sends them.";
  add(inner, beacon, el("h1", "", "PULSAR STAGE"), el("div", "await", "AWAITING TRANSMISSION"), st, hint1, hint2);
  d.appendChild(inner); stage.appendChild(d);
}
function chrome() {
  var sc = scene(), n = nslides();
  document.getElementById("kind").textContent = sc ? (KIND_LABEL[sc.kind] || sc.kind) : "idle";
  document.getElementById("ttl").textContent = sc ? (sc.title || "") : "";
  document.getElementById("pos").textContent = sc && n > 1 ? (slide + 1) + " / " + n : (items.length > 1 ? "scene " + (cur + 1) + "/" + items.length : "");
  var it = items[cur];
  document.getElementById("sid").textContent = it && it.sid ? it.sid : "";
  document.getElementById("railfill").style.width = sc ? (n > 1 ? (100 * (slide + 1) / n) : 100) + "%" : "0%";
  var fl = document.getElementById("follow");
  fl.textContent = STATIC ? "" : (follow ? "FOLLOW" : "PINNED") + (unseen ? "  +" + unseen + " new" : "");
  fl.className = "follow" + (follow ? "" : " pinned");
  document.title = (sc && sc.title ? sc.title + " · " : "") + "Pulsar Stage";
  var notes = document.getElementById("notes");
  if (notes.classList.contains("on")) showNotes(true);
  dock();
}
function dock() {
  var d = document.getElementById("dock");
  while (d.firstChild) d.removeChild(d.firstChild);
  if (items.length < 2) return;
  items.forEach(function (it, i) {
    var b = el("button", i === cur ? "on" : ""); b.type = "button";
    add(b, el("b", "", (KIND_LABEL[it.scene.kind] || it.scene.kind || "").toUpperCase()), el("span", "", it.scene.title || ("scene " + (i + 1))));
    b.addEventListener("click", function () { go(i, 0, "scene", i >= cur ? 1 : -1); });
    d.appendChild(b);
  });
  var on = d.querySelector(".on"); if (on && on.scrollIntoView) on.scrollIntoView({block: "nearest", inline: "nearest"});
}
function toast(msg, color) {
  var t = el("div", "toast", msg); if (color) t.style.setProperty("--c", color);
  var box = document.getElementById("toasts"); box.appendChild(t);
  setTimeout(function () { t.remove(); }, 3300);
}
function showNotes(keep) {
  var n = document.getElementById("notes"), sc = scene(), sl = sc && sc.kind === "deck" ? (sc.slides || [])[slide] : null;
  var text = sl && sl.notes ? sl.notes : "";
  if (!keep) n.classList.toggle("on");
  if (n.classList.contains("on") && !text) { n.classList.remove("on"); if (!keep) toast("no speaker notes on this slide", "#58546c"); return; }
  document.getElementById("notestxt").textContent = text;
}

/* ---------- live connection ---------- */
var es = null, backoff = 800, liveState = "";
function setLive(s, extra) {
  liveState = s;
  var lv = document.getElementById("live"), tx = document.getElementById("livetxt");
  lv.className = "live " + (s === "live" ? "on" : s === "static" ? "static" : s === "expired" || s === "offline" ? "off" : "wait");
  tx.textContent = s === "live" ? "LIVE" : s === "static" ? "EXPORT" : s === "expired" ? "EXPIRED" : s === "offline" ? "OFFLINE" : s === "connecting" ? "CONNECTING" : "RECONNECTING";
  var st = document.getElementById("idlestate");
  if (st) st.textContent = s === "live" ? "● connected · listening for scenes" : s === "expired" ? "this link expired" : s === "static" ? (extra || "") : s === "connecting" ? "connecting to Pulsar Core" : "reconnecting to Pulsar Core";
}
function expired() {
  setLive("expired");
  if (document.querySelector(".banner")) return;
  var b = el("div", "banner");
  b.appendChild(document.createTextNode("This stage link expired (Pulsar Core restarted). Run "));
  b.appendChild(el("code", "", "pulsarcode stage --web"));
  b.appendChild(document.createTextNode(" for a fresh one."));
  document.body.appendChild(b);
}
function connect() {
  if (STATIC) return;
  if (es) { try { es.close(); } catch (e) {} es = null; }
  fetch("/pulsar/stage/scenes" + location.search, {cache: "no-store", credentials: "omit"}).then(function (r) {
    if (r.status === 401) { expired(); return null; }
    if (!r.ok) throw new Error("http " + r.status);
    return r.json();
  }).then(function (d) {
    if (d === null) return;
    var had = items.length;
    ((d && d.scenes) || []).forEach(function (it) { upsert(it, false); });
    if (items.length && (cur < 0 || (follow && items.length !== had))) go(items.length - 1, 0, "scene");
    else render(0, true);
    openStream();
  }).catch(function () { setLive("offline"); retry(); });
}
function retry() { setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 15000); }
function openStream() {
  es = new EventSource("/pulsar/stage/stream" + location.search);
  es.onopen = function () { setLive("live"); backoff = 800; };
  es.addEventListener("scene", function (e) { try { upsert(JSON.parse(e.data), true); } catch (err) {} });
  es.addEventListener("clear", function (e) {
    var sid = ""; try { sid = str(JSON.parse(e.data).sid); } catch (err) {}
    var keep = items.filter(function (it) { return sid && it.sid !== sid; }), curId = scene() ? str(scene().id) : "";
    items = keep; cur = -1;
    for (var i = 0; i < items.length; i++) if (str(items[i].scene.id) === curId) cur = i;
    if (cur < 0 && items.length) cur = items.length - 1;
    slide = 0; render(0, false, "scene"); toast("stage cleared", "#58546c");
  });
  es.onerror = function () {
    if (!es) return;
    if (es.readyState === 2) { es.close(); es = null; setLive("offline"); retry(); }
    else setLive("reconnecting");
  };
}

/* ---------- input ---------- */
var digits = "", digitsAt = 0;
document.addEventListener("keydown", function (e) {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  var k = e.key, help = document.getElementById("help");
  if (help.classList.contains("on")) { help.classList.remove("on"); e.preventDefault(); return; }
  if (k === "ArrowRight" || k === " " || k === "PageDown" || k === "l") { step(1); e.preventDefault(); }
  else if (k === "ArrowLeft" || k === "PageUp" || k === "h" || k === "Backspace") { step(-1); e.preventDefault(); }
  else if (k === "Home") { go(cur, 0, "slide", -1); e.preventDefault(); }
  else if (k === "End") { go(cur, nslides() - 1, "slide", 1); e.preventDefault(); }
  else if (k === "]") go(cur + 1, 0, "scene", 1);
  else if (k === "[") go(cur - 1, 0, "scene", -1);
  else if (k === "f" && !STATIC) { follow = !follow; toast(follow ? "following the newest scene" : "pinned: new scenes will not take over", follow ? "#50FAAA" : "#FFC400"); if (follow && items.length) go(items.length - 1, 0, "scene"); chrome(); }
  else if (k === "n") showNotes(false);
  else if (k === "?") help.classList.add("on");
  else if (k === "p") window.print();
  else if (k === "Escape") { document.getElementById("notes").classList.remove("on"); }
  else if (/^[0-9]$/.test(k)) {
    var now = Date.now(); digits = (now - digitsAt < 800 ? digits : "") + k; digitsAt = now;
    var n = parseInt(digits, 10), total = nslides();
    if (n >= 1 && n * 10 > total) { go(cur, Math.min(n, total) - 1, "slide", n - 1 >= slide ? 1 : -1); digits = ""; }
  }
});
document.getElementById("prev").addEventListener("click", function () { step(-1); });
document.getElementById("next").addEventListener("click", function () { step(1); });
var touchX = null;
stage.addEventListener("touchstart", function (e) { touchX = e.touches[0].clientX; }, {passive: true});
stage.addEventListener("touchend", function (e) { if (touchX === null) return; var dx = e.changedTouches[0].clientX - touchX; if (Math.abs(dx) > 60) step(dx < 0 ? 1 : -1); touchX = null; }, {passive: true});
var rz = null;
window.addEventListener("resize", function () { clearTimeout(rz); rz = setTimeout(function () { if (scene()) render(0, true); }, 180); });
var printed = null;
window.addEventListener("beforeprint", function () {
  printed = document.createElement("div");
  items.forEach(function (it) {
    var sc = it.scene, count = sc.kind === "deck" ? (sc.slides || []).length : 1;
    for (var i = 0; i < count; i++) {
      var v = el("div", "view");
      try { v.appendChild(sc.kind === "deck" ? slideView(sc, i) : sceneView(sc)); }
      catch (e) { v.appendChild(add(el("section", "frame"), el("div", "md", "This scene could not be rendered."))); }
      printed.appendChild(v);
    }
  });
  if (current) current.style.display = "none";
  stage.appendChild(printed);
});
window.addEventListener("afterprint", function () { if (printed) printed.remove(); printed = null; if (current) current.style.display = ""; });

/* ---------- clock + sky ---------- */
function tick() { var d = new Date(); document.getElementById("clock").textContent = d.toTimeString().slice(0, 8); }
tick(); setInterval(tick, 1000);
(function sky() {
  var c = document.getElementById("sky"), ctx = c.getContext && c.getContext("2d");
  if (!ctx) return;
  var stars = [], W = 0, H = 0, dpr = Math.min(2, window.devicePixelRatio || 1), t0 = performance.now();
  function size() {
    W = c.clientWidth; H = c.clientHeight; c.width = W * dpr; c.height = H * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    stars = []; var n = Math.round(W * H / 5200);
    for (var i = 0; i < n; i++) stars.push({x: Math.random() * W, y: Math.random() * H, z: Math.random(), p: Math.random() * 6.28, h: Math.random()});
  }
  function frame(now) {
    var t = (now - t0) / 1000;
    ctx.clearRect(0, 0, W, H);
    for (var i = 0; i < stars.length; i++) {
      var s = stars[i], x = (s.x - t * (4 + s.z * 10)) % W; if (x < 0) x += W;
      var tw = 0.45 + 0.55 * Math.sin(t * (0.6 + s.z * 1.6) + s.p), a = (0.15 + 0.6 * s.z) * tw;
      ctx.fillStyle = s.h > 0.82 ? "rgba(54,226,255," + a + ")" : s.h > 0.7 ? "rgba(255,46,136," + a * 0.8 + ")" : "rgba(236,246,255," + a * 0.8 + ")";
      var r = 0.4 + s.z * 1.1; ctx.beginPath(); ctx.arc(x, s.y, r, 0, 6.283); ctx.fill();
    }
    var cx = W * 0.86, cy = H * 0.14, period = 4.2;
    for (var k = 0; k < 3; k++) {
      var p = ((t / period) + k / 3) % 1, R = 20 + p * Math.max(W, H) * 0.45;
      ctx.strokeStyle = "rgba(" + (p < 0.5 ? "54,226,255" : "157,77,255") + "," + (0.16 * (1 - p)) + ")";
      ctx.lineWidth = 1.2; ctx.beginPath(); ctx.arc(cx, cy, R, 0, 6.283); ctx.stroke();
    }
    var ang = t * 0.35, beam = ctx.createLinearGradient(cx, cy, cx + Math.cos(ang) * W, cy + Math.sin(ang) * W);
    beam.addColorStop(0, "rgba(54,226,255,.10)"); beam.addColorStop(1, "rgba(54,226,255,0)");
    ctx.strokeStyle = beam; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.cos(ang) * W * 1.2, cy + Math.sin(ang) * W * 1.2); ctx.stroke();
    var g = ctx.createRadialGradient(cx, cy, 0, cx, cy, 16); g.addColorStop(0, "rgba(255,255,255,.9)"); g.addColorStop(0.35, "rgba(54,226,255,.7)"); g.addColorStop(1, "rgba(54,226,255,0)");
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, 16, 0, 6.283); ctx.fill();
    if (!RM) requestAnimationFrame(frame);
  }
  size(); window.addEventListener("resize", size);
  requestAnimationFrame(frame);
})();

/* ---------- boot ---------- */
if (STATIC) {
  ((DATA && DATA.items) || []).forEach(function (it) { upsert(it, false); });
  setLive("static", "exported " + str(DATA && DATA.generated));
  follow = false;
  var h0 = readHash();
  if (items.length) go(h0 ? h0[0] : 0, h0 ? h0[1] : 0, "scene"); else render(0);
  window.addEventListener("hashchange", function () { var h = readHash(); if (h && (h[0] !== cur || h[1] !== slide)) go(h[0], h[1], "scene", h[0] >= cur ? 1 : -1); });
  document.getElementById("follow").textContent = "";
} else {
  setLive("connecting"); render(0); connect();
}
})();
</script>
</body>
</html>
"""


# The one inline script runs by its hash: an inline script anything injected into the page would never run.
CSP_LIVE = ("default-src 'none'; script-src %s; style-src 'unsafe-inline'; img-src data:; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'" % script_hash())
CSP_STATIC = ("default-src 'none'; script-src %s; style-src 'unsafe-inline'; img-src data:; "
              "base-uri 'none'; form-action 'none'" % script_hash())
