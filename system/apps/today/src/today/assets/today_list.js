// The Today list's craft, from the approved mock (today_tab.js, the user 2026-11-13): one rounded list
// per section in one aligned grid, quiet colours, a light segmented tab control, the big
// celebration when Now is cleared, and keyboard keys. Loaded before the page's main script; it
// uses that script's helpers (S, view, TAB, openIn, rowsByKey, renderList, ...) only when called.
// "This week" and the "This morning" win box are not touched here (the user's choice).

// ---- Clearing Now: a big check, confetti in the page's colours, and what the user finished ----------
const NOWCLEAR = { list: [], fresh: false, at: 0 };
function nowFinished(el){
  const r = (typeof rowsByKey !== "undefined" && rowsByKey[el.dataset.item]) || {};
  const title = String(r.title || r.words || el.querySelector(".words")?.textContent || "").replace(/\s+/g, " ").trim();
  if(title && !NOWCLEAR.list.includes(title)) NOWCLEAR.list.push(title);
  // Once the last row in Now has folded away, the celebration takes its place.
  setTimeout(() => {
    const now = document.getElementById("now");
    if(!now || now.querySelector(".item:not(.gone)")) return;
    NOWCLEAR.fresh = true; NOWCLEAR.at = Date.now();
    now.outerHTML = nowClearHtml();
    confettiFrom(document.getElementById("nowclear"));
  }, 450);
}
// Now can also empty by a step done or a refresh: when what was in Now is gone, celebrate it too.
let NOW_PENDING = false;
function nowCheck(before, after){
  if(!before || !after || !before.page.now.length || after.page.now.length) return;
  before.page.now.forEach(r => { const t = String(r.title || r.words || "").replace(/\s+/g, " ").trim(); if(t && !NOWCLEAR.list.includes(t)) NOWCLEAR.list.push(t); });
  if(Date.now() - NOWCLEAR.at < 15000) return;  // just celebrated as the last row folded away
  NOWCLEAR.fresh = true; NOWCLEAR.at = Date.now(); NOW_PENDING = true;
}
function nowAfterRender(){
  if(!NOW_PENDING) return;
  NOW_PENDING = false;
  confettiFrom(document.getElementById("nowclear"));
}
// It stays until the next refresh of the page; an empty Now is fine.
function nowClearHtml(){
  // The bloom plays once; a later redraw shows the finished state without replaying it.
  const fresh = NOWCLEAR.fresh; NOWCLEAR.fresh = false;
  return `<div class="clear${fresh ? " fresh" : ""}" id="nowclear"><span class="wonmark big" aria-hidden="true"></span><div class="cleartitle">Now is clear</div>`
    + `<div class="clearsub">${NOWCLEAR.list.map(t => `<div>&#10003; ${esc(t)}</div>`).join("")}</div></div>`;
}
function confettiFrom(anchor){
  if(!anchor || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const mark = anchor.querySelector(".wonmark"); if(!mark) return;
  const r = mark.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const c = document.createElement("canvas"); c.width = innerWidth * devicePixelRatio; c.height = innerHeight * devicePixelRatio;
  c.style.cssText = "position:fixed;inset:0;width:100vw;height:100vh;pointer-events:none;z-index:60"; document.body.appendChild(c);
  const g = c.getContext("2d"); g.scale(devicePixelRatio, devicePixelRatio);
  const cols = ["#8fb394", "#2f6b35", "#9b84c4", "#f3c77b", "#e6d3a8", "#c9b8e6"];
  const P = Array.from({length: 90}, () => { const a = -Math.PI / 2 + (Math.random() - .5) * Math.PI * 1.3, v = 5 + Math.random() * 7;
    return {x: cx, y: cy, vx: Math.cos(a) * v, vy: Math.sin(a) * v, w: 5 + Math.random() * 5, h: 3 + Math.random() * 4, rot: Math.random() * 6, vr: (Math.random() - .5) * .4, c: cols[Math.floor(Math.random() * cols.length)]}; });
  const t0 = performance.now();
  (function tick(t){ const e = (t - t0) / 1000; g.clearRect(0, 0, innerWidth, innerHeight);
    P.forEach(p => { p.vy += .28; p.vx *= .985; p.vy *= .985; p.x += p.vx; p.y += p.vy; p.rot += p.vr; g.save(); g.globalAlpha = Math.max(0, 1 - e / 1.8); g.translate(p.x, p.y); g.rotate(p.rot); g.fillStyle = p.c; g.fillRect(-p.w / 2, -p.h / 2, p.w, p.h); g.restore(); });
    if(e < 1.8) requestAnimationFrame(tick); else c.remove(); })(t0);
}

// ---- Keyboard: j/k move through the list (opening each), x finishes the open one, Enter or o
// opens or closes it, / searches. Never while the user is typing.
document.addEventListener("keydown", e => {
  if(typeof view === "undefined" || view.page !== "list" || TAB !== "today" || e.metaKey || e.ctrlKey || e.altKey) return;
  if(e.target.closest("input,textarea,select,[contenteditable]")) return;
  const rows = [...document.querySelectorAll("#main .list .item[data-item]")].filter(x => x.offsetParent && !x.classList.contains("gone"));
  if(!rows.length) return;
  const at = rows.findIndex(x => x.classList.contains("open"));
  const toggle = el => { const head = el.querySelector(".head"); if(head) head.click(); el.scrollIntoView({block: "nearest", behavior: "smooth"}); };
  if(e.key === "j" || e.key === "k"){
    e.preventDefault();
    const next = rows[at < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, at + (e.key === "j" ? 1 : -1)))];
    if(next && !next.classList.contains("open")) toggle(next);
  } else if((e.key === "Enter" || e.key === "o") && at >= 0){ e.preventDefault(); toggle(rows[at]); }
  else if(e.key === "x" && at >= 0){
    const ck = rows[at].querySelector(".head [data-done], .head [data-stepdone]");
    if(ck){ e.preventDefault(); ck.click(); }
  }
});

const TODAY_LIST_CSS = document.createElement("style");
TODAY_LIST_CSS.textContent = `
/* Tabs: a light segmented control, distinct from the content, no heavy pill */
.tabs .seg{display:inline-flex;gap:2px;background:#ece8df;border-radius:9px;padding:3px}
.tabs .tab{color:#6b665d;padding:4px 12px;border-radius:7px;font-weight:400}
.tabs .tab:hover{color:#1c1b19}
.tabs .tab.on{background:#fff;color:#1c1b19;font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.searchbox{display:inline-flex;align-items:center;gap:8px;background:#fff;border:1px solid #d9d4c8;border-radius:9px;padding:4px 10px;color:#6b665d;width:260px;margin-left:auto}
.searchbox input{font:inherit;border:0;outline:none;background:transparent;flex:1;min-width:0;color:#1c1b19}
/* One rounded list per section, thin lines between rows */
.list{background:#fff;border:1px solid #e7e3da;border-radius:12px;overflow:hidden;margin-bottom:6px}
.list .item{background:transparent;border:0;border-top:1px solid #f0ede6;border-radius:0;margin:0;box-shadow:none}
.list .item:first-child{border-top:0}
.list .item:hover{background:#fbfaf7}
.list .item.open{background:#fff}
/* One grid for every row and the card under it (the user approved the mock, 2026-11-15):
   check | title (the person's name first, in grey) | date and progress | calendar | chat.
   Everything on a row's first line sits in a box one title line tall, so the check, the date and
   both icons share the title's first-line centre; the open card starts on the title's left edge. */
.list .item{--tfs:15px;--tlh:calc(var(--tfs) * 1.55);--rail:28px;--edge:48px}
.list .item .head,.list.noname .item .head{display:grid;grid-template-columns:20px minmax(0,1fr) auto var(--rail) var(--rail);gap:0 12px;align-items:start;padding:11px 10px 11px 16px}
.list .item .words,.list .item .words.t{font-size:var(--tfs);line-height:var(--tlh);color:#1c1b19}
.list .item.placeholder .words{color:#a39d92}
.list .item .whobefore{color:#6b665d;font-weight:400}
.list .item .whobefore::after{content:" \\00b7  ";color:#a39d92}
.list .item .check2{width:20px;height:20px;margin:calc((var(--tlh) - 20px) / 2) 0 0;align-self:start}
.list .item .ckslot{width:20px}
.list .item .st .projname{max-width:19ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.list .item .st{display:inline-flex;gap:10px;align-items:center;justify-content:flex-end;white-space:nowrap;color:#a39d92;font-size:13px;height:var(--tlh);font-variant-numeric:tabular-nums}
.list .item .st:empty{display:block}
.list .item .st .duebtn,.list .item .dueslot .when{font-size:13px;color:#6b665d}
.list .item .st .duebtn.hot,.list .item .dueslot .when.hot{color:#b4541f}
/* The icon rail: two fixed slots, each a whole title line tall and a full slot wide to press */
.list .item .askicon,.list .item .askslot,.list .item .dueslot{width:var(--rail);justify-self:center}
.list .item .head .askicon,.list .item .head .dueslot .dueicon{align-self:start;height:var(--tlh);width:var(--rail);display:inline-flex;align-items:center;justify-content:center;padding:0;margin:0;border-radius:6px;color:#a39d92}
.list .item .head .dueslot{display:block;height:var(--tlh)}
/* A date sits where the date icon would, centred on it (the user, 2026-11-15); a phone shows it under the title instead. */
.list .item .head .dueslot:has(.when){display:flex;justify-content:center;align-items:center;white-space:nowrap;font-size:13px;font-variant-numeric:tabular-nums}
.list .item .head .stdue{display:none}
.list .item .head .askicon:hover,.list .item .head .dueicon:hover{color:#1c1b19;background:#f3f0ea}
/* The chat bubble's bottom edge sits level with the calendar's (the user, 2026-11-15): both are
   centred on the title's line, which left the bubble, with its tail below, looking high. */
.list .item .head .askicon svg{transform:translateY(1px)}
.list .item .head .dueicon svg{transform:translateY(-1px)}
/* Icons show when they are useful (the user, 2026-11-16): on a computer when the pointer is on the row
   or the card is open; on a phone the chat icon always, the calendar once the card is open. */
@media (hover:hover){
  .list .item .head .askicon,.list .item .head .dueicon{opacity:0}
  .list .item .head:hover .askicon,.list .item .head:hover .dueicon,.list .item.open .head .askicon,.list .item.open .head .dueicon,
  .list .item.reffed .head .askicon,.list .item .head .askicon:focus-visible,.list .item .head .dueicon:focus-visible{opacity:1}
}
@media (hover:none){
  .list .item .head .askicon{opacity:1}
  .list .item:not(.open) .head .dueslot .dueicon{display:none}
}
.list .item.open .head{padding-top:16px}
/* Titles wrap (the user, 2026-11-14): up to three lines on a closed row, all of it once open */
.list .item .words,.list .item .words.t{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;white-space:normal;overflow:hidden}
.list .item.open .words{font-weight:600;-webkit-line-clamp:unset;display:block;overflow:visible}
.list .item .body .msg.long{max-height:18em;overflow:hidden;-webkit-mask-image:linear-gradient(#000 80%,transparent)}
.list .item .body .showall{margin:0 0 8px;padding:0}
.list .item .body .msg{color:#1c1b19;white-space:pre-line;margin:0 0 6px}.list .item .body .msg .soft{color:#6b665d}
/* The open card starts on the title's left edge, every row alike */
.list .item .body,.list.noname .item .body,.list .item.todoitem .body{padding:0 14px 16px var(--edge)}
/* Quiet colours: only "today" is coloured */
.item .when{color:#6b665d}
.item .when.hot{color:#b4541f}
.list .item.forgoal{border-left:0;box-shadow:inset 3px 0 0 #9b84c4}
.list .item.changed{border-left:0;box-shadow:inset 3px 0 0 #2f6b35}
.list .item.reffed{background:#f6f2fb}
.more{background:none;border:0;font:inherit;display:inline-block;color:#6b665d;cursor:pointer;margin:2px 0 0 16px;padding:4px 0}.more:hover{color:#1c1b19}
/* Finishing, felt (the user, 2026-11-14): the press squeezes, the fill springs, the tick draws,
   the words go quiet, then the row leaves. */
.list .item .check2{transition:transform .1s ease-out,background-color .14s ease-out,border-color .14s ease-out}
.list .item .check2:active{transform:scale(.86)}
.list .item .check2.ticked,.check2.ticked{background:#2f6b35;border-color:#2f6b35;animation:tickspring .34s cubic-bezier(.34,1.56,.64,1)}
.check2.ticked::after{content:"";position:absolute;left:6px;top:2px;width:4px;height:9px;border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg);animation:tickdraw .24s .06s cubic-bezier(.65,0,.35,1) both}
@keyframes tickspring{0%{transform:scale(.86)}55%{transform:scale(1.12)}100%{transform:scale(1)}}
@keyframes tickdraw{0%{clip-path:inset(100% 0 0 0);opacity:0}30%{opacity:1}100%{clip-path:inset(0 0 0 0);opacity:1}}
.list .item.finishing .words,.list .item.finishing .st{color:#a39d92;transition:color .25s ease-out}
.list .item .check2{position:relative}
@keyframes pop{from{transform:rotate(45deg) scale(.3);opacity:0}to{transform:rotate(45deg) scale(1);opacity:1}}
/* Now is clear */
.clear{background:#f4f8f3;border:1.5px solid #cfe0d1;border-radius:12px;padding:30px 20px 26px;text-align:center}
.wonmark.big{width:56px;height:56px;margin:0 auto 14px;display:block}
.wonmark.big::after{left:21px;top:11px;width:12px;height:24px;border-width:0 3.5px 3.5px 0}
.cleartitle{font-weight:600;color:#2f6b35}
.clearsub{color:#6b665d;margin-top:8px}
.clear.fresh .wonmark{animation:bloom 1.6s cubic-bezier(.2,.7,.2,1) both}
.clear.fresh .cleartitle,.clear.fresh .clearsub{animation:rise .9s ease .35s both}
@media (max-width:600px){
  /* A phone gives the card its whole width (the user, Nov 15): a smaller check, no empty name
     column on a row with no name, and the open card's body from edge to edge. */
  /* A phone: the title at 16 px, the rail one slot wider to tap; dates and progress drop under
     the title so it keeps the row's width. */
  .list .item{--tfs:16px;--rail:36px;--edge:40px}
  .list .item .head,.list.noname .item .head{grid-template-columns:18px minmax(0,1fr) var(--rail) var(--rail);gap:0 10px;padding:11px 4px 11px 12px}
  .list .item .head .st{grid-column:2;grid-row:2;justify-content:flex-start;height:auto;margin-top:1px}
  .list .item .head .st:empty{display:none}
  .list .item .head .dueslot{grid-column:3;grid-row:1}
  .list .item .head .askicon{grid-column:4;grid-row:1}
  .list .item .head .stdue{display:inline}
  .list .item .head .dueslot .when{display:none}
  .list .item:not(.open) .head{grid-template-columns:18px minmax(0,1fr) var(--rail)}
  .list .item:not(.open) .head .dueslot{display:none}
  .list .item:not(.open) .head .askicon{grid-column:3}
  .list .item .check2{width:18px;height:18px;margin-top:calc((var(--tlh) - 18px) / 2)}
  /* Phone sizes (the user, Nov 15): the title at 16 px, everything else on the card at 14 px.
     Boxes stay 14 px too; the page's zoom limit keeps the phone from zooming into them. */
  .list .c5{--c5body:14px;--c5small:14px}
  .list .c5 .c5msg{font-size:14px}
  .list .c5 input,.list .c5 textarea{font-size:14px!important}
  .list .item .head > *{order:0}
  .list .item .body,.list.noname .item .body,.list .item.todoitem .body{padding:0 12px 14px var(--edge)}
  .searchbox{width:auto;flex:1}
  /* On a phone the project name takes the title's room (the user, 2026-11-14): it stays on wider screens */
  .list .item .st .projname{display:none}
  /* The top of the page on a phone (the user, 2026-11-14): the tabs and the search icon on one line;
     wide desktop-only views stay off the phone */
  .tabs{flex-wrap:nowrap;gap:6px;justify-content:flex-start}
  .tabs .seg{flex:1;min-width:0}
  .tabs .seg .tab{flex:1;padding:5px 2px;min-width:0}
  .tabs button.tabsearch{flex:0 0 auto;margin:0}
  .tabs .seg .tab{font-size:14.5px}
  /* Each tab as wide as its word (the user, 2026-11-16: "Projects" and "Someday" ran together). */
  .tabs .seg{gap:0}
  .tabs .seg .tab{flex:1 1 auto;padding:5px 5px;font-size:14px;white-space:nowrap}
  /* This week on a phone (the user, 2026-11-16): the user's words take the card's full width, and
     "+ next step" sits on its own short line under them instead of in a right-hand column. */
  .week{padding:14px 16px}
  .goals li .gcol{padding-right:0}
  .goals li .gadd{position:static;display:block;opacity:1;font-size:14px;padding:4px 0 2px}
  .tabs:has(.searchbox,input.tabsearch){flex-wrap:wrap}
  .tabs .searchbox,.tabs input.tabsearch{flex:1 1 100%;width:auto;margin:6px 0 0}
  .tabs .views{display:none}
}
`;
document.head.appendChild(TODAY_LIST_CSS);

// ---- Drag to reorder: one helper for every list that can be reordered ----------------------
// Lists marked `.list.dnd` (with a data-sec name) take drags of their rows (by the title line); a folded
// section's button with a data-sec is a drop target too (the row goes to its end). A mouse drag
// starts after a few pixels; on a phone, press and hold. ``onDrop({key, sec, before})`` gets the
// row's key, the section it lands in and the key of the row it now sits above ("" for the end).
function dragSort(root, onDrop){
  const NOTDRAG = "button,a,input,textarea,select,[contenteditable=true],.check2,.ckslot,.askicon";
  root.querySelectorAll(".list.dnd > .item[data-item]").forEach(row => {
    const head = row.querySelector(".head") || row;
    head.addEventListener("pointerdown", e => {
      if(e.button !== 0 || e.target.closest(NOTDRAG)) return;
      startDrag(row, e, root, onDrop);
    });
  });
}
function startDrag(row, e, root, onDrop){
  const touch = e.pointerType !== "mouse", x0 = e.clientX, y0 = e.clientY;
  let live = false, timer = null, target = null, line = null, lastY = y0;
  const blockScroll = ev => { if(live) ev.preventDefault(); };
  const begin = () => {
    live = true; row.classList.add("lifted"); document.body.classList.add("dragging-row");
    getSelection().removeAllRanges();
    line = document.createElement("div"); line.className = "dropline";
    if(navigator.vibrate && touch) navigator.vibrate(10);
    place(lastY);
  };
  const clear = () => { root.querySelectorAll(".dropover").forEach(x => x.classList.remove("dropover")); if(line) line.remove(); };
  const place = y => {
    clear(); target = null;
    const fold = [...root.querySelectorAll("button.fold[data-sec]")].find(b => { const r = b.getBoundingClientRect(); return y >= r.top - 6 && y <= r.bottom + 6; });
    if(fold){ fold.classList.add("dropover"); target = {sec: fold.dataset.sec, before: ""}; return; }
    const lists = [...root.querySelectorAll(".list.dnd[data-sec]")];
    if(!lists.length) return;
    // The list under the pointer, or the nearest one above or below it.
    const dist = l => { const r = l.getBoundingClientRect(); return y < r.top ? r.top - y : y > r.bottom ? y - r.bottom : 0; };
    const list = lists.reduce((a, b) => dist(b) < dist(a) ? b : a);
    const rows = [...list.querySelectorAll(":scope > .item[data-item]")].filter(r => r !== row);
    const next = rows.find(r => { const b = r.getBoundingClientRect(); return y < b.top + b.height / 2; });
    if(next) list.insertBefore(line, next);
    else if(rows.length) list.appendChild(line);
    else list.classList.add("dropover");
    target = {sec: list.dataset.sec, before: next ? next.dataset.item : ""};
  };
  const move = ev => {
    if(ev.pointerId !== e.pointerId) return;
    lastY = ev.clientY;
    if(!live){
      const far = Math.hypot(ev.clientX - x0, ev.clientY - y0);
      if(touch){ if(far > 8) end(); return; }  // a finger that moves first is scrolling
      if(far > 6) begin();
      return;
    }
    place(ev.clientY);
    const edge = 60, h = window.innerHeight;
    if(ev.clientY < edge) window.scrollBy(0, -12); else if(ev.clientY > h - edge) window.scrollBy(0, 12);
  };
  const end = ev => {
    clearTimeout(timer);
    document.removeEventListener("pointermove", move, true);
    document.removeEventListener("pointerup", end, true);
    document.removeEventListener("pointercancel", end, true);
    document.removeEventListener("touchmove", blockScroll, {capture: true});
    if(!live) return;
    live = false; clear(); row.classList.remove("lifted"); document.body.classList.remove("dragging-row");
    // The click that ends a drag must not open the row.
    const swallow = c => { c.stopPropagation(); c.preventDefault(); };
    document.addEventListener("click", swallow, {capture: true, once: true});
    setTimeout(() => document.removeEventListener("click", swallow, {capture: true}), 50);
    if(ev && ev.type === "pointerup" && target) onDrop({key: row.dataset.item, sec: target.sec, before: target.before});
  };
  document.addEventListener("pointermove", move, true);
  document.addEventListener("pointerup", end, true);
  document.addEventListener("pointercancel", end, true);
  document.addEventListener("touchmove", blockScroll, {capture: true, passive: false});
  if(touch) timer = setTimeout(begin, 350);
}
// A list's keys after moving ``key`` into section ``sec`` above ``before`` ("" for the end).
function movedOrder(secs, key, sec, before){
  const out = {};
  for(const [name, keys] of Object.entries(secs)) out[name] = keys.filter(k => k !== key);
  const list = out[sec] || (out[sec] = []);
  const at = before ? list.indexOf(before) : -1;
  list.splice(at < 0 ? list.length : at, 0, key);
  return out;
}
