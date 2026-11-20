"""The local web viewer: server-rendered HTML, no build step, bound to 127.0.0.1.

Screens: Live, Sessions (with tagging), Conversations, Pull requests, Overview, Rules, Review. An app shell (sidebar, compact
headers, panels) in the palette of the demo video: Geist for text, Geist Mono for data, amber
for once itself, rose for trouble, mint for success. Fonts are bundled, so nothing is fetched
from the network. Writes need the per-process token, so another page in
the browser cannot post to it.
"""
import html
import json
import re
import secrets
import subprocess
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import conversations, detectors, index, paths, prs, redact, rules, snapshot, store

TOKEN = secrets.token_urlsafe(18)
ACTIVE_SECS = 300
TAGS = [("wrong", "Wrong"), ("avoidable", "Avoidable"), ("missing_knowledge", "Needs context"), ("good", "Good")]
SCREENS = [("live", "Live"), ("sessions", "Sessions"), ("conversations", "Conversations"), ("prs", "Pull requests"),
           ("overview", "Overview"), ("rules", "Rules"), ("review", "Review")]
FONTS = Path(__file__).parent / "assets" / "fonts"

CSS = """
@font-face{font-family:Geist;src:url(/static/fonts/geist-sans-latin-400-normal.woff2) format('woff2');font-weight:400;font-display:swap}
@font-face{font-family:Geist;src:url(/static/fonts/geist-sans-latin-500-normal.woff2) format('woff2');font-weight:500;font-display:swap}
@font-face{font-family:Geist;src:url(/static/fonts/geist-sans-latin-600-normal.woff2) format('woff2');font-weight:600;font-display:swap}
@font-face{font-family:'Geist Mono';src:url(/static/fonts/geist-mono-latin-400-normal.woff2) format('woff2');font-weight:400;font-display:swap}
@font-face{font-family:'Geist Mono';src:url(/static/fonts/geist-mono-latin-500-normal.woff2) format('woff2');font-weight:500;font-display:swap}
:root{--bg:#0B0B0C;--side:#0F0F11;--panel:#141416;--raise:#1A1A1D;--ink:#F3F1EC;--muted:#A9A59C;--faint:#6F6C66;--ghost:#4A4844;
--amber:#E9A23B;--mint:#7FD6A4;--rose:#F08A7E;--line:rgba(243,241,236,.08);--edge:rgba(243,241,236,.12);--field:rgba(243,241,236,.04);
--sans:Geist,system-ui,-apple-system,sans-serif;--mono:'Geist Mono',ui-monospace,Menlo,monospace}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans);-webkit-font-smoothing:antialiased}
a{color:inherit}
:focus-visible{outline:2px solid var(--amber);outline-offset:2px;border-radius:6px}
.mono{font-family:var(--mono)}.mute{color:var(--muted)}.faint{color:var(--faint)}.rose{color:var(--rose)}.mint{color:var(--mint)}.amb{color:var(--amber)}

/* app shell: sidebar + content */
.app{display:grid;grid-template-columns:208px minmax(0,1fr);min-height:100vh;background:linear-gradient(90deg,var(--side) 0 208px,rgba(243,241,236,.08) 208px 209px,transparent 209px)}
aside{padding:16px 10px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:9px;padding:4px 10px 18px;text-decoration:none;font-weight:600;font-size:16px;letter-spacing:-.02em}
aside nav a{display:flex;align-items:center;gap:10px;padding:7px 10px;border-radius:8px;color:var(--muted);text-decoration:none;font-size:14px}
aside nav a:hover{background:rgba(243,241,236,.04);color:var(--ink)}
aside nav a.on{background:rgba(243,241,236,.07);color:var(--ink)}
aside nav a.on:before{content:"";width:6px;height:6px;border-radius:50%;background:var(--amber);box-shadow:0 0 8px var(--amber);margin-left:-2px}
aside nav a .n{margin-left:auto;font:500 11.5px var(--mono);color:var(--amber);background:rgba(233,162,59,.14);border-radius:999px;padding:1px 7px}
aside .foot{margin-top:auto;padding:10px;font:11.5px/1.6 var(--mono);color:var(--faint)}
aside .foot i{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--mint);margin-right:6px;vertical-align:middle}
main{min-width:0;padding:20px 28px 48px;max-width:1360px}
.top{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;padding-bottom:16px;margin-bottom:18px;border-bottom:1px solid var(--line)}
h1{font:600 20px/1.2 var(--sans);letter-spacing:-.01em;margin:0}
.sum{color:var(--muted);margin-top:4px}
.hint{color:var(--faint);font-size:13px;margin:-8px 0 16px}
.repos{font:12.5px var(--mono);color:var(--faint);text-align:right;white-space:nowrap}
.repopick{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--faint)}
.repopick select{font:12.5px var(--mono);color:var(--ink);background:var(--raise);border:1px solid var(--edge);border-radius:8px;
 padding:7px 30px 7px 10px;min-height:34px;max-width:min(340px,70vw);appearance:none;-webkit-appearance:none;cursor:pointer;
 background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);
 background-position:calc(100% - 15px) 50%,calc(100% - 10px) 50%;background-size:5px 5px;background-repeat:no-repeat}
.repopick select:hover{border-color:rgba(243,241,236,.3)}.repopick select:focus{outline:none;border-color:rgba(233,162,59,.6)}
.tools{display:flex;flex-wrap:wrap;justify-content:flex-end;align-items:center;gap:10px}.tools .search input[type=text]{width:220px}
.top>div:first-child{min-width:0;flex:1 1 280px}
.repos a{color:var(--faint);text-decoration:none}.repos a:hover{color:var(--ink)}.repos b{color:var(--ink);font-weight:500}.repos .sep{color:var(--ghost);margin:0 7px}

/* panels */
.win{border-radius:12px;background:var(--panel);border:1px solid var(--edge);margin-bottom:16px;overflow:hidden}
.bar{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--line);font:500 13px var(--sans);color:var(--ink)}
.bar .right{margin-left:auto;font:12px var(--mono);color:var(--faint)}
.pad{padding:12px 14px}
.status{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:9px 16px;border-top:1px solid var(--line);background:rgba(0,0,0,.3);font:12.5px var(--mono);color:var(--muted)}
.status .sep{color:var(--ghost)}
.cols{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:16px;align-items:start}
.cols.even{grid-template-columns:1fr 1fr}.cols.list{grid-template-columns:minmax(0,.85fr) minmax(0,1.4fr)}
.cols>div{min-width:0}

/* timeline rows, as in the video */
.tl{font-family:var(--mono);font-size:13px}
.st{display:flex;align-items:center;gap:12px;padding:5px 10px;border-radius:7px}
.st:hover{background:rgba(243,241,236,.03)}
.st .num{color:var(--ghost);width:22px;text-align:right;flex:none}
.st .d{width:7px;height:7px;border-radius:50%;background:#55534E;flex:none}
.st .k{color:var(--faint);width:60px;flex:none;overflow:hidden;text-overflow:ellipsis}
.st .t{flex:1 1 0;min-width:30%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.st .r{flex:0 1 auto;max-width:46%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--faint);font-size:12.5px}
.st.bad .d{background:var(--rose)}.st.bad .r{color:var(--rose)}
.st.ok .d{background:var(--mint)}.st.ok .r{color:var(--mint)}
.st.prompt .t{color:var(--muted)}
.st.now{background:rgba(233,162,59,.07)}.st.now .d{background:var(--amber);box-shadow:0 0 8px var(--amber)}
.st.tagged .t:after{content:" · tagged";color:var(--amber)}
.flagbox{margin:3px -2px;border-radius:9px;border:1px solid rgba(240,138,126,.4);background:rgba(240,138,126,.07);padding:1px 0}
.flagnote{padding:1px 10px 7px 56px;font:13px var(--sans);color:var(--rose)}
.flagnote .det{display:inline-block}
details.mk{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}details.mk>summary{cursor:pointer;color:var(--amber);font-size:13px;list-style:none}details.mk>summary::-webkit-details-marker{display:none}.flagnote .det:first-letter{text-transform:uppercase}

/* lists and side panels */
.card{display:block;text-decoration:none;padding:11px 14px;border-radius:10px;border:1px solid var(--edge);background:var(--panel);margin-bottom:8px}
.card:hover{border-color:rgba(243,241,236,.24)}
.card.on{border-color:rgba(233,162,59,.55);background:rgba(233,162,59,.05)}
.card b{font-weight:500}.card .meta{display:block;margin-top:3px;font:12px var(--mono);color:var(--faint)}
.box{border-radius:12px;border:1px solid var(--edge);background:var(--panel);padding:14px 16px;margin-bottom:12px}
.box h3{margin:0;font:500 14px var(--sans)}.box p{margin:6px 0 0;color:var(--muted)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--amber);margin-left:7px;vertical-align:middle}
.dot.rose{background:var(--rose)}
.chip{display:inline-block;font:12px var(--mono);padding:2px 9px;border-radius:999px;border:1px solid var(--edge);color:var(--muted)}
.chip.mint{color:var(--mint);border-color:rgba(127,214,164,.45);background:rgba(127,214,164,.08)}
.chip.rose{color:var(--rose);border-color:rgba(240,138,126,.45);background:rgba(240,138,126,.08)}
.chip.amb{color:var(--amber);border-color:rgba(233,162,59,.45);background:rgba(233,162,59,.08)}
.directive{font:500 17px/1.4 var(--sans);margin:8px 0 2px}
.rid{font:12px var(--mono);color:var(--faint)}

/* tables */
.rows{width:100%;border-collapse:collapse;font-size:13.5px}
.rows th{text-align:left;font:500 12px var(--sans);color:var(--faint);padding:9px 14px;border-bottom:1px solid var(--line);white-space:nowrap}
.rows td{padding:9px 14px;border-bottom:1px solid var(--line);vertical-align:middle}
.rows tr:last-child td{border-bottom:0}
.rows tbody tr{cursor:pointer}.rows tbody tr:hover{background:rgba(243,241,236,.03)}
.rows td.when a{font-family:var(--mono);font-size:12.5px;color:var(--ink);text-decoration:none;white-space:nowrap}
.rows td.p{color:var(--muted);max-width:52ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rows td.n{font-family:var(--mono);color:var(--muted);white-space:nowrap}
.rows .zero{color:var(--ghost)}

/* numbers */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(150px,100%),1fr));gap:10px;margin-bottom:16px}
.tile{border-radius:10px;border:1px solid var(--edge);background:var(--panel);padding:11px 14px}
.tile b{display:block;font:500 22px/1.2 var(--sans);margin-top:3px;font-variant-numeric:tabular-nums}.tile span{font-size:12.5px;color:var(--faint)}
.bars{display:flex;flex-direction:column;gap:10px}
.bars .row{display:grid;grid-template-columns:minmax(0,.9fr) minmax(0,1.3fr) minmax(0,1.5fr);gap:12px;align-items:center;font-size:13px}
.bars .track{height:6px;border-radius:999px;background:rgba(243,241,236,.06);overflow:hidden}
.bars .fill{height:100%;border-radius:999px;background:var(--rose)}
.replay .lane{display:flex;align-items:center;gap:8px;margin-top:10px}
.replay .lane>span:first-child{width:118px;color:var(--muted);font-size:13px;flex:none}
.replay .run{flex:1;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:15px}
.replay .run.x{background:rgba(240,138,126,.1);border:1px solid rgba(240,138,126,.35);color:var(--rose)}
.replay .run.v{background:rgba(127,214,164,.1);border:1px solid rgba(127,214,164,.35);color:var(--mint)}
.facts{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:12px;padding-top:10px;border-top:1px solid var(--line);font:12px var(--mono);color:var(--faint)}

/* forms */
details.box summary{cursor:pointer;list-style:none}details.box summary::-webkit-details-marker{display:none}
details.box summary:after{content:"+";float:right;color:var(--faint);font:16px var(--mono)}details.box[open] summary:after{content:"–"}
label.lbl{display:block;font-size:12.5px;color:var(--faint);margin:12px 0 5px}
input[type=text]{width:100%;font:13.5px var(--sans);color:var(--ink);background:var(--field);border:1px solid var(--edge);border-radius:8px;padding:8px 10px}
input[type=text]::placeholder{color:var(--faint)}input[type=text]:focus{outline:none;border-color:rgba(233,162,59,.6)}
input.num{width:56px;text-align:center;font-family:var(--mono)}
.steps{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px;margin-top:10px}
.pick{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.pick input{position:absolute;opacity:0;pointer-events:none}
.pick label{font-size:12.5px;padding:4px 11px;border-radius:999px;border:1px solid var(--edge);color:var(--muted);cursor:pointer}
.pick input:checked+label{background:var(--amber);color:#17130C;border-color:var(--amber)}
.pick input:focus-visible+label{outline:2px solid var(--amber);outline-offset:2px}
.actions{display:flex;gap:8px;margin-top:12px}
button{font:500 13px var(--sans);border-radius:8px;padding:7px 14px;cursor:pointer;border:1px solid var(--edge);background:var(--raise);color:var(--ink)}
button:hover{border-color:rgba(243,241,236,.3)}
button.pri{background:var(--amber);border-color:var(--amber);color:#17130C}button.pri:hover{filter:brightness(1.08)}
.empty{padding:22px 16px;color:var(--muted)}.empty code,.box code{font-family:var(--mono);color:var(--ink);background:var(--field);padding:1px 6px;border-radius:5px}

/* keyboard, trend, deltas */
.ksel{outline:1px solid rgba(233,162,59,.7);outline-offset:-1px;background:rgba(233,162,59,.06)!important;border-radius:7px}
.tile a{text-decoration:none;color:inherit;display:block}.tile:hover{border-color:rgba(243,241,236,.24)}
.tile small{display:block;font:12px var(--mono);color:var(--faint);margin-top:2px}.tile small.up{color:var(--rose)}.tile small.down{color:var(--mint)}
.trend svg{display:block;width:100%;height:auto}.trend .ax{stroke:var(--line)}.trend .ln{fill:none;stroke-width:2}
.trend .lbl{fill:var(--faint);font:11px var(--mono)}.trend .pt{stroke:var(--panel);stroke-width:2}
.seg{display:inline-flex;gap:4px}.seg a{font:12px var(--mono);padding:2px 9px;border-radius:999px;border:1px solid var(--edge);color:var(--faint);text-decoration:none}
.seg a.on{color:var(--ink);border-color:rgba(233,162,59,.55)}
.msg{border-radius:10px;border:1px solid rgba(233,162,59,.45);background:rgba(233,162,59,.07);padding:10px 14px;margin-bottom:12px;white-space:pre-wrap;font-size:13px}
#keys{position:fixed;right:16px;bottom:16px;background:var(--raise);border:1px solid var(--edge);border-radius:10px;padding:12px 14px;font:12.5px/1.8 var(--mono);color:var(--muted);z-index:9}
#keys b{color:var(--ink);font-weight:500;display:inline-block;min-width:54px}
.dot.grey{background:var(--ghost)}

/* expandable steps */
details.stp>summary{list-style:none;cursor:pointer}details.stp>summary::-webkit-details-marker{display:none}
details.stp[open]>summary{background:rgba(243,241,236,.04)}
.stx{margin:2px 10px 8px 56px;padding:10px 12px;border-left:2px solid var(--edge);font:13px var(--sans);color:var(--muted)}
.stx pre{margin:4px 0 0;padding:8px 10px;background:rgba(0,0,0,.35);border-radius:7px;font:12.5px/1.5 var(--mono);color:var(--ink);white-space:pre-wrap;word-break:break-word;max-height:320px;overflow:auto}
.stx pre.err{color:var(--rose)}
.stx .lab{font:11.5px var(--mono);color:var(--faint);margin-top:8px}.stx .lab:first-child{margin-top:0}
.stx p{margin:8px 0 0}.stx a{color:var(--amber)}
/* diff */
.files{display:flex;flex-direction:column}
details.fd{border-top:1px solid var(--line)}details.fd:first-child{border-top:0}
details.fd>summary{list-style:none;cursor:pointer;display:flex;gap:12px;align-items:center;padding:9px 16px;font:13px var(--mono)}
details.fd>summary::-webkit-details-marker{display:none}
details.fd>summary .p{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.add{color:var(--mint)}.del{color:var(--rose)}
.patch{font:12.5px/1.55 var(--mono);overflow:auto;max-height:520px;border-top:1px solid var(--line);background:rgba(0,0,0,.25)}
.patch div{padding:0 16px;white-space:pre}.patch .a{background:rgba(127,214,164,.09);color:var(--mint)}
.patch .r{background:rgba(240,138,126,.09);color:var(--rose)}.patch .h{color:var(--faint);padding-top:6px}
/* impact */
.impact{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:10px}
.impact div{border:1px solid var(--line);border-radius:8px;padding:8px 10px}.impact b{display:block;font:500 18px var(--sans)}
.impact span{font-size:12px;color:var(--faint)}
.ba{display:grid;grid-template-columns:70px minmax(0,1fr) auto;gap:10px;align-items:center;margin-top:10px;font-size:13px}
.ba .track{height:6px;border-radius:999px;background:rgba(243,241,236,.06);overflow:hidden}.ba .fill{display:block;height:100%;background:var(--rose)}
/* health */
.checks{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.checks div{display:grid;grid-template-columns:18px 140px minmax(0,1fr);gap:8px;font-size:13px;align-items:baseline}
.checks .d{font:12.5px var(--mono);color:var(--faint);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* conversations */
.thread{display:flex;flex-direction:column;gap:12px;padding:16px 18px 20px}
.cv{max-width:86%;min-width:0}.cv .who{font:11.5px var(--mono);color:var(--faint);margin-bottom:3px}
.cv .tx{white-space:pre-wrap;word-break:break-word}
.cv.u{align-self:flex-end;max-width:78%;background:#1F1F23;border:1px solid var(--line);border-radius:12px 12px 4px 12px;padding:10px 14px}
.cv.c{border-color:rgba(233,162,59,.45);background:rgba(233,162,59,.07)}.cv.c .who{color:var(--amber)}
.cv.a .tx{color:#E6E3DC}
details.cvs{border-radius:9px;background:rgba(0,0,0,.25);border:1px solid var(--line)}details.cvs.bad{border-color:rgba(240,138,126,.4)}
details.cvs>summary{cursor:pointer;list-style:none;padding:8px 12px;font:12.5px var(--mono);color:var(--muted);display:flex;flex-wrap:wrap;gap:4px 10px}
details.cvs>summary::-webkit-details-marker{display:none}details.cvs>summary b{color:var(--ink);font-weight:500}
details.cvs .ln{padding:0 12px;font:12.5px/1.75 var(--mono);color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
details.cvs .ln:last-of-type{padding-bottom:8px}details.cvs .ln a{color:var(--ghost);text-decoration:none;margin-right:8px}
details.cvs .ln.bad{color:var(--rose)}
.cvflag{padding:8px 12px;border-top:1px solid rgba(240,138,126,.3);color:var(--rose);font-size:13px}
.clist .card .t{display:flex;gap:8px;align-items:center}.clist .card .t b{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.clist .card .l{display:block;margin-top:3px;color:var(--muted);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.day{font:500 12px var(--sans);color:var(--faint);margin:12px 2px 8px}.day:first-child{margin-top:0}
.convhd{padding:14px 18px;border-bottom:1px solid var(--line)}.convhd .r{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
.convhd h2{margin:0;font:500 16px/1.35 var(--sans);flex:1 1 260px;min-width:0}
a.btn{font:500 13px var(--sans);border-radius:8px;padding:7px 14px;border:1px solid var(--edge);background:var(--raise);color:var(--ink);text-decoration:none}
a.btn:hover{border-color:rgba(243,241,236,.3)}
form.inline{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.search{display:flex;gap:8px;align-items:center}.search input[type=text]{width:260px;max-width:60vw}
/* pull requests */
.prcard .hd{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;padding:12px 16px;border-bottom:1px solid var(--line)}
.prcard .hd .ttl{font-weight:500;font-size:15px;text-decoration:none;color:var(--ink)}.prcard .hd .ttl span{font-family:var(--mono);color:var(--muted)}
.prcard .hd .ttl:hover{color:var(--amber)}
.prcard .hd .meta{margin-left:auto;display:flex;flex-wrap:wrap;align-items:center;gap:12px;font:12.5px var(--mono);color:var(--muted)}
.linkf input[type=text]{width:170px;padding:6px 9px}
.scroll{overflow-x:auto}

@media (max-width:900px){.cols,.cols.even,.cols.list{grid-template-columns:minmax(0,1fr)}
 .convcols>div:last-child{order:-1}}
@media (max-width:700px){
 .app{grid-template-columns:minmax(0,1fr);grid-template-rows:auto 1fr;background:none}aside nav a{white-space:nowrap}
 .repos{white-space:normal}.repos a,.repos b{white-space:nowrap}.search input[type=text]{width:auto;flex:1;min-width:0}.search{width:100%}aside{background:var(--side);border-bottom:1px solid var(--line);position:static;height:auto;flex-direction:row;align-items:center;gap:4px;padding:8px;overflow-x:auto}
 .brand{padding:4px 8px}aside nav{display:flex;gap:2px}aside nav a.on:before{display:none}aside .foot{display:none}
 main{padding:16px 14px 40px}.top{flex-direction:column;align-items:flex-start}.repos{text-align:left}.rows .opt{display:none}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

ROSEDOT = '<span class="dot rose"></span>'
STALE_DOT = '<span class="dot grey" title="stale: not given in 30 days"></span>'
LOGO = ('<svg width="20" height="20" viewBox="0 0 28 28" aria-hidden="true"><circle cx="14" cy="14" r="12" fill="none" '
        'stroke="#F3F1EC" stroke-width="1.8"/><circle cx="14" cy="14" r="3.6" fill="#E9A23B"/></svg>')


def e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def ago(ts) -> str:
    if not ts:
        return "never"
    d = time.time() - ts
    for unit, sec in (("d", 86400), ("h", 3600), ("min", 60)):
        if d >= sec:
            return f"{int(d // sec)} {unit} ago"
    return "just now"


def plural(n, word, many=None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _q(repo, **more) -> str:
    d = {k: v for k, v in (("repo", repo), *more.items()) if v}
    return ("?" + urllib.parse.urlencode(d)) if d else ""


def _waiting(slug) -> int:
    try:
        return sum(1 for r in rules.load_all(slug) if r.get("status") == "candidate") if slug else 0
    except Exception:
        return 0


KEYS_JS = """<script>(()=>{const items=()=>[...document.querySelectorAll('[data-k]')];let i=-1;
function sel(n){const it=items();if(!it.length)return;i=Math.max(0,Math.min(it.length-1,n));it.forEach(e=>e.classList.remove('ksel'));
const e=it[i];e.classList.add('ksel');e.scrollIntoView({block:'nearest'})}
const nav={'1':'/live','2':'/sessions','3':'/conversations','4':'/prs','5':'/overview','6':'/rules','7':'/review'};
const tags={w:'wrong',a:'avoidable',m:'missing_knowledge',g:'good'};
document.addEventListener('keydown',ev=>{if(ev.target.closest('input,textarea,select')||ev.metaKey||ev.ctrlKey||ev.altKey)return;
const k=ev.key;if(k==='j'){sel(i+1);ev.preventDefault()}else if(k==='k'){sel(i-1);ev.preventDefault()}
else if(k==='Enter'&&i>=0){const e=items()[i];const h=e.dataset.href||(e.matches('a')?e.href:(e.querySelector('a')||{}).href);if(h){location.href=h;ev.preventDefault()}}
else if(nav[k]){const q=new URLSearchParams(location.search);const r=q.get('repo');location.href=nav[k]+(r?'?repo='+encodeURIComponent(r):'')}
else if(tags[k]&&document.getElementById('tagform')){const f=document.getElementById('tagform');const e=items()[i];
if(e&&e.dataset.step){if(!(ev.shiftKey&&f.lo.value))f.lo.value=e.dataset.step;f.hi.value=e.dataset.step}
const r=f.querySelector('input[value="'+tags[k]+'"]');if(r)r.checked=true;const d=f.closest('details');if(d)d.open=true;f.note.focus();ev.preventDefault()}
else if(k==='?'){document.getElementById('keys').toggleAttribute('hidden')}})})();</script>"""
KEYS_HELP = ('<div id="keys" hidden><b>j / k</b> move<br><b>Enter</b> open<br><b>1 to 7</b> Live, Sessions, Conversations, Pull requests, Overview, Rules, Review'
             '<br><b>w a m g</b> tag the selected step: wrong, avoidable, missing, good<br><b>⇧ + w…</b> extend the range'
             '<br><b>?</b> this help</div>')


def page(title: str, active: str, body: str, repo: str | None, refresh_js: str = "", slug: str | None = None) -> str:
    q = _q(repo)
    waiting = _waiting(slug or repo)
    links = ""
    for k, n in SCREENS:
        badge = f'<span class="n">{waiting}</span>' if k == "review" and waiting else ""
        cur = ' class="on" aria-current="page"' if k == active else ""
        links += f'<a{cur} href="/{k}{q}">{n}{badge}</a>'
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{e(title)} · once</title><style>{CSS}</style></head><body><div class=app>"
            f'<aside><a class="brand" href="/live{q}">{LOGO}<span>once</span></a><nav aria-label="Screens">{links}</nav>'
            f'<div class="foot">127.0.0.1 · local only<br>nothing leaves this machine<br>press ? for keys</div></aside>'
            f"<main>{body}</main></div>{KEYS_HELP}{KEYS_JS}{refresh_js}</body></html>")


def head(screen: str, summary: str = "", hint: str = "", repos: str = "", stamp: str | None = None) -> str:
    return (f'<div class="top"><div><h1>{e(screen)}</h1>' + (f'<div class="sum">{summary}</div>' if summary else "")
            + f"</div>{repos}</div>" + (f'<p class="hint">{hint}</p>' if hint else ""))


def repo_bar(con, repo: str | None, base: str, keep: dict | None = None, allow_all: bool = True) -> str:
    """A repo picker: a plain GET form, so it works without JavaScript; with it, picking a repo goes there at once."""
    found = [r[0] for r in con.execute("SELECT DISTINCT repo FROM sessions WHERE repo NOT LIKE 'replay/%' ORDER BY repo")]
    if repo and repo not in found:
        found.append(repo)
    opts = (f'<option value=""{" selected" if not repo else ""}>All repos</option>' if allow_all else "") + "".join(
        f'<option value="{e(r)}"{" selected" if r == repo else ""}>{e(r)}</option>' for r in found)
    hidden = "".join(f'<input type=hidden name="{e(k)}" value="{e(v)}">' for k, v in (keep or {}).items() if v)
    return (f'<form class="repopick" method=get action="{e(base)}">{hidden}<label for="repo-pick">Repo</label>'
            f'<select id="repo-pick" name=repo onchange="this.form.submit()">{opts}</select>'
            '<noscript><button>Go</button></noscript></form>')


def window(title: str, body: str, right: str = "", foot: str = "", pad: bool = True) -> str:
    return (f'<section class="win"><div class="bar"><span>{e(title)}</span><span class="right">{e(right)}</span></div>'
            f'<div class="{"pad" if pad else ""}">{body}</div>{foot}</section>')


def statusline(slug: str | None, flagged: int | None = None) -> str:
    if not slug:
        return ""
    try:
        rs = rules.load_all(slug)
    except Exception:
        rs = []
    active = sum(1 for r in rs if r.get("status") == "active")
    wait = sum(1 for r in rs if r.get("status") == "candidate")
    parts = [f'<span class="amb">once ●</span>', plural(active, "rule")]
    if wait:
        parts.append(f'<span class="amb">{wait} to review</span>')
    if flagged:
        parts.append(f'<span class="rose">{plural(flagged, "step")} flagged</span>')
    return '<div class="status">' + '<span class="sep">·</span>'.join(parts) + "</div>"


def _step_detail(s: dict) -> str:
    kind = s.get("tool", "")
    out = []
    if s["event"] == "user_prompt":
        out.append(f'<pre>{e(s.get("prompt", ""))}</pre>')
    else:
        if kind == "shell":
            out.append(f'<div class="lab">command</div><pre>{e(s.get("command") or s.get("target", ""))}</pre>')
        else:
            out.append(f'<div class="lab">{e(kind or "target")}</div><pre>{e(s.get("target", ""))}</pre>')
        if s.get("ok") is False:
            out.append(f'<div class="lab">error{" · " + e(s["error_head"]) if s.get("error_head") else ""}</div>'
                       f'<pre class="err">{e(s.get("error") or "(no error text recorded)")}</pre>')
        if kind in ("edit", "write"):
            out.append('<p class="faint">once keeps a fingerprint of each edit, not its content. '
                       '<a href="#diff">See what the session changed</a>.</p>')
        paths_ = [p for p in s.get("paths", []) if p]
        if paths_ and kind != "edit":
            out.append(f'<div class="lab">files</div><div class="mono">{e(", ".join(paths_[:12]))}</div>')
    meta = [x for x in (s.get("tool_name"), time.strftime("%H:%M:%S", time.localtime(s["ts"])) if s.get("ts") else None) if x]
    if meta:
        out.append(f'<div class="faint mono" style="font-size:12px;margin-top:8px">{e(" · ".join(meta))}</div>')
    return "".join(out)


def _row(s: dict, cls: str, right: str, kind: str, text: str) -> str:
    n = s["step"]
    return (f'<details class="stp" id="s{n}"><summary class="st {cls}" data-k data-step="{n}" title="{e(text)}"><span class="num">{n}</span>'
            f'<span class="d"></span><span class="k">{e(kind)}</span><span class="t">{e(text)}</span>'
            + (f'<span class="r">{e(right)}</span>' if right else "")
            + f'</summary><div class="stx">{_step_detail(s)}</div></details>')


def timeline_html(evs: list[dict], flags: list[dict], notes: list[dict], live: bool = False) -> str:
    st = detectors.steps(evs)
    starts = {f["step_from"]: f for f in flags}
    tagged = {s for n in notes for s in range(n["step_from"], n["step_to"] + 1)}
    out, open_flag = [], None

    def close(f):
        out.append(f'<div class="flagnote"><span class="det">{e(f["detector"].replace("_", " "))}</span>: '
                   f'{e(f.get("note", ""))}</div></div>')
    for i, s in enumerate(st):
        n = s["step"]
        if open_flag is None and n in starts:
            open_flag = starts[n]
            out.append('<div class="flagbox">')
        if s["event"] == "user_prompt":
            kind, text, cls = "prompt", s.get("prompt", ""), "prompt"
        else:
            kind, text, cls = s.get("tool", ""), s.get("target", ""), ""
        right = ""
        if s.get("ok") is False:
            cls += " bad"
            right = f"failed {s.get('error_head', '')}".strip()
        if n in tagged:
            cls += " tagged"
        if live and i == len(st) - 1:
            cls += " now"
            right = right or "now"
        out.append(_row(s, cls, right, kind, text))
        if open_flag is not None and n >= open_flag["step_to"]:
            close(open_flag)
            open_flag = None
    if open_flag is not None:
        close(open_flag)
    return f'<div class="tl">{"".join(out)}</div>' if out else '<div class="empty">No steps yet.</div>'


# ---------------------------------------------------------------- make a rule from a flagged step

def _bare(cmd: str) -> str:
    """A command without its file arguments: `python -m pytest tests/x.py -q` -> `python -m pytest`."""
    out = []
    for tok in cmd.split():
        if "/" in tok or re.search(r"\.\w{1,5}$", tok) or (out and tok.startswith("-") and len(out) >= 2 and not out[-1].startswith("-")):
            break
        out.append(tok)
    return " ".join(out) or cmd


def suggest_rule(evs: list[dict], f: dict) -> tuple[str, str]:
    """A first wording for a rule that would have avoided a flagged range, and the error signature it targets."""
    st = detectors.steps(evs)
    bad = [s for s in st if f["step_from"] <= s["step"] <= f["step_to"] and s.get("ok") is False]
    first = bad[0] if bad else None
    sig = re.escape(first["error_head"]) if first and first.get("error_head") else ""
    if f["detector"] == "wandering_search":
        edit = next((s for s in st if s["step"] > f["step_to"] and s.get("tool") == "edit" and s.get("paths")), None)
        where = str(Path(edit["paths"][0]).parent) + "/" if edit else "the right folder"
        return f"For tasks like this, start by reading {where}", ""
    if not first:
        return "", ""
    cmd = first.get("command") or first.get("target") or ""
    fixed = next((s for s in st if s["step"] > f["step_to"] and s.get("tool") == first.get("tool")
                  and s.get("ok") is True and s.get("tool") == "shell"), None)
    good = _bare((fixed.get("command") or fixed.get("target") or "") if fixed else "")
    cmd = _bare(cmd)
    if good and good.split()[:2] != cmd.split()[:2]:
        return f"Use {good} instead of {cmd}", sig
    if f["detector"] == "retry_cluster":
        return f"When {cmd} fails with {first.get('error_head') or 'an error'}, read the error and fix its cause before running it again", sig
    return f"Do not run {cmd} here; it fails with {first.get('error_head') or 'an error'}", sig


def _make_rule_form(sid: str, sentence: str, sig: str, slug: str) -> str:
    return (f'<details class="mk"><summary>Make this a rule</summary>'
            f'<form method=post action="/teach"><input type=hidden name=token value="{TOKEN}">'
            f'<input type=hidden name=slug value="{e(slug)}"><input type=hidden name=sid value="{e(sid)}">'
            f'<input type=hidden name=signature value="{e(sig)}">'
            f'<label class="lbl">The rule, as your agent will read it</label>'
            f'<input type=text name=sentence value="{e(sentence)}" placeholder="what the agent should do instead">'
            f'<label class="lbl">Only for these files (optional)</label><input type=text name=path placeholder="src/api/**">'
            f'<div class="actions"><button class="pri">Save rule</button></div></form></details>')


# ---------------------------------------------------------------- what a session changed

def _git(root: str, *a, limit: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, timeout=20)


def session_diff(row) -> str:
    cwd = row["cwd"] if "cwd" in row.keys() else None
    if not cwd or not Path(cwd).is_dir():  # e.g. a path the secret masker took for a token: use the known checkout
        from . import repo as _repo
        known = _repo.known_root(row["repo"]) if row["repo"] else None
        cwd = str(known) if known else cwd
    if not cwd or not Path(cwd).is_dir():
        return window("What the session changed", '<div class="empty">The repo folder for this session is not on this machine.</div>')
    root = _git(cwd, "rev-parse", "--show-toplevel").stdout.strip()
    if not root:
        return ""
    start, end = snapshot.ref(row["id"], "start"), snapshot.ref(row["id"], "end")
    have = lambda r: _git(root, "rev-parse", "-q", "--verify", r).returncode == 0
    if not have(start):
        return window("What the session changed", '<div class="empty">No snapshot was taken for this session. '
                      'Snapshots are on by default from 0.5; <code>once config snapshots</code> shows the setting.</div>')
    span, label = ([start, end], "start → end") if have(end) else ([start], "start → now, tracked files")
    stat = _git(root, "diff", "--numstat", *span).stdout.splitlines()
    if not stat:
        return f'<div id="diff">{window("What the session changed", "<div class=empty>No file changes.</div>", label)}</div>'
    patch = _git(root, "diff", "--no-color", "-U3", *span).stdout
    clipped = len(patch) > 200_000
    patch = redact.redact_text(patch[:200_000])[0]
    blocks = {}
    for chunk in re.split(r"(?m)^(?=diff --git )", patch):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", chunk)
        if m:
            blocks[m.group(2)] = chunk
    files, adds, dels = "", 0, 0
    for i, line in enumerate(stat[:60]):
        a_, d_, path = (line.split("\t") + ["", "", ""])[:3]
        adds += int(a_) if a_.isdigit() else 0
        dels += int(d_) if d_.isdigit() else 0
        body = ""
        for ln in blocks.get(path, "").splitlines()[4:]:
            c = "a" if ln.startswith("+") else "r" if ln.startswith("-") else "h" if ln.startswith("@@") else ""
            body += f'<div class="{c}">{e(ln) or " "}</div>'
        files += (f'<details class="fd"{" open" if len(stat) <= 3 else ""}><summary><span class="p">{e(path)}</span>'
                  f'<span class="add">+{e(a_)}</span><span class="del">−{e(d_)}</span></summary>'
                  f'<div class="patch">{body or "<div class=h>binary or renamed file</div>"}</div></details>')
    more = f'<div class="empty">{len(stat) - 60} more files not shown.</div>' if len(stat) > 60 else ""
    more += '<div class="empty">The diff was cut at 200 KB.</div>' if clipped else ""
    right = f"{label} · {plural(len(stat), 'file')} · +{adds} −{dels}"
    return f'<div id="diff">{window("What the session changed", f"<div class=files>{files}</div>{more}", right, pad=False)}</div>'


# ---------------------------------------------------------------- did a rule work

def _signature(rule: dict) -> str | None:
    pv = rule.get("provenance") or {}
    return (pv.get("signature") or (pv.get("replay_hint") or {}).get("signature")
            or ((rule.get("evidence") or {}).get("replay") or {}).get("signature")) or None


def rule_impact(con, slug: str, rule: dict) -> dict:
    rid = rule["id"]
    pv = rule.get("provenance") or {}
    created = pv.get("created")
    try:
        t0 = float(pv["created_at"]) if pv.get("created_at") else (time.mktime(time.strptime(created, "%Y-%m-%d")) if created else None)
    except (ValueError, TypeError):
        t0 = None
    sig = _signature(rule)
    try:
        rx = re.compile(sig) if sig else None
    except re.error:
        rx = None
    given, ignored, hit_given = set(), 0, 0
    before, after = [0, 0], [0, 0]
    for r in con.execute("SELECT id, started FROM sessions WHERE repo=? ORDER BY started", (slug,)):
        evs = store.events(con, r["id"])
        got = any(rid in ev.get("rule_ids", []) for ev in evs if ev.get("event") == "rule_injected")
        if got:
            given.add(r["id"])
        ignored += sum(1 for ev in evs if ev.get("event") == "rule_violation" and ev.get("rule_id") == rid)
        if rx is None or t0 is None:
            continue
        hit = any(ev.get("event") == "tool_result" and ev.get("ok") is False
                  and rx.search(f'{ev.get("error_head", "")}\n{ev.get("error", "")}') for ev in evs)
        side = after if (r["started"] or 0) >= t0 else before
        side[0] += 1
        side[1] += 1 if hit else 0
        hit_given += 1 if (hit and got) else 0
    return {"given": len(given), "ignored": ignored, "sig": sig, "tracked": rx is not None and t0 is not None,
            "before": before, "after": after, "hit_given": hit_given, "since": created}


def _plain(sig: str) -> str:
    return re.sub(r"\\(.)", r"\1", sig or "").replace("|", " or ")


def impact_html(im: dict) -> str:
    tiles = (f'<div class="impact"><div><span>Given in</span><b>{plural(im["given"], "session")}</b></div>'
             f'<div><span>Ignored</span><b class="{"rose" if im["ignored"] else ""}">{im["ignored"]}</b></div>'
             f'<div><span>Failure since</span><b class="{"rose" if im["tracked"] and im["after"][1] else "mint" if im["tracked"] and im["after"][0] else ""}">'
             + (f'{im["after"][1]} of {im["after"][0]}' if im["tracked"] else "–") + "</b></div></div>")
    if not im["tracked"]:
        note = ('<p class="faint" style="font-size:13px;margin-top:10px">No failure pattern to track for this rule. Rules made from a '
                'flagged step, or replayed with <code>--signature</code>, track the error they came from.</p>')
        return f'<div class="box"><h3>Did it work?</h3>{tiles}{note}</div>'
    def bar(label, pair):
        n, k = pair
        pct = round(100 * k / n) if n else 0
        return (f'<div class="ba"><span class="faint">{label}</span><span class="track"><span class="fill" style="width:{pct}%"></span></span>'
                f'<span class="mono" style="font-size:12.5px">{k} of {plural(n, "session")}</span></div>')
    note = (f'<p class="faint mono" style="font-size:12px;margin-top:10px">sessions with “{e(_plain(im["sig"]))}”, '
            f'split when the rule was made ({e(im["since"])})</p>')
    if im["hit_given"]:
        note += f'<p class="rose" style="font-size:13px">It still happened in {plural(im["hit_given"], "session")} that were given this rule.</p>'
    return f'<div class="box"><h3>Did it work?</h3>{tiles}{bar("Before", im["before"])}{bar("Since", im["after"])}{note}</div>'


# ---------------------------------------------------------------- setup check

_HEALTH = {"t": 0.0, "slug": None, "v": []}


def health_html(slug: str | None) -> str:
    if not slug:
        return ""
    if _HEALTH["slug"] != slug or time.time() - _HEALTH["t"] > 30:
        try:
            from . import cli
            _HEALTH.update(t=time.time(), slug=slug, v=cli.health(slug, timing=False))
        except Exception as ex:  # the dashboard must not break because a check did
            _HEALTH.update(t=time.time(), slug=slug, v=[("checks", False, f"could not run: {ex}")])
    soft = ("capture", "codex capture")
    rows, bad = "", 0
    for name, ok, detail in _HEALTH["v"]:
        mark = '<span class="mint">✓</span>' if ok else ('<span class="faint">–</span>' if name in soft else '<span class="rose">✕</span>')
        bad += 0 if ok or name in soft else 1
        rows += f'<div>{mark}<span>{e(name)}</span><span class="d" title="{e(detail)}">{e(detail)}</span></div>'
    fix = ('<p class="rose" style="font-size:13px;margin-top:10px">Something needs fixing. Run <code>once doctor --fix</code> '
           'in this repo.</p>') if bad else ""
    return f'<div class="box"><h3>Setup</h3><div class="checks">{rows}</div>{fix}</div>'



def _when(ts) -> str:
    return time.strftime("%b %d %H:%M", time.localtime(ts or 0))


def _session_paths(con, sid) -> set:
    return {p for ev in store.events(con, sid) for p in ev.get("paths", [])}


def view_sessions(con, repo, path: str | None = None, flagged_only: bool = False, lab: bool = False):
    rows = [r for r in store.sessions(con, repo, 200) if not (r["repo"] or "").startswith("replay/")]
    filt = []
    if flagged_only:
        rows = [r for r in rows if r["flags"]]
        filt.append("with flagged steps")
    if lab:
        rows = [r for r in rows if any(ev.get("event") == "outcome" for ev in store.events(con, r["id"]))]
        filt.append("lab runs")
    if path:
        rows = [r for r in rows if any(p.startswith(path) for p in _session_paths(con, r["id"]))]
        filt.append(f"touching {path}")
    flagged = sum(1 for r in rows if r["flags"])
    show_repo = not repo
    trs = ""
    for r in rows:
        href = f'/session/{urllib.parse.quote(r["id"])}'
        dot = '<span class="dot rose"></span>' if r["flags"] and not r["tags"] else ""
        num = lambda v: f'{v}' if v else '<span class="zero">0</span>'
        trs += (f'<tr data-k data-href="{href}" onclick="location.href=\'{href}\'"><td class="when"><a href="{href}">{e(_when(r["started"]))}</a></td>'
                f'<td><span class="chip">{e(r["agent"])}</span></td>'
                + (f'<td class="n opt">{e(r["repo"])}</td>' if show_repo else "")
                + f'<td class="p">{e(detectors._short(r["prompt"], 90)) or "<span class=zero>no prompt</span>"}</td>'
                f'<td class="n">{num(r["steps"])}</td><td class="n">{num(r["flags"])}{dot}</td>'
                f'<td class="n opt">{num(r["tags"])}</td><td class="n opt">{num(r["rules_given"])}</td></tr>')
    table = ('<table class="rows"><thead><tr><th>When</th><th>Agent</th>' + ('<th class="opt">Repo</th>' if show_repo else "")
             + '<th>Prompt</th><th>Steps</th><th>Flags</th><th class="opt">Tags</th><th class="opt">Rules</th></tr></thead>'
             f"<tbody>{trs}</tbody></table>")
    empty = '<div class="empty">No sessions captured yet. Start your agent in a repo where you ran <code>once init</code>.</div>'
    summary = f"{plural(len(rows), 'session')} · {flagged} with flagged steps" if rows else "None yet"
    hint = (f"Showing sessions {', '.join(e(x) for x in filt)}. <a href='/sessions{_q(repo)}'>Show all</a>" if filt else "")
    body = (head("Sessions", e(summary), hint, repo_bar(con, repo, "/sessions"))
            + window("All sessions", table if rows else empty, f"newest first · {len(rows)} shown", pad=False))
    return page("Sessions", "sessions", body, repo)


def _tag_form(sid, lo, hi, det="", pre="avoidable", fixed=False, form_id=""):
    picks = "".join(
        f'<input type=radio name=tag value="{k}" id="t{k}{lo}{det}"{" checked" if k == pre else ""}>'
        f'<label for="t{k}{lo}{det}">{n}</label>' for k, n in TAGS)
    return (f'<form method=post action="/tag"{f" id={form_id}" if form_id else ""}><input type=hidden name=token value="{TOKEN}">'
            f'<input type=hidden name=sid value="{e(sid)}"><input type=hidden name=detector value="{e(det)}">'
            + (f'<input type=hidden name=lo value="{lo}"><input type=hidden name=hi value="{hi}">' if fixed else
               f'<div class="steps">Steps <input class="num" type=text name=lo value="{lo}" aria-label="From step"> to '
               f'<input class="num" type=text name=hi value="{hi}" aria-label="To step"></div>')
            + f'<div class="pick" role="radiogroup" aria-label="Tag">{picks}</div>'
            f'<label class="lbl" for="note{lo}{det}">What should it have done?</label>'
            f'<input type=text id="note{lo}{det}" name=note placeholder="e.g. run tox -e py, not pytest">'
            f'<div class="actions"><button class="pri">Save tag</button></div></form>')


def _session_pr_box(row, evs) -> str:
    from . import repo as repo_
    try:
        pr, how = prs.link(row["repo"] or "", row["id"], evs, row["started"], prs.cached(row["repo"] or "")[0],
                           repo_.known_root(row["repo"] or ""))
    except Exception:
        pr, how = None, ""
    back = f"/session/{urllib.parse.quote(row['id'])}"
    hidden = (f'<input type=hidden name=token value="{TOKEN}"><input type=hidden name=sid value="{e(row["id"])}">'
              f'<input type=hidden name=back value="{e(back)}">')
    by_hand = row["id"] in prs.manual()
    if pr:
        body = (f'<p>{_pr_chip(pr)} <a href="{e(pr["url"])}" target="_blank" rel="noopener">#{e(pr.get("number"))} '
                f'{e(pr.get("title") or "")}</a></p><p class="faint" style="font-size:13px">Linked by {e(how)}.</p>'
                + (f'<form method=post action="/prs/link" class="actions">{hidden}<button name=action value="none">Not this PR</button>'
                   + ('<button name=action value="auto">Link automatically</button>' if by_hand else "") + "</form>"))
    else:
        body = ('<p>No PR found for this session yet.</p>' + (
            f'<form method=post action="/prs/link" class="actions">{hidden}<button name=action value="auto">Link automatically</button></form>'
            if by_hand else ""))
    return (f'<div class="box"><h3>Pull request</h3>{body}<div style="margin-top:10px">'
            f'{_link_form(row["id"], back, label="Link another" if pr else "Link")}</div></div>')


def view_session(con, sid):
    row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        return None
    evs = store.events(con, sid)
    flags, notes = detectors.detect(evs), detectors.annotations(evs)
    given = list(dict.fromkeys(rid for ev in evs if ev.get("event") == "rule_injected" for rid in ev.get("rule_ids", [])))
    side = ""
    oc = next((ev for ev in evs if ev.get("event") == "outcome"), None)
    if oc:
        tone, v = {True: ("mint", "Passed the pull request's tests"), False: ("rose", "Failed the pull request's tests"),
                   None: ("", "Not checked")}[oc.get("verified")]
        side += (f'<div class="box"><h3>Lab run · task {e(oc.get("task"))} · {e(oc.get("arm"))} arm</h3>'
                 f'<p><span class="chip {tone}">{v}</span>{" &nbsp;<span class=mono>$%.2f</span>" % oc["cost_usd"] if oc.get("cost_usd") else ""}</p>'
                 + (f'<p>The real fix also changed <span class="mono" style="color:var(--ink)">{e(", ".join(oc["missed_files"]))}</span>.</p>' if oc.get("missed_files") else "")
                 + ('<p>The real fix added tests; the agent wrote none.</p>' if oc.get("wrote_tests") is False else "") + "</div>")
    for f in flags:
        side += (f'<div class="box"><h3>Steps {f["step_from"]} to {f["step_to"]}</h3>'
                 f'<p class="rose">{e(f["detector"].replace("_", " ").capitalize())}: {e(f["note"])}</p>'
                 f'<div style="margin-top:4px">{_tag_form(sid, f["step_from"], f["step_to"], f["detector"], fixed=True)}</div>'
                 + (_make_rule_form(sid, *suggest_rule(evs, f), row["repo"]) if f["detector"] != "ignored_rule" else
                    '<p class="faint" style="font-size:13px;margin-top:10px">The agent was given this rule and broke it anyway. '
                    'Tag it so the rule can be reworded or enforced.</p>')
                 + "</div>")
    side += (f'<details class="box"{"" if flags else " open"}><summary><h3 style="display:inline">Tag other steps</h3></summary>'
             f'<p>Tags are the strongest signal once learns from. Keys: j/k to a step, then w, a, m or g.</p><div style="margin-top:14px">{_tag_form(sid, "", "", pre="wrong", form_id="tagform")}</div></details>')
    if notes:
        side += '<div class="box"><h3>Tags on this session</h3>' + "".join(
            f'<p><span class="chip amb">{e(n.get("tag", "").replace("_", " "))}</span> steps {n["step_from"]} to {n["step_to"]}'
            f'{" · " + e(n["note"]) if n.get("note") else ""}</p>' for n in notes) + "</div>"
    enf = [ev for ev in evs if ev.get("event") == "rule_enforced"]
    if enf:  # a refused call leaves no step in the timeline
        words = {"block": "refused before it landed", "check": "sent back as an error to fix", "remind": "reminder after the edit"}
        side += '<div class="box"><h3>Rules enforced</h3>' + "".join(
            f'<p><span class="chip amb">{e(ev.get("level"))}</span> <span class="mono">{e(ev.get("rule_id"))}</span>: '
            f'{e(words.get(ev.get("level"), ""))}{" · " + e(ev["path"]) if ev.get("path") else ""}</p>' for ev in enf) + "</div>"
    side = _session_pr_box(row, evs) + side
    blockers = [ev for ev in evs if ev.get("event") == "blocker"]
    if blockers:
        side += '<div class="box"><h3>Blockers the agent recorded</h3>' + "".join(f'<p>{e(b.get("note"))}</p>' for b in blockers) + "</div>"
    dur = max(1, round(((row["last_event"] or 0) - (row["started"] or 0)) / 60))
    pct = round(100 * detectors.avoidable_ratio(evs, flags))
    title = e(detectors._short(row["prompt"], 110)) or "No prompt"
    lede = (f"{e(row['agent'])} · {e(time.strftime('%b %d %H:%M', time.localtime(row['started'] or 0)))} · {dur} min · {plural(row['steps'] or 0, 'step')}"
            + (f" · <span class='rose'>{pct}% avoidable</span>" if flags else "")
            + (f" · rules given: <span class='mono'>{e(', '.join(given))}</span>" if given else " · no rules given"))
    q = _q(row["repo"])
    crumbs = (f'<div class="repos"><a href="/sessions{q}">← sessions</a><span class="sep">·</span>'
              f'<a href="/conversations{_q(row["repo"], sid=row["id"])}">read the conversation</a><span class="sep">·</span><b>{e(row["repo"])}</b></div>')
    body = (head("Session", lede, f'<span style="color:var(--ink)">{title}</span>', crumbs)
            + '<div class="cols"><div>'
            + window("Timeline", timeline_html(evs, flags, notes), plural(row["steps"] or 0, "step"),
                     statusline(row["repo"], sum(f["step_to"] - f["step_from"] + 1 for f in flags)))
            + f"</div><div>{side}</div></div>" + session_diff(row))
    return page("Session", "sessions", body, row["repo"], slug=row["repo"])


def live_fragment(con, repo, sid, cwd_slug=None):
    now = time.time()
    rows = [r for r in store.sessions(con, repo, 50)
            if not r["ended"] and now - (r["last_event"] or 0) < ACTIVE_SECS and not (r["repo"] or "").startswith("replay/")]
    if not rows:
        last = next((r for r in store.sessions(con, repo, 20) if not (r["repo"] or "").startswith("replay/")), None)
        msg = ('<div class="cols"><div>' + window("Waiting for an agent",
               '<div class="empty">Start <code>claude</code>, <code>codex</code> or <code>agy</code> in a connected repo. '
               "The session appears here within a second and fills in as the agent works.</div>", "live",
               statusline(repo)) + "</div><div>")
        if last:
            evs = store.events(con, last["id"])
            flags, notes = detectors.detect(evs), detectors.annotations(evs)
            msg += (f'<div class="box"><h3>Last session · {e(ago(last["last_event"] or last["started"]))}</h3>'
                    f'<p>{e(detectors._short(last["prompt"], 80)) or "No prompt"}</p>'
                    f'<p class="mono faint" style="font-size:13px">{e(last["agent"])} · {plural(last["steps"] or 0, "step")} · '
                    f'{plural(len(flags), "flag")}</p><div class="actions"><a href="/session/{urllib.parse.quote(last["id"])}">'
                    f'<button>Open it</button></a></div></div>')
        return msg + health_html(repo or cwd_slug) + "</div></div>"
    sel = next((r for r in rows if r["id"] == sid), rows[0])
    cards = ""
    for r in rows:
        evs = store.events(con, r["id"])
        st = detectors.steps(evs)
        doing = detectors._short(st[-1].get("target") or st[-1].get("prompt"), 40) if st else "starting"
        cards += (f'<a class="card{" on" if r["id"] == sel["id"] else ""}" data-k href="/live{_q(repo, sid=r["id"])}">'
                  f'<b>{e(r["agent"])}</b>{ROSEDOT if r["flags"] else ""}'
                  f'<span class="meta" style="color:var(--muted);font-family:var(--sans);font-size:14px">{e(detectors._short(r["prompt"], 60))}</span>'
                  f'<span class="meta">step {r["steps"]} · {ago(r["started"])} · {e(doing)}</span></a>')
    evs = store.events(con, sel["id"])
    flags, notes = detectors.detect(evs), detectors.annotations(evs)
    st = detectors.steps(evs)
    files = {p for s in st for p in s.get("paths", [])}
    tiles = (f'<div class="tiles" style="margin:12px 0 0"><div class="tile"><span>Rules given</span><b>{sel["rules_given"] or 0}</b></div>'
             f'<div class="tile"><span>Files touched</span><b>{len(files)}</b></div>'
             f'<div class="tile"><span>Flags</span><b class="{"rose" if flags else ""}">{len(flags)}</b></div></div>')
    detail = window(f"{sel['agent']} · started {time.strftime('%H:%M', time.localtime(sel['started'] or 0))}",
                    timeline_html(evs[-400:], flags, notes, live=True), "live",
                    statusline(sel["repo"], sum(f["step_to"] - f["step_from"] + 1 for f in flags)))
    detail += tiles + (f'<div class="actions"><a href="/session/{urllib.parse.quote(sel["id"])}"><button>Open the full session to tag steps</button></a></div>')
    return f'<div class="cols list"><div>{cards}</div><div>{detail}</div></div>'


def view_live(con, repo, sid, cwd_slug=None):
    now = time.time()
    running = sum(1 for r in store.sessions(con, repo, 50)
                  if not r["ended"] and now - (r["last_event"] or 0) < ACTIVE_SECS and not (r["repo"] or "").startswith("replay/"))
    # A server-sent event stream: the server watches the session files and pushes the view when one changes.
    # Nothing is polled by the page; polling is only the fallback when the stream cannot be opened.
    q = _q(repo, sid=sid)
    js = ("<script>(()=>{const box=document.getElementById('live');"
          "function put(h){const open=[...box.querySelectorAll('details[open]')].map(d=>d.id).filter(Boolean);"
          "box.innerHTML=h;open.forEach(i=>{const d=document.getElementById(i);if(d)d.open=true})}"
          "function poll(){setInterval(async()=>{try{const r=await fetch('/live/fragment%s');if(r.ok)put(await r.text())}catch(e){}},1500)}"
          "if(!window.EventSource){poll();return}let es=new EventSource('/live/stream%s'),fails=0;"
          "es.onmessage=e=>{put(e.data);fails=0};es.onerror=()=>{if(++fails>3){es.close();poll()}}})()</script>" % (q, q))
    summary = (f'<span class="amb">●</span> {plural(running, "session")} running' if running else "Nothing running · updates by itself")
    body = head("Live", summary, "", repo_bar(con, repo, "/live")) + f'<div id="live">{live_fragment(con, repo, sid, cwd_slug)}</div>'
    return page("Live", "live", body, repo, js)


def _evidence(sel) -> str:
    rp = (sel.get("evidence") or {}).get("replay")
    if not rp:
        return (f'<div class="box"><h3>No replay evidence yet</h3><p>A replay runs the agent three times without the rule and three '
                f'times with it, from the same commit. To run one:</p><p class="mono" style="color:var(--ink);font-size:13px">'
                f'once lab replay {e(sel["id"])} --prompt "..."</p></div>')
    runs = int(rp.get("runs") or 3)
    wo, wi = int(rp["without_rule"]["failed"]), int(rp["with_rule"]["failed"])
    lane = lambda failed: "".join('<span class="run x">✕</span>' if k < failed else '<span class="run v">✓</span>' for k in range(runs))
    verdict = (rp.get("verdict") or "").replace("_", " ")
    tone = "mint" if wo >= 2 and wi == 0 else ("rose" if wi > wo else "")
    return (f'<div class="box replay"><div style="display:flex;align-items:center;justify-content:space-between;gap:12px">'
            f'<h3>Replay, {runs} runs each way</h3><span class="chip {tone}">{e(verdict or "done")}</span></div>'
            f'<p>Same commit, same prompt.</p>'
            f'<div class="lane"><span>Without the rule</span>{lane(wo)}</div>'
            f'<div class="lane"><span>With the rule</span>{lane(wi)}</div>'
            f'<div class="facts"><span>failed {wo} of {runs} without</span><span>failed {wi} of {runs} with</span>'
            f'<span>commit {e(rp.get("commit"))}</span><span>{e(rp.get("date"))}</span></div></div>')


def _usage(con, slug) -> dict:
    """Per rule: sessions it was given in, edits that went against it (the ignored-rule detector), times enforced,
    and when it was last given."""
    out = {}
    for row in con.execute("SELECT e.raw, s.started FROM events e JOIN sessions s ON s.id = e.session_id WHERE s.repo=? "
                           "AND e.event IN ('rule_injected','rule_violation','rule_enforced')", (slug,)):
        ev = json.loads(row["raw"])
        ids = ev.get("rule_ids", []) if ev.get("event") == "rule_injected" else [ev.get("rule_id")]
        for i in ids:
            u = out.setdefault(i, {"fired": 0, "ignored": 0, "enforced": 0, "last": 0})
            if ev["event"] == "rule_injected":
                u["fired"] += 1
                u["last"] = max(u["last"], row["started"] or 0)
            else:
                u["ignored" if ev["event"] == "rule_violation" else "enforced"] += 1
    return out


LEVELS = ("instruct", "remind", "check", "block")


def view_rules(con, repo, rid, cwd_slug, only_candidates=False, msg: str | None = None):
    slug = repo or cwd_slug
    all_rules = [r for r in rules.all_levels(slug) if r.get("status") != "deprecated"
                 and (not only_candidates or r.get("status") == "candidate")]
    use = _usage(con, slug)
    fired = {k: v["fired"] for k, v in use.items()}
    month = time.time() - 30 * 86400
    sel = next((r for r in all_rules if r.get("id") == rid), all_rules[0] if all_rules else None)
    route = "review" if only_candidates else "rules"
    lst = ""
    for r in all_rules:
        gl = (r.get("scope") or {}).get("path_globs") or []
        src = (r.get("provenance") or {}).get("source_kind")
        u = use.get(r["id"], {})
        try:
            made = time.mktime(time.strptime(str((r.get("provenance") or {}).get("created"))[:10], "%Y-%m-%d"))
        except Exception:
            made = time.time()
        stale = r.get("status") == "active" and made < month and (u.get("last") or 0) < month
        src_txt = {"manual": "taught by you", "chat": "from your chat", "missed_file": "from files the agent missed",
                   "retry_cluster": "from repeated failures", "tag": "from your tags", "pr_review": "from review comments",
                   "human_tag": "from your tags", "bootstrap": "from the repo's config"}.get(src, src or "")
        lvl = r.get("_level", "repo")
        if lvl != "repo":
            src_txt = f"{lvl} level" + (f" ({r['_level_key']})" if r.get("_level_key") else "")
        lst += (f'<a class="card{" on" if sel and r["id"] == sel["id"] else ""}" data-k href="/{route}{_q(repo, id=r["id"])}">'
                f'<b>{e(r["id"])}</b>{"<span class=dot></span>" if r.get("status") == "candidate" else ""}'
                + (STALE_DOT if stale else "")
                + f'<span class="meta" style="font-family:var(--sans);font-size:14px;color:var(--muted)">{e(detectors._short((r.get("rule") or {}).get("directive"), 90))}</span>'
                f'<span class="meta">{e(", ".join(gl) if gl else "whole repo")} · {e(src_txt)} · given in {plural(fired.get(r["id"], 0), "session")}'
                + (f' · <span class="rose">ignored in {u["ignored"]}</span>' if u.get("ignored") else "")
                + (" · stale" if stale else "") + '</span></a>')
    teach = (f'<div class="box"><h3>Add a rule</h3><p>It is active from your next session. No review needed.</p>'
             f'<form method=post action="/teach"><input type=hidden name=token value="{TOKEN}"><input type=hidden name=slug value="{e(slug)}">'
             f'<label class="lbl" for="sentence">The rule</label><input type=text id=sentence name=sentence placeholder="always use uv run, never bare pip">'
             f'<label class="lbl" for="glob">Only for these files (optional)</label><input type=text id=glob name=path placeholder="src/api/**">'
             f'<div class="actions"><button class="pri">Teach</button></div></form></div>')
    detail = ""
    if sel:
        pv = sel.get("provenance") or {}
        rule = sel.get("rule") or {}
        who = pv.get("approved_by") or ""
        detail = (f'<div class="box"><div class="rid">{e(sel["id"])}</div>'
                  f'<div class="directive">{e(rule.get("directive"))}</div>'
                  + (f'<p>Why: {e(rule["rationale"])}</p>' if rule.get("rationale") else "")
                  + f'<div class="facts"><span>level {e((sel.get("enforcement") or {}).get("level", "instruct"))}</span>'
                  f'<span>source {e(pv.get("source_kind"))}</span>' + (f"<span>by {e(who)}</span>" if who else "")
                  + f'<span>{e(pv.get("created"))}</span><span>seen in {plural(fired.get(sel["id"], 0), "session")}, '
                  f'ignored in {use.get(sel["id"], {}).get("ignored", 0)}</span>'
                  + (f'<span>enforced {use[sel["id"]]["enforced"]}x</span>' if use.get(sel["id"], {}).get("enforced") else "")
                  + (f'<span>{e(sel["_level"])} level</span>' if sel.get("_level", "repo") != "repo" else "") + '</div>')
        if sel.get("status") == "candidate":
            detail += (f'<form method=post action="/review" style="margin-top:16px"><input type=hidden name=token value="{TOKEN}">'
                       f'<input type=hidden name=slug value="{e(slug)}"><input type=hidden name=id value="{e(sel["id"])}">'
                       f'<label class="lbl" for="text">Wording, edit before accepting if you like</label>'
                       f'<input type=text id=text name=text value="{e(rule.get("directive"))}">'
                       f'<label class="lbl" for="reason">If rejecting, why? It teaches the next drafts</label>'
                       f'<input type=text id=reason name=reason placeholder="a one-off, not a convention">'
                       f'<div class="actions"><button class="pri" name=decision value="accept">Accept</button>'
                       f'<button name=decision value="reject">Reject</button></div></form></div>')
        else:
            cur = (sel.get("enforcement") or {}).get("level", "instruct")
            nxt_ = LEVELS[min(LEVELS.index(cur) + 1, len(LEVELS) - 1)] if cur in LEVELS else "remind"
            hid = (f'<input type=hidden name=token value="{TOKEN}"><input type=hidden name=slug value="{e(slug)}">'
                   f'<input type=hidden name=id value="{e(sel["id"])}">')
            detail += (f'<form method=post action="/rules/edit" style="margin-top:14px">{hid}'
                       f'<label class="lbl" for="wording">Wording</label>'
                       f'<input type=text id=wording name=text value="{e(rule.get("directive"))}">'
                       f'<div class="actions"><button>Save wording</button></div></form>'
                       f'<div class="actions">'
                       + (f'<form method=post action="/rules/promote">{hid}<button title="needs a detector; '
                          f'check and block also need the agent to have ignored it">Promote to {nxt_}</button></form>'
                          if cur != "block" else "")
                       + f'<form method=post action="/rules/drop">{hid}<button>Retire</button></form></div></div>')
        detail += impact_html(rule_impact(con, slug, sel)) + _evidence(sel)
    crumbs = repo_bar(con, slug, "/review" if only_candidates else "/rules", allow_all=False) if slug else ""
    if msg:
        detail = f'<div class="msg">{e(msg)}</div>' + detail
    if only_candidates and not all_rules:
        body = (head("Review", "Nothing waiting", "No suggested rules waiting. Suggestions arrive after "
                     "<code class='mono'>once learn</code>, or when you correct your agent in chat.", crumbs)
                + f'<div class="cols even"><div></div><div>{teach}</div></div>')
        return page("Review", "review", body, repo, slug=slug)
    if only_candidates:
        title = f"{plural(len(all_rules), 'suggestion')} waiting"
        lede = ""
    else:
        active = sum(1 for r in all_rules if r.get("status") == "active")
        title = f"{active} active · {len(all_rules) - active} suggested" if all_rules else "No rules yet"
        lede = "" if all_rules else "Teach one below, or correct your agent in chat and once will suggest one."
    body = (head("Review" if only_candidates else "Rules", e(title), lede, crumbs)
            + f'<div class="cols list"><div>{lst or "<div class=empty>No rules yet.</div>"}</div><div>{detail}{teach}</div></div>')
    return page("Review" if only_candidates else "Rules", route, body, repo, slug=slug)


def _window(con, repo, lo, hi):
    """Numbers for sessions started in [lo, hi): count, flagged, avoidable %, verified %, cost per verified."""
    rows = [r for r in store.sessions(con, repo, 5000)
            if lo <= (r["started"] or 0) < hi and not (r["repo"] or "").startswith("replay/")]
    ratios, ocs = [], []
    for r in rows:
        evs = store.events(con, r["id"])
        ratios.append(detectors.avoidable_ratio(evs, detectors.detect(evs)))
        ocs += [ev for ev in evs if ev.get("event") == "outcome" and ev.get("verified") is not None]
    passed = sum(1 for o in ocs if o["verified"])
    cost = sum(o.get("cost_usd") or 0 for o in ocs)
    return {"rows": rows, "sessions": len(rows), "flagged": sum(1 for r in rows if r["flags"]),
            "avoidable": 100 * sum(ratios) / len(ratios) if ratios else None,
            "verified": 100 * passed / len(ocs) if ocs else None,
            "cpv": cost / passed if passed and cost else None}


def _delta(cur, prev, unit="", lower_is_better=False, money=False):
    if cur is None or prev is None:
        return '<small>no earlier period to compare</small>' if prev is None and cur is not None else "<small>&nbsp;</small>"
    d = cur - prev
    if abs(d) < 0.5 and not money or money and abs(d) < 0.005:
        return "<small>same as the 30 days before</small>"
    good = (d < 0) == lower_is_better
    txt = (f"{'+' if d > 0 else '−'}${abs(d):.2f}" if money else f"{'+' if d > 0 else '−'}{abs(d):.0f}{unit}")
    return f'<small class="{"down" if good else "up"}">{txt} vs the 30 days before</small>'


def _trend_svg(points, label):
    """One line, drawn by the server. points: [(week label, value or None)]."""
    W, H, P = 600, 150, 26
    vals = [v for _, v in points if v is not None]
    if not vals:
        return '<div class="faint">Not enough sessions yet to draw a trend.</div>'
    top = max(10.0, max(vals) * 1.15)
    n = len(points)
    x = lambda i: P + (W - 2 * P) * (i / max(n - 1, 1))
    y = lambda v: H - P - (H - 2 * P) * (v / top)
    pts = [(x(i), y(v)) for i, (_, v) in enumerate(points) if v is not None]
    color = "var(--rose)" if label == "avoidable" else "var(--mint)"
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{e(label)} by week">',
           f'<line class="ax" x1="{P}" y1="{H - P}" x2="{W - P}" y2="{H - P}"/>',
           f'<line class="ax" x1="{P}" y1="{y(top / 2):.1f}" x2="{W - P}" y2="{y(top / 2):.1f}" stroke-dasharray="3 4"/>',
           f'<text class="lbl" x="{P}" y="{y(top / 2) - 4:.1f}">{top / 2:.0f}%</text>']
    if len(pts) > 1:
        out.append(f'<polyline class="ln" stroke="{color}" points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in pts)}"/>')
    for i, (wk, v) in enumerate(points):
        if v is not None:
            out.append(f'<circle class="pt" cx="{x(i):.1f}" cy="{y(v):.1f}" r="4" fill="{color}"><title>{e(wk)}: {v:.0f}%</title></circle>')
        if i % max(1, n // 6) == 0 or i == n - 1:
            out.append(f'<text class="lbl" x="{x(i):.1f}" y="{H - 8}" text-anchor="middle">{e(wk)}</text>')
    return "".join(out) + "</svg>"


def view_overview(con, repo, trend="avoidable"):
    now = time.time()
    cur, prev = _window(con, repo, now - 30 * 86400, now + 1), _window(con, repo, now - 60 * 86400, now - 30 * 86400)
    rows = cur["rows"]
    hot, by_agent = {}, {}
    for r in rows:
        evs = store.events(con, r["id"])
        flags = detectors.detect(evs)
        ratio = detectors.avoidable_ratio(evs, flags)
        a = by_agent.setdefault(r["agent"], [0, 0.0, 0])
        a[0] += 1
        a[1] += ratio
        a[2] += len(flags)
        st = {s["step"]: s for s in detectors.steps(evs)}
        for f in flags:
            paths_ = [p for s in range(f["step_from"], f["step_to"] + 1) for p in st[s].get("paths", [])]
            key = (str(Path(paths_[0]).parent) + "/") if paths_ else "(no file)"
            h = hot.setdefault(key, {})
            h[f["detector"]] = h.get(f["detector"], 0) + 1
    fmt = lambda v, suffix="%": "–" if v is None else f"{v:.0f}{suffix}"
    active = len(rules.active(repo)) if repo else "–"
    has_prev = prev["sessions"] > 0
    pv = lambda k: prev[k] if has_prev else None
    tile = lambda label, val, delta, href, cls="": (
        f'<div class="tile"><a href="{href}"><span>{label}</span><b class="{cls}">{val}</b>{delta}</a></div>')
    q = _q(repo)
    sep = "&" if q else "?"
    tiles = ('<div class="tiles">'
             + tile("Sessions", cur["sessions"], _delta(cur["sessions"], pv("sessions")), f"/sessions{q}")
             + tile("With flags", cur["flagged"], _delta(cur["flagged"], pv("flagged"), lower_is_better=True),
                    f"/sessions{q}{sep}flagged=1")
             + tile("Avoidable steps", fmt(cur["avoidable"]), _delta(cur["avoidable"], pv("avoidable"), " pts", True),
                    f"/sessions{q}{sep}flagged=1", "rose" if cur["avoidable"] else "")
             + tile("Verified (lab runs)", fmt(cur["verified"]), _delta(cur["verified"], pv("verified"), " pts"),
                    f"/sessions{q}{sep}lab=1")
             + tile("Cost per verified", f"${cur['cpv']:.2f}" if cur["cpv"] else "–",
                    _delta(cur["cpv"], pv("cpv"), lower_is_better=True, money=True), f"/sessions{q}{sep}lab=1")
             + tile("Active rules", active, "<small>&nbsp;</small>", f"/rules{q}", "amb") + "</div>")
    # Weekly trend: avoidable steps is the number memory should push down; verified rate can be toggled in.
    weeks = []
    for k in range(11, -1, -1):
        lo = now - (k + 1) * 7 * 86400
        w = _window(con, repo, lo, lo + 7 * 86400)
        weeks.append((time.strftime("%b %d", time.localtime(lo)), w["avoidable" if trend != "verified" else "verified"]))
    seg = (f'<span class="seg"><a class="{"on" if trend != "verified" else ""}" href="/overview{_q(repo, trend="avoidable")}">avoidable steps</a>'
           f'<a class="{"on" if trend == "verified" else ""}" href="/overview{_q(repo, trend="verified")}">verified rate</a></span>')
    trend_html = (f'<section class="win"><div class="bar"><span>By week</span><span class="right">{seg}</span></div>'
                  f'<div class="pad trend">{_trend_svg(weeks, "verified" if trend == "verified" else "avoidable")}</div></section>')
    hot_rows = sorted(hot.items(), key=lambda kv: -sum(kv[1].values()))[:8]
    top = max((sum(v.values()) for _, v in hot_rows), default=1)
    hot_html = "".join(
        f'<a class="row" data-k style="text-decoration:none;color:inherit" href="/sessions{_q(repo, path=k if k != "(no file)" else None)}">'
        f'<span class="mono" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{e(k)}</span>'
        f'<span class="track"><span class="fill" style="display:block;width:{round(100 * sum(v.values()) / top)}%"></span></span>'
        f'<span class="faint" style="font-size:13px">{e(", ".join(f"{n} {d.replace(chr(95), chr(32))}" for d, n in v.items()))}</span></a>'
        for k, v in hot_rows) or '<div class="faint">Nothing flagged yet.</div>'
    ag = "".join(f'<tr data-k data-href="/sessions{q}"><td><span class="chip">{e(k)}</span></td><td class="n">{v[0]}</td>'
                 f'<td class="n">{round(100 * v[1] / v[0])}%</td><td class="n">{v[2]}</td></tr>'
                 for k, v in sorted(by_agent.items(), key=lambda kv: -kv[1][0]))
    agents = ('<table class="rows"><thead><tr><th>Agent</th><th>Sessions</th><th>Avoidable</th><th>Flags</th></tr></thead>'
              f"<tbody>{ag}</tbody></table>") if ag else '<div class="empty">No sessions yet.</div>'
    body = (head("Overview", "Last 30 days, against the 30 before", "Every number opens its sessions. Verified rate and "
                 "cost come from lab runs, where a check exists.", repo_bar(con, repo, "/overview")) + tiles + trend_html
            + '<div class="cols even"><div>' + window("Where agents struggle", f'<div class="bars">{hot_html}</div>', "by folder")
            + "</div><div>" + window("By agent", agents, "same repo, same period", pad=False) + "</div></div>")
    return page("Overview", "overview", body, repo)


# ---------------------------------------------------------------- pull requests and conversations

PR_TONE = {"merged": "mint", "open": "amb", "closed": ""}


def _real_sessions(con, repo, limit=300):
    return [r for r in store.sessions(con, repo, limit) if not (r["repo"] or "").startswith("replay/")]


def _length(r) -> str:
    m = max(0, round(((r["last_event"] or 0) - (r["started"] or 0)) / 60))
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{max(m, 1)}m"


def _pr_links(con, rows, commits: bool = True, every: bool = False):
    """[(row, events, pr or None, how)] for these sessions, from the cache only: GitHub is never waited on.
    every: one entry per PR when a session created several (the PR page); else the latest (one chip)."""
    slugs = sorted({r["repo"] for r in rows if r["repo"]})
    cache = {s: prs.cached(s)[0] for s in slugs}
    roots = {}
    if commits:
        from . import repo as repo_
        roots = {s: repo_.known_root(s) for s in slugs}
    hand = prs.manual()
    out = []
    for r in rows:
        evs = store.events(con, r["id"])
        try:
            found = prs.links(r["repo"] or "", r["id"], evs, r["started"], cache.get(r["repo"], []), roots.get(r["repo"]), hand)
        except Exception:
            found = []
        if every and found:
            out += [(r, evs, pr, how) for pr, how in found]
        else:
            pr, how = found[-1] if found else (None, "")
            out.append((r, evs, pr, how))
    return out


def _pr_chip(pr) -> str:
    st = pr.get("state") or ""
    return f'<span class="chip {PR_TONE.get(st, "")}">{e(st or "PR")}</span>'


def _pr_label(pr) -> str:
    return f"#{pr.get('number')}" + (f" {pr['state']}" if pr.get("state") else "")


def _link_form(sid, back, value="", label="Link") -> str:
    return (f'<form class="inline linkf" method=post action="/prs/link"><input type=hidden name=token value="{TOKEN}">'
            f'<input type=hidden name=sid value="{e(sid)}"><input type=hidden name=back value="{e(back)}">'
            f'<input type=text name=ref value="{e(value)}" placeholder="PR number or link" aria-label="PR number or link">'
            f'<button>{e(label)}</button></form>')


def view_prs(con, repo, state: str | None = None, msg: str | None = None):
    rows = _real_sessions(con, repo)
    slugs = sorted({r["repo"] for r in rows if r["repo"]})
    for s in slugs:
        prs.refresh_async(s)
    linked = _pr_links(con, rows, every=True)
    groups: dict = {}
    loose = []
    for r, evs, pr, how in linked:
        if pr and pr.get("url"):
            g = groups.setdefault(pr["url"].lower(), {"pr": pr, "sessions": [], "last": 0})
            g["sessions"].append((r, how))
            g["last"] = max(g["last"], r["started"] or 0)
        else:
            loose.append(r)
    allg = sorted(groups.values(), key=lambda g: -g["last"])
    count = lambda st: sum(1 for g in allg if g["pr"].get("state") == st)
    shown = [g for g in allg if not state or g["pr"].get("state") == state]
    flagged_merged = sum(r["flags"] or 0 for g in allg if g["pr"].get("state") == "merged" for r, _ in g["sessions"])
    n_linked = len({r["id"] for g in allg for r, _ in g["sessions"]})  # a session can sit under several PRs

    gh = prs.gh_available()
    fetched = [prs.cached(s)[1] for s in slugs if s.startswith("github.com/")]
    errs = [(s, prs.last_error(s)) for s in slugs if s.startswith("github.com/") and prs.last_error(s)]
    if gh and errs:
        status = (f"GitHub did not answer for {e(errs[0][0])}: {e(errs[0][1][0])} · "
                  f"<a href='/prs{_q(repo, state=state, refresh=1)}'>try again</a>")
    elif not gh:
        status = ("<code>gh</code> is not installed, so titles, state and checks are missing. "
                  "PR links the agents printed still show. Install GitHub's CLI and run <code>gh auth login</code>.")
    elif not [f for f in fetched if f]:
        status = "Asking GitHub for this repo's pull requests. Reload in a few seconds." if fetched else ""
    else:
        status = f"Checked GitHub {ago(min(f for f in fetched if f))} · <a href='/prs{_q(repo, state=state, refresh=1)}'>check now</a>"
    tiles = (f'<div class="tiles"><div class="tile"><span>Merged</span><b>{count("merged")}</b></div>'
             f'<div class="tile"><span>Open</span><b>{count("open")}</b></div>'
             f'<div class="tile"><span>Sessions linked to a PR</span><b>{n_linked} of {len(rows)}</b></div>'
             f'<div class="tile"><span>Flagged steps behind merged PRs</span><b class="{"rose" if flagged_merged else ""}">{flagged_merged}</b></div></div>')
    from .collector import shipping
    one = {}  # one row per session for the comparison: a merged PR counts over an open one
    for r, evs, pr, _ in linked:
        if r["id"] not in one or (pr and pr.get("state") == "merged"):
            one[r["id"]] = (r, evs, pr)
    ship = shipping([{"rules_given": sorted({i for ev in evs if ev.get("event") == "rule_injected" for i in ev.get("rule_ids", [])}),
                      "held_out": r["held_out"], "steps": r["steps"],
                      "pr": {"state": pr.get("state")} if pr else None} for r, evs, pr in one.values()])
    ship_html = ""
    if len(ship) == 2 and any(v["merged"] for v in ship.values()):
        pct = lambda v: "–" if v is None else f"{100 * v:.0f}%"
        ship_html = window("Did rules help ship?", '<table class="rows"><thead><tr><th>Sessions</th><th>Count</th>'
                           '<th>Led to a merged PR</th><th>Steps per merged session</th></tr></thead><tbody>' + "".join(
                               f'<tr><td>{e(k)}</td><td class="n">{v["sessions"]}</td><td class="n">{v["merged"]} ({pct(v["merged_rate"])})</td>'
                               f'<td class="n">{v["steps_per_merged"] or "–"}</td></tr>' for k, v in sorted(ship.items())) + "</tbody></table>",
                           "not a controlled test unless a hold-out is set", pad=False)
    seg = '<div class="seg" role="group" aria-label="Filter by state" style="margin-bottom:14px">' + "".join(
        f'<a href="/prs{_q(repo, state=k)}" class="{"on" if (state or "") == k else ""}"'
        f'{" aria-current=true" if (state or "") == k else ""}>{n} {c}</a>'
        for k, n, c in (("", "All", len(allg)), ("open", "Open", count("open")), ("merged", "Merged", count("merged")),
                        ("closed", "Closed", count("closed")))) + "</div>"
    cards = ""
    for g in shown:
        pr = g["pr"]
        meta = []
        if pr.get("checks"):
            tone = {"passing": "mint", "failing": "rose", "pending": "amb"}[pr["checks"]]
            meta.append(f'<span class="{tone}">● checks {e(pr["checks"])}</span>')
        if pr.get("additions") is not None:
            meta.append(f'<span><span class="add">+{e(pr["additions"])}</span> <span class="del">−{e(pr.get("deletions") or 0)}</span></span>')
        when = (f"merged {ago(pr['merged'])}" if pr.get("merged") else f"closed {ago(pr['closed'])}" if pr.get("closed")
                else f"opened {ago(pr['created'])}" if pr.get("created") else "")
        if when:
            meta.append(f"<span>{e(when)}</span>")
        meta.append(f'<a href="{e(pr["url"])}" target="_blank" rel="noopener" style="font:500 12.5px var(--sans);color:var(--amber)">Open on GitHub ↗</a>')
        trs = ""
        for r, how in sorted(g["sessions"], key=lambda x: x[0]["started"] or 0):
            href = f"/conversations{_q(r['repo'], sid=r['id'])}"
            num = lambda v: f'{v}' if v else '<span class="zero">0</span>'
            trs += (f'<tr data-k data-href="{href}" onclick="location.href=\'{href}\'">'
                    f'<td class="p" style="color:var(--ink)"><a href="{href}" style="text-decoration:none">{e(detectors._short(r["prompt"], 90)) or "no prompt"}</a></td>'
                    f'<td><span class="chip">{e(r["agent"])}</span></td><td class="n">{num(r["steps"])}</td>'
                    f'<td class="n{" rose" if r["flags"] else ""}">{num(r["flags"])}</td><td class="n">{_length(r)}</td>'
                    f'<td class="n opt">{e(_when(r["started"]))}</td><td><span class="chip">{e(how)}</span></td></tr>')
        title = pr.get("title") or ("" if pr.get("state") else "title unknown until gh answers")
        cards += (f'<section class="win prcard"><div class="hd">{_pr_chip(pr)}'
                  f'<a class="ttl" href="{e(pr["url"])}" target="_blank" rel="noopener"><span>#{e(pr.get("number"))}</span> {e(title)}</a>'
                  + (f'<span class="mono faint" style="font-size:12px">{e(pr["head"])}</span>' if pr.get("head") else "")
                  + f'<span class="meta">{"".join(meta)}</span></div><div class="scroll"><table class="rows" style="min-width:760px">'
                  '<thead><tr><th>Session</th><th>Agent</th><th>Steps</th><th>Flags</th><th>Length</th><th class="opt">Started</th>'
                  f'<th>Linked by</th></tr></thead><tbody>{trs}</tbody></table></div></section>')
    if not shown:
        cards = window("Pull requests", '<div class="empty">' + (
            "No PRs in this state." if allg else
            "No session is linked to a PR yet. Sessions are linked when the agent runs <code>gh pr create</code>, "
            "when they worked on a PR's branch or made one of its commits, or when you link one below.") + "</div>", pad=False)
    back = f"/prs{_q(repo, state=state)}"
    ltr = "".join(
        f'<tr><td class="p" style="color:var(--ink)"><a href="/conversations{_q(r["repo"], sid=r["id"])}" style="text-decoration:none">'
        f'{e(detectors._short(r["prompt"], 80)) or "no prompt"}</a></td><td><span class="chip">{e(r["agent"])}</span></td>'
        f'<td class="n opt">{e(_branch(evs_by.get(r["id"], [])))}</td><td class="n">{e(ago(r["started"]))}</td>'
        f'<td style="text-align:right">{_link_form(r["id"], back)}</td></tr>'
        for evs_by in [{x[0]["id"]: x[1] for x in linked}] for r in loose[:20])
    loose_html = window("Sessions with no PR yet", f'<div class="scroll"><table class="rows" style="min-width:640px"><tbody>{ltr}</tbody></table></div>',
                        f"{plural(len(loose), 'session')}" + (" · newest 20" if len(loose) > 20 else ""), pad=False) if loose else ""
    note = f'<div class="msg">{e(msg)}</div>' if msg else ""
    summary = f"{plural(len(allg), 'PR')} made with an agent · {n_linked} of {plural(len(rows), 'session')} linked"
    body = (head("Pull requests", e(summary), status, repo_bar(con, repo, "/prs", {"state": state})) + note + tiles + seg + cards + ship_html + loose_html)
    return page("Pull requests", "prs", body, repo)


def _branch(evs) -> str:
    sig = prs.session_signals(evs)
    work = [b for b in sig["branches"] if b not in prs.DEFAULT_BRANCHES]
    return (work or sig["branches"] or [""])[-1]


def _day(ts) -> str:
    d = time.localtime(ts or 0)
    today = time.localtime()
    if (d.tm_year, d.tm_yday) == (today.tm_year, today.tm_yday):
        return "Today"
    y = time.localtime(time.time() - 86400)
    if (d.tm_year, d.tm_yday) == (y.tm_year, y.tm_yday):
        return "Yesterday"
    return time.strftime("%a %b %d", d)


def _cost(evs, model) -> str:
    try:
        from . import cost
        end = next((ev for ev in reversed(evs) if ev.get("event") == "session_end" and ev.get("usage")), None)
        if not end:
            return ""
        usd = cost.dollars(end["usage"], model, cost.load_prices())
        return f"${usd:.2f}" if usd is not None else ""
    except Exception:
        return ""


AGENTS = [("", "All"), ("claude-code", "Claude Code"), ("codex", "Codex"), ("antigravity", "Antigravity")]


def view_conversations(con, repo, sid=None, agent=None, q=None):
    rows = [r for r in _real_sessions(con, repo) if r["prompt"] or r["steps"] or r["id"] == sid]  # nothing was said: skip
    if agent:
        rows = [r for r in rows if r["agent"] == agent]
    if q:
        rows = [r for r in rows if conversations.search_hit(store.events(con, r["id"]), r["id"], r["agent"] or "", q)]
    rows = rows[:120]
    sel = next((r for r in rows if r["id"] == sid), None) if sid else (rows[0] if rows else None)
    if sid and not sel:
        sel = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    linked = {r["id"]: (pr, how) for r, _, pr, how in _pr_links(con, rows, commits=False)}
    items_html, day = "", None
    for r in rows:
        d = _day(r["started"])
        if d != day:
            items_html += f'<div class="day">{e(d)}</div>'
            day = d
        pr = linked.get(r["id"], (None, ""))[0]
        on = sel is not None and r["id"] == sel["id"]
        href = f"/conversations{_q(repo, agent=agent, q=q, sid=r['id'])}"
        items_html += (f'<a data-k class="card{" on" if on else ""}" href="{href}"{" aria-current=true" if on else ""}>'
                       f'<span class="t"><b>{e(detectors._short(r["prompt"], 90)) or "no prompt"}</b>{ROSEDOT if r["flags"] else ""}</span>'
                       f'<span class="meta">{e(r["agent"])} · {plural(r["steps"] or 0, "step")} · {e(ago(r["started"]))}'
                       + (f' · <span class="mint">{e(_pr_label(pr))}</span>' if pr else "") + "</span></a>")
    pills = '<div class="seg" role="group" aria-label="Filter by agent" style="margin-bottom:12px">' + "".join(
        f'<a href="/conversations{_q(repo, agent=k, q=q)}" class="{"on" if (agent or "") == k else ""}">{n}</a>' for k, n in AGENTS) + "</div>"
    search = (f'<form class="search" method=get action="/conversations">'
              + (f'<input type=hidden name=repo value="{e(repo)}">' if repo else "")
              + (f'<input type=hidden name=agent value="{e(agent)}">' if agent else "")
              + f'<input type=text name=q value="{e(q or "")}" placeholder="Words from a prompt or a reply" aria-label="Search conversations">'
              f'<button>Search</button></form>')
    if not rows:
        lst = f'<div class="empty">{"Nothing matches." if q or agent else "No conversations captured yet."}</div>'
    else:
        lst = items_html
    right = window("Conversation", '<div class="empty">Pick a conversation.</div>') if not sel else _conversation(con, sel, linked.get(sel["id"]))
    summary = "Everything you and your agents said, across Claude Code, Codex and Antigravity"
    hint = f"{plural(len(rows), 'conversation')} matching <b>{e(q)}</b>. <a href='/conversations{_q(repo, agent=agent)}'>Clear</a>" if q else ""
    body = (head("Conversations", summary, hint, f'<div class="tools">{repo_bar(con, repo, "/conversations", {"agent": agent})}{search}</div>') + '<div class="cols list convcols"><div class="clist">' + pills + lst
            + f"</div><div>{right}</div></div>")
    return page("Conversations", "conversations", body, repo)


def _conversation(con, r, link=None) -> str:
    evs = store.events(con, r["id"])
    items, have = conversations.thread(evs, r["id"], r["agent"] or "")
    if link is None:
        from . import repo as repo_
        pr, how = prs.link(r["repo"] or "", r["id"], evs, r["started"], prs.cached(r["repo"] or "")[0], repo_.known_root(r["repo"] or ""))
    else:
        pr, how = link
    who = (r["agent"] or "agent").split("-")[0].replace("claude", "Claude").replace("codex", "Codex").replace("antigravity", "Antigravity")
    learned = []
    try:
        learned = [x for x in rules.load_all(r["repo"]) if r["id"] in ((x.get("provenance") or {}).get("source_sessions") or [])]
    except Exception:
        pass
    out = []
    for it in items:
        t = time.strftime("%H:%M", time.localtime(it["ts"])) if it.get("ts") else ""
        if it["kind"] == "user":
            c = it.get("correction")
            out.append(f'<div class="cv u{" c" if c else ""}"><div class="who">you{" · " + t if t else ""}{" · correction" if c else ""}</div>'
                       f'<div class="tx">{e(it["text"])}</div>'
                       + ("".join(f'<div class="mono amb" style="font-size:12px;margin-top:8px"><a href="/rules{_q(r["repo"], id=x["id"])}">became rule {e(x["id"])} →</a></div>'
                                  for x in learned if (x.get("provenance") or {}).get("source_kind") == "chat") if c else "")
                       + "</div>")
        elif it["kind"] == "agent":
            out.append(f'<div class="cv a"><div class="who">{e(who)}{" · " + t if t else ""}</div><div class="tx">{e(it["text"])}</div></div>')
        else:
            st = it["steps"]
            kinds = {}
            for s in st:
                kinds[s.get("tool") or "other"] = kinds.get(s.get("tool") or "other", 0) + 1
            gist = " · ".join(f"{k} ×{n}" if n > 1 else k for k, n in kinds.items())
            bad = it["failed"] or it["flags"]
            lines = "".join(
                f'<div class="ln{" bad" if s.get("ok") is False else ""}"><a href="/session/{urllib.parse.quote(r["id"])}#s{s["step"]}">{s["step"]}</a>'
                f'{e((s.get("tool") or "").ljust(6))} {e(detectors._short(s.get("target", ""), 110))}'
                + (f' · {e(s.get("error_head") or "failed")}' if s.get("ok") is False else "") + "</div>" for s in st[:60])
            if len(st) > 60:
                lines += f'<div class="ln">{len(st) - 60} more steps on the timeline</div>'
            flags = "".join(f'<div class="cvflag">Flagged: {e(f["detector"].replace("_", " "))}, {e(f.get("note", ""))}</div>' for f in it["flags"])
            out.append(f'<details class="cvs{" bad" if bad else ""}"{" open" if bad else ""}><summary><b>{plural(len(st), "step")}</b>'
                       f'<span>{e(gist)}</span>' + (f'<span class="rose">{it["failed"]} failed</span>' if it["failed"] else "")
                       + f"</summary>{lines}{flags}</details>")
    if not have:
        out.insert(0, '<p class="faint" style="margin:0;font-size:13px">The agent\'s transcript is not on this machine any more, '
                      'so only your prompts and the steps are shown.</p>')
    if not out:
        out = ['<div class="empty" style="padding:0">Nothing was said in this session.</div>']
    first = next((it["text"] for it in items if it["kind"] == "user"), None) if have else None
    title = detectors._short(first or r["prompt"], 140) or "No prompt"  # the transcript is masked freshly
    md = conversations.as_markdown(items, title)
    meta = [r["agent"] or "", r["model"] or "", plural(conversations.turns(evs), "turn"), plural(r["steps"] or 0, "step"),
            _length(r), _cost(evs, r["model"]), _branch(evs), r["repo"] or ""]
    prchip = (f'<a class="chip {PR_TONE.get(pr.get("state") or "", "")}" href="{e(pr["url"])}" target="_blank" rel="noopener" '
              f'title="linked by {e(how)}" style="text-decoration:none">{e(_pr_label(pr))} ↗</a>') if pr else ""
    hd = (f'<div class="convhd"><div class="r"><h2>{e(title)}</h2>{prchip}'
          f'<a class="btn" href="/session/{urllib.parse.quote(r["id"])}">Open timeline</a>'
          f'<button type=button onclick="navigator.clipboard.writeText(document.getElementById(\'md\').value);this.textContent=\'Copied\'">Copy as Markdown</button></div>'
          f'<div class="facts" style="border:0;padding:0;margin-top:8px">{"".join(f"<span>{e(m)}</span>" for m in meta if m)}</div></div>'
          f'<textarea id="md" hidden>{e(md)}</textarea>')
    return f'<section class="win">{hd}<div class="thread">{"".join(out)}</div></section>'


def make_handler(cwd_slug: str, refresh):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store" if ctype.startswith("text/html") else "max-age=86400")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _local(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def do_GET(self):
            if not self._local():
                return self._send(403, "forbidden")
            u = urllib.parse.urlparse(self.path)
            if u.path.startswith("/static/fonts/"):
                name = Path(u.path).name
                f = FONTS / name
                if name.endswith(".woff2") and f.parent == FONTS and f.is_file():
                    return self._send(200, f.read_bytes(), "font/woff2")
                return self._send(404, "Not found.")
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            repo = q.get("repo") or None
            if u.path == "/live/stream":
                return self._stream(repo, q.get("sid"))
            con = store.connect()
            try:
                store.sync(con)
                if u.path in ("/", ""):
                    return self._send(302, "", headers={"Location": "/live"})
                if u.path == "/live":
                    return self._send(200, view_live(con, repo, q.get("sid"), cwd_slug))
                if u.path == "/live/fragment":
                    return self._send(200, live_fragment(con, repo, q.get("sid"), cwd_slug))
                if u.path == "/sessions":
                    return self._send(200, view_sessions(con, repo, q.get("path"), bool(q.get("flagged")), bool(q.get("lab"))))
                if u.path.startswith("/session/"):
                    out = view_session(con, urllib.parse.unquote(u.path[len("/session/"):]))
                    return self._send(200, out) if out else self._send(404, "No such session.")
                if u.path == "/rules":
                    return self._send(200, view_rules(con, repo, q.get("id"), cwd_slug, msg=q.get("msg")))
                if u.path == "/review":
                    return self._send(200, view_rules(con, repo, q.get("id"), cwd_slug, only_candidates=True))
                if u.path == "/overview":
                    return self._send(200, view_overview(con, repo, q.get("trend", "avoidable")))
                if u.path == "/conversations":
                    return self._send(200, view_conversations(con, repo, q.get("sid"), q.get("agent"), q.get("q")))
                if u.path == "/prs":
                    msg = q.get("msg")
                    if q.get("refresh"):
                        slugs = [repo] if repo else sorted({r["repo"] for r in _real_sessions(con, None) if r["repo"]})
                        res = [prs.fetch(s) for s in slugs if s.startswith("github.com/")]
                        msg = msg or "; ".join(m for ok, m in res if not ok) or None
                    st = q.get("state") if q.get("state") in ("open", "merged", "closed") else None
                    return self._send(200, view_prs(con, repo, st, msg))
                return self._send(404, "Not found.")
            finally:
                con.close()

        def _stream(self, repo, sid):
            """Server-sent events: watch the session files, push the Live view when one changes."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            base = paths.local() / "trajectories"
            last, beat, t0 = None, time.time(), time.time()
            try:
                while time.time() - t0 < 600:  # the browser reconnects by itself after this
                    sig = max(((f.stat().st_mtime, f.stat().st_size) for f in base.rglob("*.jsonl")), default=None) \
                        if base.is_dir() else None
                    if sig != last:
                        con = store.connect()
                        try:
                            store.sync(con)
                            frag = live_fragment(con, repo, sid, cwd_slug)
                        finally:
                            con.close()
                        self.wfile.write(("data: " + frag.replace("\n", "\ndata: ") + "\n\n").encode())
                        self.wfile.flush()
                        last, beat = sig, time.time()
                    elif time.time() - beat > 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        beat = time.time()
                    time.sleep(0.4)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if not self._local():
                return self._send(403, "forbidden")
            n = int(self.headers.get("Content-Length") or 0)
            f = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode(), keep_blank_values=True).items()}
            if not secrets.compare_digest(f.get("token", ""), TOKEN):
                return self._send(403, "Missing or wrong token.")
            con = store.connect()
            try:
                if self.path == "/tag":
                    from . import cli
                    store.sync(con)
                    row = con.execute("SELECT * FROM sessions WHERE id=?", (f.get("sid"),)).fetchone()
                    try:
                        lo, hi = int(f.get("lo", "")), int(f.get("hi") or f.get("lo", ""))
                    except ValueError:
                        return self._send(400, "Steps must be numbers.")
                    tag = f.get("tag") if f.get("tag") in dict(TAGS) else "wrong"
                    if not row or not cli._write_annotation(row, store.events(con, row["id"]), lo, hi, tag,
                                                            f.get("note", ""), f.get("detector") or None):
                        return self._send(400, "Those steps are out of range.")
                    return self._send(303, "", headers={"Location": f"/session/{urllib.parse.quote(row['id'])}"})
                if self.path == "/teach":
                    slug = f.get("slug") or cwd_slug
                    rule = rules.new_manual(f.get("sentence", "").strip(), [f["path"].strip()] if f.get("path", "").strip() else [], "you")
                    rule["id"] = rules.unique_id(slug, rule["id"])
                    if f.get("sid"):
                        rule["provenance"]["source_sessions"] = [f["sid"]]
                    if f.get("signature"):
                        try:
                            re.compile(f["signature"])
                            rule["provenance"]["signature"] = f["signature"][:300]
                        except re.error:
                            pass
                    if rules.lint(rule):
                        return self._send(400, "Not added: " + "; ".join(rules.lint(rule)))
                    file = rules.save(slug, rule)
                    rules.commit(f"teach {rule['id']}")
                    refresh(slug)
                    store.log_action(con, "teach", {"slug": slug, "id": rule["id"], "file": str(file), "root": None})
                    return self._send(303, "", headers={"Location": f"/rules?id={rule['id']}&repo={urllib.parse.quote(slug)}"})
                if self.path == "/review":
                    from . import learn
                    slug = f.get("slug") or cwd_slug
                    r = rules.find(slug, f.get("id", ""))
                    if not r or r.get("status") != "candidate":
                        return self._send(400, "That suggestion is no longer waiting.")
                    learn.decide(slug, r, "reject" if f.get("decision") == "reject" else "accept", "you",
                                 f.get("text") or None, f.get("reason") or None)
                    refresh(slug)
                    return self._send(303, "", headers={"Location": f"/review?repo={urllib.parse.quote(slug)}"})
                if self.path == "/rules/edit":
                    slug = f.get("slug") or cwd_slug
                    r = rules.find(slug, f.get("id", ""))
                    text = " ".join((f.get("text") or "").split())
                    if r and text and text != (r.get("rule") or {}).get("directive"):
                        new = dict(r, rule={**(r.get("rule") or {}), "directive": text})
                        errs = rules.lint({k: v for k, v in new.items() if not k.startswith("_")})
                        if errs:
                            return self._send(303, "", headers={"Location": f"/rules?id={r['id']}&repo={urllib.parse.quote(slug)}"
                                                                           f"&msg={urllib.parse.quote('Not saved: ' + errs[0])}"})
                        rules.save(slug, new)
                        rules.commit(f"edit {r['id']}")
                        refresh(slug)
                    return self._send(303, "", headers={"Location": f"/rules?id={urllib.parse.quote(f.get('id', ''))}&repo={urllib.parse.quote(slug)}"})
                if self.path == "/rules/promote":
                    import contextlib
                    import io
                    from types import SimpleNamespace
                    from . import cli, repo as repo_
                    slug = f.get("slug") or cwd_slug
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):  # the same promotion test as the CLI
                        cli._rules_promote(SimpleNamespace(id=f.get("id", ""), pattern=None, force=False, dry_run=False),
                                           slug, repo_.known_root(slug))
                    msg = "\n".join(l for l in buf.getvalue().strip().splitlines() if l.strip())
                    return self._send(303, "", headers={"Location": f"/rules?id={urllib.parse.quote(f.get('id', ''))}"
                                                                   f"&repo={urllib.parse.quote(slug)}&msg={urllib.parse.quote(msg[:600])}"})
                if self.path == "/prs/link":
                    row = con.execute("SELECT * FROM sessions WHERE id=?", (f.get("sid"),)).fetchone()
                    back = f.get("back") or "/prs"
                    if not back.startswith("/") or back.startswith("//"):
                        back = "/prs"
                    if not row:
                        return self._send(400, "No such session.")
                    ref = (f.get("ref") or "").strip()
                    if f.get("action") == "auto":
                        prs.set_manual(row["id"], None)
                    elif f.get("action") == "none":
                        prs.set_manual(row["id"], "")
                    else:
                        url = prs.parse_ref(ref, row["repo"])
                        if not url:
                            sep = "&" if "?" in back else "?"
                            return self._send(303, "", headers={"Location": back + sep + "msg=" + urllib.parse.quote(
                                f"Not linked: {ref or 'nothing'} is not a PR number or a github.com pull request link.")})
                        prs.set_manual(row["id"], url)
                    return self._send(303, "", headers={"Location": back})
                if self.path == "/rules/drop":
                    slug = f.get("slug") or cwd_slug
                    r = rules.find(slug, f.get("id", ""))
                    if r:
                        r["status"] = "deprecated"
                        rules.save(slug, r)
                        rules.commit(f"drop {r['id']}")
                        refresh(slug)
                        store.log_action(con, "drop", {"slug": slug, "id": r["id"], "root": None})
                    return self._send(303, "", headers={"Location": f"/rules?repo={urllib.parse.quote(slug)}"})
                return self._send(404, "Not found.")
            finally:
                con.close()
    return H


def serve(cwd_slug: str, refresh, port: int = 4317):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cwd_slug, refresh))
    srv.daemon_threads = True  # open live streams must not keep Ctrl-C from stopping the server
    return srv
