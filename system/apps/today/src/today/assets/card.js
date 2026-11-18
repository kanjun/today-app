// The one task card (the user approved mock 5, 2026-11-14). Every list on the Today tab and every
// project page opens the same card, so a change here changes it everywhere:
//   their conversation, inline and smaller, with the app it lives in at the top right;
//   the task's steps, the current one (or the one the user clicked) carrying the actions;
//   the options the assistant offers (act.py), "You picked ... · Other options", what was done;
//   the reply written, with Send (10 seconds to undo) and "Change it" (the bar at the bottom);
//   a quiet footer: + Step, Not now, more.
// Loaded before the page's main script; uses its helpers (api, esc, toast, toastUndo, sendSoon,
// finishRow, dockRef, go, rowsByKey) only when called.

const CARD = { data: {}, opened: {}, notesOpen: {}, busy: {}, adding: {}, threadUp: {}, threadDown: {}, own: {}, saveT: {}, pending: {}, down: false, later: new Set() };
// Each edit is also kept on this device until Today has it, so a page the phone closes
// without warning loses nothing: the next time Today opens, it shows and sends what was left.
function postEdit(slot, url, payload, done){
  return fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload), keepalive: true})
    .then(r => r.json().catch(() => ({})).then(j => {
      if(r.ok){ try{ const now = JSON.parse(localStorage.getItem(slot) || "null"); if(now && JSON.stringify(now.payload) === JSON.stringify(payload)) localStorage.removeItem(slot); }catch(e){}
        if(done) done(j); }
      else { toast("Edit not saved: " + (j.error || r.status)); if(r.status < 500) localStorage.removeItem(slot); } }))
    .catch(() => toast("Edit not saved yet. It's kept on this device and goes when you're back online."));
}
addEventListener("DOMContentLoaded", () => {  // once the page's own script (and its edit store) is in
  for(let n = localStorage.length - 1; n >= 0; n--){
    const slot = localStorage.key(n);
    if(!slot || !slot.startsWith("todayEdit:")) continue;
    try{ const left = JSON.parse(localStorage.getItem(slot));
      if(left.payload.index === undefined || left.payload.index === null) xpEdits[left.payload.key] = left.payload.text;
      postEdit(slot, left.url, left.payload); }catch(e){ localStorage.removeItem(slot); }
  }
});
// An edit still waiting to save goes now when the user switches away or closes the page.
addEventListener("visibilitychange", () => { if(document.visibilityState === "hidden") Object.values(CARD.pending).forEach(f => f()); });
addEventListener("pagehide", () => Object.values(CARD.pending).forEach(f => f()));
// Steps the user opened with their options button, per card; the current step always shows its own.
const openedSteps = key => CARD.opened[key] || (CARD.opened[key] = new Set());

// Working out options takes the model several seconds. Those requests go one at a time, so they
// never hold up opening a card, which reads in a few milliseconds (the user, 2026-11-14: the first
// open was slow while other cards' options were being worked out).
const OPTQ = { running: 0, queue: [] };
function optionsSoon(fn){ OPTQ.queue.push(fn); optPump(); }
function optPump(){
  while(OPTQ.running < 1 && OPTQ.queue.length){
    const fn = OPTQ.queue.shift(); OPTQ.running++;
    fn().finally(() => { OPTQ.running--; optPump(); });
  }
}
// A card's own data is read as soon as the pointer rests on its row, so it is there on click.
document.addEventListener("mouseover", e => {
  const row = e.target.closest && e.target.closest(".item[data-item]");
  if(!row || row.classList.contains("open")) return;
  const key = row.dataset.item, r = (typeof rowsByKey !== "undefined" && rowsByKey[key]) || null;
  if(!r || CARD.data[key] || CARD.busy[key + "|read"] || !usesCard(r, row.dataset.view)) return;
  CARD.busy[key + "|read"] = 1;
  api("/api/card?key=" + encodeURIComponent(key)).then(d => { if(!CARD.data[key]) CARD.data[key] = d; }).catch(() => {}).finally(() => { delete CARD.busy[key + "|read"]; });
});
// A redraw never replaces a button under a press: one that arrives mid-click waits for the release
// (the user, 2026-11-14: unticking a step took several clicks while options were being worked out).
document.addEventListener("pointerdown", e => { if(e.target.closest && e.target.closest("[data-card]")) CARD.down = true; }, true);
document.addEventListener("pointerup", () => { if(!CARD.down) return; CARD.down = false;
  setTimeout(() => { const later = [...CARD.later]; CARD.later.clear(); later.forEach(f => f()); }, 0); }, true);
const CARD_ARROW = '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.3" width="10" height="10" aria-hidden="true"><path d="M4 2.5h5.5V8M9.5 2.5 3 9"/></svg>';
const CARD_SVC = {
  Slack: '<svg viewBox="0 0 122.8 122.8" width="14" height="14" aria-hidden="true"><path d="M25.8 77.6c0 7.1-5.8 12.9-12.9 12.9S0 84.7 0 77.6s5.8-12.9 12.9-12.9h12.9v12.9zm6.5 0c0-7.1 5.8-12.9 12.9-12.9s12.9 5.8 12.9 12.9v32.3c0 7.1-5.8 12.9-12.9 12.9s-12.9-5.8-12.9-12.9V77.6z" fill="#e01e5a"/><path d="M45.2 25.8c-7.1 0-12.9-5.8-12.9-12.9S38.1 0 45.2 0s12.9 5.8 12.9 12.9v12.9H45.2zm0 6.5c7.1 0 12.9 5.8 12.9 12.9s-5.8 12.9-12.9 12.9H12.9C5.8 58.1 0 52.3 0 45.2s5.8-12.9 12.9-12.9h32.3z" fill="#36c5f0"/><path d="M97 45.2c0-7.1 5.8-12.9 12.9-12.9s12.9 5.8 12.9 12.9-5.8 12.9-12.9 12.9H97V45.2zm-6.5 0c0 7.1-5.8 12.9-12.9 12.9s-12.9-5.8-12.9-12.9V12.9C64.7 5.8 70.5 0 77.6 0s12.9 5.8 12.9 12.9v32.3z" fill="#2eb67d"/><path d="M77.6 97c7.1 0 12.9 5.8 12.9 12.9s-5.8 12.9-12.9 12.9-12.9-5.8-12.9-12.9V97h12.9zm0-6.5c-7.1 0-12.9-5.8-12.9-12.9s5.8-12.9 12.9-12.9h32.3c7.1 0 12.9 5.8 12.9 12.9s-5.8 12.9-12.9 12.9H77.6z" fill="#ecb22e"/></svg>',
  Gmail: '<svg viewBox="0 0 256 193" width="14" height="14" aria-hidden="true"><path fill="#4285F4" d="M58.182 192.05V93.14L27.507 65.077 0 49.504v125.091c0 9.658 7.825 17.455 17.455 17.455h40.727Z"/><path fill="#34A853" d="M197.818 192.05h40.727c9.659 0 17.455-7.826 17.455-17.455V49.505l-31.156 17.837-27.026 25.798v98.91Z"/><path fill="#EA4335" d="m58.182 93.14-4.174-38.647 4.174-36.989L128 69.868l69.818-52.364 4.669 34.992-4.669 40.644L128 145.504z"/><path fill="#FBBC04" d="M197.818 17.504V93.14L256 49.504V26.231c0-21.585-24.64-33.89-41.89-20.945l-16.292 12.218Z"/><path fill="#C5221F" d="M0 49.504l26.759 20.07L58.182 93.14V17.504L41.89 5.286C24.61-7.66 0 4.646 0 26.23v23.273Z"/></svg>',
  Messages: '<svg viewBox="0 0 64 64" width="14" height="14" aria-hidden="true"><defs><linearGradient id="c5msg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5BF675"/><stop offset="1" stop-color="#0CBD2A"/></linearGradient></defs><rect width="64" height="64" rx="14" fill="url(#c5msg)"/><path d="M32 13c-11.6 0-21 7.6-21 17 0 5.4 3.1 10.2 8 13.3-.4 2.6-1.7 5.1-3.6 7 3.9-.2 7.4-1.6 10.1-3.8 2 .5 4.2.8 6.5.8 11.6 0 21-7.6 21-17.3S43.6 13 32 13z" fill="#fff"/></svg>',
  Signal: '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><circle cx="12" cy="12" r="12" fill="#3A76F0"/><path fill="#fff" d="M12 5.5c-3.9 0-7 2.7-7 6.1 0 1.3.5 2.6 1.3 3.6l-.7 3.3 3.4-1.1c.9.4 1.9.6 3 .6 3.9 0 7-2.7 7-6.2S15.9 5.5 12 5.5z"/></svg>',
  WhatsApp: '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><circle cx="12" cy="12" r="12" fill="#25D366"/><path fill="none" stroke="#fff" stroke-width="1.6" d="M6.3 17.7l.8-2.8A6 6 0 1 1 9.4 17z"/><path fill="#fff" d="M9.6 8.6c.2-.4.5-.4.7-.4h.4c.1 0 .3 0 .4.3l.6 1.4c0 .2 0 .3-.1.4l-.4.5c-.1.1-.1.3 0 .4.4.7 1.3 1.6 2.1 2 .1.1.3.1.4 0l.5-.6c.1-.1.3-.2.4-.1l1.4.7c.2.1.2.2.2.4 0 .5-.4 1.2-1.1 1.3-.6.1-1.4.1-3.2-1.2-1.6-1.2-2.4-2.7-2.6-3.3-.2-.6 0-1.3.3-1.8z"/></svg>',
  'X (Twitter)': '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><rect width="24" height="24" rx="5" fill="#000"/><path fill="#fff" transform="translate(5 5) scale(.583)" d="M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932ZM17.61 20.644h2.039L6.486 3.24H4.298Z"/></svg>',
  Messenger: '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><defs><linearGradient id="c5mgr" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#0099FF"/><stop offset=".6" stop-color="#A033FF"/><stop offset="1" stop-color="#FF5280"/></linearGradient></defs><path fill="url(#c5mgr)" d="M12 1C5.9 1 1 5.5 1 11.1c0 3.2 1.6 6 4.1 7.8V23l3.8-2.1c1 .3 2 .4 3.1.4 6.1 0 11-4.5 11-10.1S18.1 1 12 1z"/><path fill="#fff" d="M6 14.5l4.2-5 2.8 2.3 5-2.3-4.2 5-2.8-2.3z"/></svg>',
  Telegram: '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><circle cx="12" cy="12" r="12" fill="#26A5E4"/><path fill="#fff" d="M5.4 11.8l11.3-4.4c.5-.2 1 .1.8.9l-1.9 9c-.1.6-.5.8-1 .5l-2.9-2.1-1.4 1.3c-.2.2-.3.3-.6.3l.2-2.9 5.3-4.8c.2-.2 0-.3-.3-.1l-6.6 4.1-2.8-.9c-.6-.2-.6-.6.1-.9z"/></svg>',
  LinkedIn: '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><rect width="24" height="24" rx="4" fill="#0A66C2"/><path fill="#fff" d="M5.3 9.5h2.9V19H5.3zM6.8 4.9a1.7 1.7 0 1 1 0 3.4 1.7 1.7 0 0 1 0-3.4zM10.1 9.5h2.8v1.3c.4-.7 1.4-1.5 2.8-1.5 3 0 3.5 2 3.5 4.5V19h-2.9v-4.6c0-1.1 0-2.5-1.5-2.5s-1.8 1.2-1.8 2.4V19h-2.9z"/></svg>',
  Instagram: '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><defs><radialGradient id="c5ig" cx=".3" cy="1.07" r="1.3"><stop offset="0" stop-color="#FFDD55"/><stop offset=".1" stop-color="#FFDD55"/><stop offset=".5" stop-color="#FF543E"/><stop offset="1" stop-color="#C837AB"/></radialGradient></defs><rect width="24" height="24" rx="6" fill="url(#c5ig)"/><rect x="5" y="5" width="14" height="14" rx="4" fill="none" stroke="#fff" stroke-width="1.6"/><circle cx="12" cy="12" r="3.3" fill="none" stroke="#fff" stroke-width="1.6"/><circle cx="16.3" cy="7.7" r="1" fill="#fff"/></svg>'
};
CARD_SVC.Linkedin = CARD_SVC.LinkedIn; CARD_SVC.Twitter = CARD_SVC["X (Twitter)"];

// Which rows open as the card: everything except a weekly priority's own move (This week is the user's
// to keep as it is) and the Filed away list.
function usesCard(r, viewName){ return !!r && !(r.move && r.move.kind !== "message") && viewName !== "filed" && !r.placeholder; }

// Drawn at once from the row; the rest arrives from /api/card.
function cardShell(r, viewName){
  const d = CARD.data[r.key];
  return `<div class="c5" data-card="${esc(r.key)}">${d ? cardInner(r, viewName, d) : `<div class="c5thread"><div class="c5msg"><div class="c5mt">${esc(tidyMsg(r.words || ""))}</div></div></div><div class="c5writing">Opening…</div>`}</div>`;
}

// What the user said or picked, and what Today did about it: one quiet block (the user approved the mock,
// 2026-11-16). ``back`` is the Other options button's attributes; ``did`` the results, already drawn.
function c5said(t, back, did){
  return `<div class="c5said"><div class="c5chosen"><span>You ${t.label === t.said ? "said" : "picked"}</span>`
    + `<button ${back}>Other options</button></div><div class="c5her">${esc(t.label)}</div>${did}</div>`;
}

function c5msgs(d){
  const t = d.thread || {messages: [], anchor: -1, earlier: 0}, ms = t.messages;
  const at = t.anchor >= 0 ? t.anchor : ms.length - 1, up = CARD.threadUp[d.key], down = CARD.threadDown[d.key];
  // The lead-up stays short; a couple of replies show, since the ask usually moves on there.
  const lo = up ? 0 : Math.max(0, at - 2), hi = down ? ms.length : Math.min(ms.length, at + 3);
  const shown = ms.slice(lo, hi), above = t.earlier + lo, below = ms.length - hi;
  // Every chat app shows its icon; one with a link back opens it (some chats have none, the user 2026-11-15).
  const out = !d.service ? "" : d.url ? `<a class="c5out" href="${esc(d.url)}" target="_blank" title="Open in ${esc(d.service)}">${CARD_SVC[d.service] || `<span class="c5svc">${esc(d.service)}</span>`}${CARD_ARROW}</a>`
    : CARD_SVC[d.service] ? `<span class="c5out c5from-app" title="From ${esc(d.service)}">${CARD_SVC[d.service]}</span>` : "";
  if(!shown.length){
    const words = d.manual ? "" : d.ask_text;
    return words ? `<div class="c5thread"><div class="c5msg"><div class="c5mh"><span class="c5from">${esc(d.who || "")}</span>${out}</div><div class="c5mt">${esc(tidyMsg(words))}</div></div></div>` : "";
  }
  const more = (n, word) => `${n} ${word} ${n === 1 ? "message" : "messages"}`;
  let h = `<div class="c5thread">`;
  // Older messages the card didn't keep (t.earlier) open in the conversation itself.
  if(lo > 0) h += `<div class="c5mh"><button class="c5earlier" data-c5earlier>${more(above, "earlier")}</button>${out}</div>`;
  else if(t.earlier > 0) h += `<div class="c5mh"><a class="c5earlier" href="${esc(d.url || "#")}" target="_blank">${more(t.earlier, "earlier")} in ${esc(d.service || "the conversation")}</a>${out}</div>`;
  const head = lo > 0 || t.earlier > 0;
  h += shown.map((m, i) => `<div class="c5msg${m.me ? " me" : ""}${m.reply ? " reply" : ""}${lo + i === at && ms.length - 1 !== at ? " c5src" : ""}"><div class="c5mh"><span class="c5from">${esc(m.from)}</span><span class="c5at">${esc(m.at)}</span>${i === 0 && !head ? out : ""}</div>`
    + `<div class="c5mt">${esc(tidyMsg(m.text))}</div></div>`).join("");
  if(below > 0) h += `<button class="c5earlier c5newer" data-c5newer>${more(below, "newer")}</button>`;
  return h + `</div>`;
}

// What the user does now, for one focus (the task, or one step): what the user picked, what was done, the
// reply when one is written, else the options.
function c5act(r, d, focus){
  const key = d.key, v = (focus ? d.step_act[focus] : d.act) || {turns: [], next: null, ready: false};
  if(CARD.busy[key + "|" + focus]) return `<div class="c5writing">${esc(CARD.busy[key + "|" + focus])}</div>`;
  let h = "";
  const last = v.turns[v.turns.length - 1];
  if(last) h += c5said(last, `data-c5back title="Take that back and see the other options again"`,
    v.turns.map(t => t.did ? `<div class="c5result">${esc(t.did)}</div>` : "").join(""));
  if(!focus || (last && last.draft)) h += c5draft(d);
  if(!focus) h += c5made(d);
  if(v.next === null || v.next === undefined) h += `<div class="c5writing" data-c5opening>Working out options…</div>`;
  else if(v.next.length) h += `<div class="c5opts">${v.next.map((o, i) => `<button class="btn" data-c5opt="${i}" title="${esc(o.say)}">${esc(o.label)}</button>`).join("")}</div>`;
  return h;
}

// The reply written (or the nudge, on Waiting on), with Send and "Change it".
// What was made for the task as a whole (calendar entries, emails written): shown on the card
// whether or not a step is open, since a step's options often write them (the user, 2026-11-17:
// the email to Anton was written from a step and never showed).
function c5made(d){
  let h = "";
  (d.calendar || []).forEach(c => { h += c.added
      ? `<div class="c5result">On your calendar: ${esc(c.title)}${c.link ? ` <a href="${esc(c.link)}" target="_blank">Open</a>` : ""}</div>`
      : `<div class="c5row"><button class="btn" data-c5cal="${c.i}" title="Puts it on your calendar, with no guests. Nothing is sent.">Add to my calendar: ${esc(c.title)}${c.start ? ", " + esc(fmtWhen(c.start)) : ""}</button></div>`; });
  (d.emails || []).forEach((e, i) => { if(e.sent) return;
    h += `<div class="c5email" data-c5email="${i}"><div class="c5ef"><span>To</span><input data-f="to" value="${esc(e.to)}"></div><div class="c5ef"><span>Subject</span><input data-f="subject" value="${esc(e.subject)}"></div>`
      + `<textarea class="c5draft" data-f="body" rows="${Math.min(14, Math.max(4, e.body.split("\n").length + 1))}">${esc(e.body)}</textarea>`
      + `<div class="c5row"><button class="btn primary" data-c5sendemail="${i}" title="Sends this email, on your thread with them when there is one. You get 10 seconds to undo.">Send</button></div></div>`; });
  return h;
}

function c5draft(d){
  const key = d.key, hd = d.user_draft;
  if(hd){
    const text = xpEdits[key] ?? hd.text;
    return `<div class="c5label">Your unsent draft in Gmail${hd.date ? " · " + esc(fmtDay(hd.date)) : ""}</div><textarea class="c5draft" data-c5draft rows="${Math.min(14, Math.max(4, text.split("\n").length + 1))}">${esc(text)}</textarea>`
      + `<div class="c5row"><button class="btn primary" data-c5send data-own="1" title="Sends your draft, with your changes. You get 10 seconds to undo.">Send</button>`
      + `<span class="c5saved" title="Your changes go into the draft in Gmail as you type. Nothing is sent.">Saved to your Gmail draft as you type</span></div>`;
  }
  const text = xpEdits[key] ?? (d.state === "waiting" ? d.nudge : d.draft);
  if(!text) return "";
  if(d.draft_for && !d.can_send){
    // Written for someone other than this conversation (Iris on Slack, say): shown as what it
    // is, to copy, never dressed up as an email (the user, Nov 15).
    const rows = Math.min(14, Math.max(3, text.split("\n").reduce((n, l) => n + Math.max(1, Math.ceil(l.length / 70)), 0) + 1));
    return `<div class="c5label">Draft for ${esc(d.draft_for)}. Today can't send this one yet</div><textarea class="c5draft" data-c5draft rows="${rows}">${esc(text)}</textarea>`
      + `<div class="c5row"><button class="btn quiet" data-c5copy title="Copies the message so you can paste it where you talk with them">Copy</button>`
      + `<button class="btn quiet" data-c5change title="Say what should change, in the bar at the bottom">Change it</button></div>`;
  }
  if(!(d.can_send || d.state === "waiting")) return c5owndraft(d, text);
  const rows = Math.min(14, Math.max(3, text.split("\n").reduce((n, l) => n + Math.max(1, Math.ceil(l.length / 70)), 0) + 1));
  return `<textarea class="c5draft" data-c5draft rows="${rows}">${esc(text)}</textarea>`
    + `<div class="c5row"><button class="btn primary" data-c5send title="${esc((d.send_to || "Send") + ". You get 10 seconds to undo.")}">Send</button>`
    + `<button class="btn quiet" data-c5change title="Say what should change, in the bar at the bottom">Change it</button></div>`
    + (d.error ? `<div class="err">${esc(d.error)}</div>` : "");
}

// A draft on a task with no conversation to answer (a to-do of the user's): the same message box as
// every card, with To and Subject to fill in, Send, a Gmail draft, and Change it (the user, Nov 15:
// the user's own to-do cards do everything the others do).
function c5owndraft(d, text){
  const ed = CARD.own[d.key] || {to: d.draft_to || "", subject: d.draft_subject || d.title || ""};
  const rows = Math.min(14, Math.max(3, text.split("\n").reduce((n, l) => n + Math.max(1, Math.ceil(l.length / 70)), 0) + 1));
  return `<div class="c5email" data-c5own><div class="c5ef"><span>To</span><input data-f="to" value="${esc(ed.to)}" placeholder="Email addresses, separated by commas"></div>`
    + `<div class="c5ef"><span>Subject</span><input data-f="subject" value="${esc(ed.subject)}"></div>`
    + `<textarea class="c5draft" data-c5draft rows="${rows}">${esc(text)}</textarea>`
    + `<div class="c5row"><button class="btn primary" data-c5sendown title="Sends this email from your Gmail. You get 10 seconds to undo.">Send</button>`
    + `<button class="btn quiet" data-c5gmailown title="Puts it in your Gmail drafts. Nothing is sent.">Save as a Gmail draft</button>`
    + `<button class="btn quiet" data-c5change title="Say what should change, in the bar at the bottom">Change it</button></div></div>`
    + (d.error ? `<div class="err">${esc(d.error)}</div>` : "");
}

const C5_SPARK = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" aria-hidden="true"><path d="M8 1.8 9.4 6.6 14.2 8 9.4 9.4 8 14.2 6.6 9.4 1.8 8 6.6 6.6z"/></svg>';

function fmtWhen(iso){ const t = new Date(iso); return isNaN(t) ? iso : t.toLocaleString([], {weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}); }

function cardInner(r, viewName, d){
  let h = d.finished ? `<div class="c5done">Finished: ${esc(d.finished)} <button class="link" data-c5bringback title="Back on your list. Nothing is sent.">Bring it back</button></div>` : "";
  h += c5msgs(d);
  // Threads the user attached to this task: Today reads them with it. Each opens the whole saved conversation.
  // One Today found itself says why; any can be taken off (Today won't attach it again).
  if((d.attached || []).length) h += `<div class="c5att">Also reading ${d.attached.map((a, i) => `<span class="c5atti"><a href="/thread?c=${encodeURIComponent(a.conversation)}" title="${esc(a.by === "today" ? "Today found this is about the task: " + a.why + ". Opens the whole saved conversation." : "The whole saved conversation")}">${esc(a.title)}</a>${a.kind ? ` (${esc(a.kind)})` : ""}${i < d.attached.length - 1 ? "," : ""}<button class="c5sx" data-c5detach="${esc(a.conversation)}" title="Take this off the task. Today won't read it with the task or attach it again.">&times;</button></span>`).join(" ")}</div>`;
  const steps = d.steps || [];
  const cur = steps.findIndex(s => !s.done);
  const opened = openedSteps(d.key);
  const shows = s => !s.done && (s.cid === (cur >= 0 ? steps[cur].cid : "") || opened.has(s.cid)) || (s.done && opened.has(s.cid));
  const foc = cur >= 0 ? steps[cur].cid : (steps.some(s => opened.has(s.cid)) ? [...opened][0] : "");
  // What was done on the task as a whole (like making these steps) stays visible above them,
  // with its way back.
  if(foc && d.act && d.act.turns.length && !CARD.busy[d.key + "|"]){
    const t = d.act.turns[d.act.turns.length - 1];
    h += c5said(t, `data-c5back data-c5task title="Take that back"`, t.did ? `<div class="c5result">${esc(t.did)}</div>` : "");
  }
  if(foc && CARD.busy[d.key + "|"]) h += `<div class="c5writing">${esc(CARD.busy[d.key + "|"])}</div>`;
  if(steps.length || CARD.adding[d.key]){
    h += `<ul class="c5steps">` + steps.map(s => `<li class="${s.done ? "done" : ""}${shows(s) ? " cur" : ""}" data-c5focus="${esc(s.cid)}">`
      + `<div class="c5sl"><button class="c5sq" data-c5tick="${esc(s.cid)}" title="${s.done ? "Not done yet" : "Done. Nothing is sent."}"></button><span class="c5st" contenteditable="true" spellcheck="true" data-c5edit="${esc(s.cid)}">${esc(s.title)}</span>`
      + `<button class="c5sx" data-c5del="${esc(s.cid)}" title="Remove this step">&times;</button>`
      + (s.cid === foc && !opened.has(s.cid) ? `<span class="c5gen"></span>` : `<button class="c5gen${opened.has(s.cid) ? " on" : ""}" data-c5gen="${esc(s.cid)}" title="${opened.has(s.cid) ? "Hide this step's options" : "Options for this step, to do it now alongside the others"}" aria-label="Options for this step">${C5_SPARK}</button>`)
      + `</div>` + (s.note || s.url ? `<div class="c5note">${esc(s.note || "")}${s.url ? ` <a href="${esc(s.url)}" target="_blank" rel="noopener">Open</a>` : ""}</div>` : "")
      + (shows(s) ? `<div class="c5under">${c5act(r, d, s.cid)}</div>` : "") + `</li>`).join("")
      + (CARD.adding[d.key] ? `<li><div class="c5sl"><span class="c5sq dashed"></span><input class="c5new" data-c5new placeholder="New step, then Enter"></div></li>` : "") + `</ul>`;
  }
  if(!foc) h += c5act(r, d, "");
  else{
    if(!(d.step_act[foc] || {turns: []}).turns.some(t => t.draft)) h += c5draft(d);
    h += c5made(d);
  }
  h += c5notes(d);
  h += `<div class="c5foot"><button data-c5add title="Add a step to this task">+ Step</button><button data-c5notnow title="${esc(viewName === "now" ? TIPS.notnowNow : TIPS.notnow)}">Not now</button><button data-c5more title="Plan in Week, not mine, drop, or open the full page" aria-label="More">&middot;&middot;&middot;</button></div>`;
  return h;
}

// What the user told the assistant about this task, kept short; every AI on the task reads them.
// Folded to one quiet line until the user opens it (the user, Nov 15: notes shouldn't noise up the card).
function c5notes(d){
  const ns = d.notes || [];
  if(!ns.length) return "";
  const open = !!CARD.notesOpen[d.key];
  let h = `<div class="c5notes"><button class="c5nt" data-c5notes title="${open ? "Hide the notes" : "What you've told Today about this task. Every part of Today that works on it reads these."}">Notes (${ns.length})<span class="c5car">${open ? "&#9662;" : "&#9656;"}</span></button>`;
  if(open) h += `<ul>` + ns.map(n => `<li title="${esc(n.said ? "From what you said on " + n.at + ": " + n.said : n.at)}"><span>${esc(n.text)}${n.step ? `<span class="c5nstep"> · ${esc(n.step)}</span>` : ""}</span>`
    + `<button class="c5sx" data-c5unnote="${esc(n.id)}" title="Remove this note. Today won't write it again.">&times;</button></li>`).join("") + `</ul>`;
  return h + `</div>`;
}

async function loadCard(el, reload){
  const box = el.querySelector("[data-card]"); if(!box) return;
  const key = box.dataset.card;
  try{ CARD.data[key] = await api("/api/card?key=" + encodeURIComponent(key)); }
  catch(e){ box.innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
  drawCard(el, reload);
}

// While the user is typing in a card (a step's words, the reply, a new step), a redraw waits until the user
// leaves the box, so nothing the user typed is lost and Enter never lands on the list instead.
function typingIn(el){ const a = document.activeElement; return !!(a && el.contains(a) && a.matches("[contenteditable],input,textarea")); }
document.addEventListener("focusout", () => { setTimeout(() => { if(CARD.down) return; const later = [...CARD.later]; CARD.later.clear(); later.forEach(f => f()); }, 0); }, true);

function drawCard(el, reload, now=false){
  if(!now && (CARD.down || typingIn(el))){ CARD.later.add(() => drawCard(el, reload)); return; }
  const box = el.querySelector("[data-card]"); if(!box || !document.body.contains(el)) return;
  const key = box.dataset.card, d = CARD.data[key], r = rowsByKey[key] || {key}, viewName = el.dataset.view;
  if(!d){ loadCard(el, reload); return; }
  box.innerHTML = cardInner(r, viewName, d);
  bindCard(el, reload);
  // The first options for what is in focus are worked out once, then kept on the task.
  box.querySelectorAll("[data-c5opening]").forEach(opening => {
    const focus = opening.closest("[data-c5focus]") ? opening.closest("[data-c5focus]").dataset.c5focus : "";
    if(CARD.busy[key + "|open|" + focus]) return;
    CARD.busy[key + "|open|" + focus] = 1;
    optionsSoon(() => api("/api/assistant/open", {key, focus}).then(v => { if(focus) d.step_act[focus] = v; else d.act = v; })
      .catch(e => { if(focus) d.step_act[focus] = {turns: [], next: [], ready: true}; else d.act = {turns: [], next: [], ready: true}; toast(e.message); })
      .finally(() => { delete CARD.busy[key + "|open|" + focus]; drawCard(el, reload); }));
  });
}

// The user tapped an option or said something (from the bar at the bottom): the assistant does it,
// then the card shows what was done and what comes next.
// The one assistant's answer where it isn't a card (a project's box, the bottom bar): what the user
// said or picked, with Other options to take it back, what it did, and the next options. The
// same look and words as a card's (the user, 2026-11-15: one assistant everywhere).
function assistHtml(v, busy){
  if(busy) return `<div class="c5writing">${esc(busy)}</div>`;
  const turns = (v && v.turns) || [], last = turns[turns.length - 1];
  if(!last) return "";
  let h = c5said(last, `data-c5back title="Take that back and see the other options again"`, last.did ? `<div class="c5result">${esc(last.did)}</div>` : "");
  if(v.next && v.next.length) h += `<div class="c5opts">${v.next.map((o, i) => `<button class="btn" data-c5opt="${i}" title="${esc(o.say)}">${esc(o.label)}</button>`).join("")}</div>`;
  return h;
}
// Its buttons: an option asks again with what it says; Other options takes the last answer back.
function bindAssist(root, v, onSay, onBack){
  if(!root) return;
  root.querySelectorAll("[data-c5opt]").forEach(b => b.onclick = () => { const o = ((v && v.next) || [])[+b.dataset.c5opt]; if(o) onSay(o.say, o.label); });
  root.querySelectorAll("[data-c5back]").forEach(b => b.onclick = onBack);
}

async function cardAct(el, reload, key, say, label, focus){
  const d = CARD.data[key];
  CARD.busy[key + "|" + focus] = "Working on it…"; if(el) drawCard(el, reload);
  let res;
  try{ res = await api("/api/assistant", {scope: {kind: "task", key, focus}, say, label}); }
  catch(e){ delete CARD.busy[key + "|" + focus]; if(el) drawCard(el, reload); toast(e.message); return; }
  delete CARD.busy[key + "|" + focus];
  delete xpEdits[key];
  if(res.gone){ if(el) finishRow(el); delete CARD.data[key]; toastUndo(res.turns[res.turns.length - 1].did || "Done", async () => { await api("/api/assistant/back", {scope: {kind: "task", key, focus}}); reload(); }); reload(); return; }
  if(d) CARD.data[key] = await api("/api/card?key=" + encodeURIComponent(key));
  if(el) drawCard(el, reload);
}

function bindCard(el, reload){
  const box = el.querySelector("[data-card]"); if(!box) return;
  const key = box.dataset.card, d = CARD.data[key], r = rowsByKey[key] || {key}, q = s => box.querySelector(s);
  const focusOf = node => { if(node.hasAttribute("data-c5task")) return ""; const li = node.closest("[data-c5focus]"); return li ? li.dataset.c5focus : ""; };
  const optsOf = focus => ((focus ? d.step_act[focus] : d.act) || {}).next || [];
  box.onclick = e => e.stopPropagation();
  if(q("button[data-c5earlier]")) q("button[data-c5earlier]").onclick = () => { CARD.threadUp[key] = true; drawCard(el, reload); };
  if(q("[data-c5newer]")) q("[data-c5newer]").onclick = () => { CARD.threadDown[key] = true; drawCard(el, reload); };
  box.querySelectorAll("[data-c5opt]").forEach(b => b.onclick = () => { const focus = focusOf(b), o = optsOf(focus)[+b.dataset.c5opt]; if(o) cardAct(el, reload, key, o.say, o.label, focus); });
  box.querySelectorAll("[data-c5back]").forEach(b => b.onclick = async () => { const focus = focusOf(b);
    CARD.busy[key + "|" + focus] = "Taking that back…"; drawCard(el, reload);
    try{ await api("/api/assistant/back", {scope: {kind: "task", key, focus}}); CARD.data[key] = await api("/api/card?key=" + encodeURIComponent(key)); }
    catch(e){ toast(e.message); }
    delete CARD.busy[key + "|" + focus]; delete xpEdits[key]; drawCard(el, reload); });
  const ta = q("[data-c5draft]");
  // Every edit to a drafted message is saved as the user types, and again when the user leaves the page
  // (the user, Nov 15: edits were lost); the card's copy follows so a redraw shows them too.
  const keep = (field, value, body) => {
    clearTimeout(CARD.saveT[key + field]);
    // keepalive: a save started as the page closes still arrives.
    const payload = {key, ...body()}, slot = "todayEdit:" + key + "|" + field;
    try{ localStorage.setItem(slot, JSON.stringify({url: "/api/draft/edit", payload})); }catch(e){}
    const save = () => { delete CARD.pending[key + field]; clearTimeout(CARD.saveT[key + field]);
      return postEdit(slot, "/api/draft/edit", payload); };
    CARD.pending[key + field] = save;
    CARD.saveT[key + field] = setTimeout(save, 700);
  };
  if(ta && !ta.closest("[data-c5email]") && !(d.user_draft)){
    const own = ta.closest("[data-c5own]"), of = n => own.querySelector(`[data-f="${n}"]`).value;
    const body = () => own ? {text: ta.value, to: of("to"), subject: of("subject")} : {text: ta.value};
    const track = () => { xpEdits[key] = ta.value; if(d.state === "waiting") d.nudge = ta.value; else d.draft = ta.value;
      if(own){ d.draft_to = of("to"); d.draft_subject = of("subject"); } keep("draft", ta.value, body); };
    ta.oninput = track;
    if(own) own.querySelectorAll("input").forEach(i => i.addEventListener("input", track));
  } else if(ta && d.user_draft){
    // The user's own Gmail draft: the edit goes into that draft in Gmail (never sent), a little less often.
    const slot = "todayEdit:" + key + "|user";
    const saveUser = () => { delete CARD.pending[key + "user"]; clearTimeout(CARD.saveT[key + "user"]);
      const text = ta.value; if(!text.trim()) return;
      return postEdit(slot, "/api/user-draft", {key, message_id: d.user_draft.message_id, text},
        j => { if(j.message_id) d.user_draft.message_id = j.message_id; d.user_draft.text = text; }); };
    ta.oninput = () => { xpEdits[key] = ta.value;
      try{ localStorage.setItem(slot, JSON.stringify({url: "/api/user-draft", payload: {key, message_id: d.user_draft.message_id, text: ta.value}})); }catch(e){}
      clearTimeout(CARD.saveT[key + "user"]); CARD.pending[key + "user"] = saveUser; CARD.saveT[key + "user"] = setTimeout(saveUser, 1500); };
  } else if(ta) ta.oninput = () => { xpEdits[key] = ta.value; };
  box.querySelectorAll("[data-c5email]").forEach(card => {
    const i = +card.dataset.c5email, f = n => card.querySelector(`[data-f="${n}"]`).value;
    const track = () => { const e = d.emails[i]; if(e){ e.body = f("body"); e.to = f("to"); e.subject = f("subject"); }
      keep("email" + i, "", () => ({index: i, text: f("body"), to: f("to"), subject: f("subject")})); };
    card.querySelectorAll("input,textarea").forEach(x => x.addEventListener("input", track));
  });
  const send = q("[data-c5send]");
  if(send) send.onclick = async () => { const text = ta ? ta.value : ""; if(!text.trim()){ toast("The message is empty"); return; }
    send.disabled = true;
    const own = send.dataset.own && d.user_draft;
    try{ await sendSoon({key, text, who: d.who || "them", ...(own ? {what: "user_draft", fields: {message_id: d.user_draft.message_id}} : {}),
      gone: () => { delete xpEdits[key]; finishRow(el); }, undone: () => { xpEdits[key] = text; reload(); }, after: reload}); }
    catch(e){ send.disabled = false; toast(e.message); } };
  box.querySelectorAll("[data-c5sendemail]").forEach(b => b.onclick = async () => {
    const card = b.closest("[data-c5email]"), f = n => card.querySelector(`[data-f="${n}"]`).value;
    b.disabled = true;
    try{ await sendSoon({key, text: f("body"), what: "email", fields: {index: +card.dataset.c5email, to: f("to"), cc: "", subject: f("subject")}, who: f("to").split("<")[0].trim() || "them",
      gone: () => { card.style.opacity = ".45"; }, after: () => { delete CARD.data[key]; reload(); }}); }
    catch(e){ b.disabled = false; toast(e.message); } });
  if(q("[data-c5change]")) q("[data-c5change]").onclick = () => dockRef(key);
  if(q("[data-c5copy]")) q("[data-c5copy]").onclick = async () => { try{ await navigator.clipboard.writeText(ta.value); toast("Copied"); } catch(e){ ta.select(); toast("Selected. Copy it from here"); } };
  const own = q("[data-c5own]");
  if(own){
    const of = n => own.querySelector(`[data-f="${n}"]`).value;
    own.querySelectorAll("input").forEach(i => i.oninput = () => { CARD.own[key] = {to: of("to"), subject: of("subject")}; });
    q("[data-c5sendown]").onclick = async e => { const b = e.currentTarget, text = ta.value;
      if(!text.trim()){ toast("The message is empty"); return; }
      b.disabled = true;
      try{ await sendSoon({key, text, what: "email", fields: {index: -1, to: of("to"), cc: "", subject: of("subject")}, who: of("to").split("<")[0].trim() || "them",
        gone: () => { delete xpEdits[key]; delete CARD.own[key]; finishRow(el); }, undone: () => { xpEdits[key] = text; reload(); }, after: () => { delete CARD.data[key]; reload(); }}); }
      catch(err){ b.disabled = false; toast(err.message); } };
    q("[data-c5gmailown]").onclick = async () => {
      try{ const res = await api("/api/gmail-draft", {key, index: -1, to: of("to"), cc: "", subject: of("subject"), text: ta.value}); toast(res.message); }
      catch(err){ toast(err.message); } };
  }
  if(q("[data-c5saveuser]")) q("[data-c5saveuser]").onclick = async () => { try{ const res = await api("/api/user-draft", {key, message_id: d.user_draft.message_id, text: ta.value}); toast(res.message); } catch(e){ toast(e.message); } };
  box.querySelectorAll("[data-c5cal]").forEach(b => b.onclick = async () => { b.disabled = true;
    try{ await api("/api/calendar/add", {key, index: +b.dataset.c5cal}); toast("On your calendar"); CARD.data[key] = await api("/api/card?key=" + encodeURIComponent(key)); drawCard(el, reload); }
    catch(e){ b.disabled = false; toast(e.message); } });
  if(q("[data-c5bringback]")) q("[data-c5bringback]").onclick = async () => { const u = await api("/api/undo-finish", {key}); toast(u.message); delete CARD.data[key]; reload(); };
  // Steps: click one for its own options; tick, edit in place, remove.
  box.querySelectorAll("[data-c5gen]").forEach(b => b.onclick = () => { const set = openedSteps(key), cid = b.dataset.c5gen;
    if(set.has(cid)) set.delete(cid); else set.add(cid);
    drawCard(el, reload); });
  box.querySelectorAll("[data-c5edit]").forEach(s => {
    s.onkeydown = e => { if(e.key === "Enter"){ e.preventDefault(); s.blur(); } if(e.key === "Escape"){ s.textContent = (d.steps.find(x => x.cid === s.dataset.c5edit) || {}).title || ""; s.blur(); } };
    s.onblur = async () => { const t = s.textContent.replace(/\s+/g, " ").trim(), st = d.steps.find(x => x.cid === s.dataset.c5edit);
      if(!st || !t || t === st.title) return;
      try{ await api("/api/projects/step/edit", {cid: st.cid, title: t}); st.title = t; } catch(e){ toast(e.message); } };
  });
  box.querySelectorAll("[data-c5tick]").forEach(b => b.onclick = async () => { const st = d.steps.find(x => x.cid === b.dataset.c5tick);
    const done = !st.done;
    st.done = done; openedSteps(key).delete(st.cid); drawCard(el, reload);
    try{ const res = await api("/api/projects/step/tick", {cid: st.cid, done});
      if(res.all_done) toast("Every step is done. Check the task off when it's finished."); }
    catch(e){ st.done = !done; drawCard(el, reload); toast(e.message); } });
  box.querySelectorAll("[data-c5del]").forEach(b => b.onclick = async () => { const cid = b.dataset.c5del;
    const res = await api("/api/projects/step/remove", {cid});
    d.steps = d.steps.filter(x => x.cid !== cid); drawCard(el, reload);
    toastUndo("Step removed", async () => { await api("/api/undo", {batch: res.batch}); CARD.data[key] = await api("/api/card?key=" + encodeURIComponent(key)); drawCard(el, reload); }); });
  box.querySelectorAll("[data-c5detach]").forEach(b => b.onclick = async () => { const conversation = b.dataset.c5detach;
    let res; try{ res = await api("/api/card/detach", {key, conversation}); } catch(e){ toast(e.message); return; }
    d.attached = d.attached.filter(a => a.conversation !== conversation); drawCard(el, reload);
    toastUndo("Taken off the task", async () => { await api("/api/undo", {batch: res.batch}); CARD.data[key] = await api("/api/card?key=" + encodeURIComponent(key)); drawCard(el, reload); }); });
  if(q("[data-c5notes]")) q("[data-c5notes]").onclick = () => { CARD.notesOpen[key] = !CARD.notesOpen[key]; drawCard(el, reload); };
  box.querySelectorAll("[data-c5unnote]").forEach(b => b.onclick = async () => { const id = b.dataset.c5unnote;
    let res; try{ res = await api("/api/card/note/remove", {id}); } catch(e){ toast(e.message); return; }
    d.notes = d.notes.filter(n => n.id !== id); drawCard(el, reload);
    toastUndo("Note removed", async () => { await api("/api/undo", {batch: res.batch}); CARD.data[key] = await api("/api/card?key=" + encodeURIComponent(key)); drawCard(el, reload); }); });
  q("[data-c5add]").onclick = () => { CARD.adding[key] = true; drawCard(el, reload); const n = box.querySelector("[data-c5new]"); if(n) n.focus(); };
  const nw = q("[data-c5new]");
  if(nw) nw.onkeydown = async e => {
    if(e.key === "Escape"){ delete CARD.adding[key]; drawCard(el, reload); return; }
    if(e.key !== "Enter" || !nw.value.trim()) return;
    const title = nw.value.trim(); nw.value = "";
    try{ await api("/api/projects/step/add", {task: d.task, key, title}); CARD.data[key] = await api("/api/card?key=" + encodeURIComponent(key)); }
    catch(err){ toast(err.message); }
    // The user is still in the new-step box: show the step now and keep the user's there.
    drawCard(el, reload, true); const again = box.querySelector("[data-c5new]"); if(again) again.focus(); };
  // In Now, Not now puts it back in its own list; anywhere else it moves to Slipped (the user, 2026-11-16).
  q("[data-c5notnow]").onclick = async () => { const fromNow = el.dataset.view === "now"; finishRow(el, false);
    await api("/api/not-now", {key, from_now: fromNow});
    toastUndo(fromNow ? "Out of Now, back where it was. Nothing sent" : "Moved to Slipped. Nothing sent", async () => { await api("/api/not-now", {key, on: false, from_now: fromNow}); reload(); }); reload(); };
  q("[data-c5more]").onclick = e => cardMenu(e.currentTarget, el, key, reload);
}

// Moving a task between projects is one undoable change to the task (project_book.task_action).
async function cardMove(el, key, to, reload){
  try{ const res = await api("/api/projects/task", {id: "", key, action: "move", to});
    if(view.page === "proj") finishRow(el);
    toastUndo(res.message, async () => { await api("/api/undo", {batch: res.batch}); reload(); }); }
  catch(e){ toast(e.message); }
  reload();
}
async function cardProjects(){ if(!PJ.list) PJ.list = await api("/api/projects"); return PJ.list.projects || []; }

function cardMenu(anchor, el, key, reload){
  const d = CARD.data[key] || {};
  const inw = !!((S.plan || {}).in_week || {})[key];
  menu(anchor, [
    ["Move to a project…", async () => { const here = view.page === "proj" ? view.id : "";
      const ps = (await cardProjects()).filter(p => p.id !== here);
      menu(anchor, ps.map(p => [p.title, () => cardMove(el, key, p.id, reload)]).concat(here ? [["No project", () => cardMove(el, key, "", reload)]] : [])); }],
    [inw ? "Take off Week's list" : "Plan in Week", async () => { await api("/api/in-week", {key, on: !inw}); toast(inw ? "Off Week's list" : "On Week's list. Give it a morning there when you want."); reload(); }],
    ["Not mine", async () => { finishRow(el); const res = await api("/api/plan", {key, choice: "notmine"}); toast(res.message, key, reload); reload(); }],
    ["Drop it", async () => { finishRow(el); const res = await api("/api/ignore", {key}); toast(res.message, key, reload); reload(); }],
    ...(d.service === "Gmail" && d.draft && !d.user_draft ? [["Save as a Gmail draft", async () => { try{ const res = await api("/api/gmail-draft", {key, text: xpEdits[key] ?? d.draft}); toast(res.message); } catch(e){ toast(e.message); } }]] : []),
    ...(view.page === "item" ? [] : [["Open the full page", () => go("#" + encodeURIComponent(key))]]),
  ]);
}

const CARD_CSS = document.createElement("style");
CARD_CSS.textContent = `
.c5{font-size:15px}
.c5thread{border-left:2px solid #eee9df;padding-left:12px;margin:2px 0 12px}
.c5att{font-size:13px;color:#a39d92;margin:-4px 0 12px}
.c5att a{color:#6b665d;text-decoration:underline;text-decoration-color:#d9d4c8;text-underline-offset:2px}
.c5att a:hover{color:#1c1b19}
.c5earlier{border:0;background:none;color:#a39d92;font:inherit;font-size:12.5px;padding:0 0 4px;cursor:pointer}
.c5earlier:hover{color:#1c1b19}
.c5newer{display:block;padding:2px 0 0}
a.c5earlier{text-decoration:none}
.c5msg.c5src{color:#1c1b19}
.c5msg.reply{margin-left:12px}
.c5msg{font-size:13px;line-height:1.5;color:#1c1b19;margin-bottom:8px}
.c5msg.me{color:#6b665d}
.c5mh{display:flex;align-items:center;gap:6px}
.c5from{font-weight:600;color:#1c1b19}
.c5at{color:#a39d92;font-size:12px}
.c5out{margin-left:auto;display:inline-flex;align-items:center;gap:2px;color:#a39d92;text-decoration:none;padding:1px 3px;border-radius:5px;align-self:flex-start}
.c5out:hover{color:#1c1b19;background:#f3f0ea}
.c5out.c5from-app:hover{background:none}
.c5svc{font-size:12px}
.c5mt{white-space:pre-wrap;overflow-wrap:anywhere}
ul.c5steps{list-style:none;margin:0 0 6px;padding:0}
ul.c5steps>li{padding:3px 0}
.c5sl{display:flex;gap:10px;align-items:flex-start;line-height:1.55}
/* A step's box sits on its first line, however many lines the words wrap to */
.c5sl>.c5sq{font-size:inherit;margin-top:calc((1.55em - 14px) / 2)}
.c5sl>.c5gen,.c5sl>.c5sx{font-size:inherit;height:1.55em}
.c5sq{width:14px;height:14px;border:1.5px solid #c9c3b6;border-radius:4px;flex-shrink:0;cursor:pointer;background:#fff;padding:0}
.c5sq.dashed{border-style:dashed;cursor:default}
li.done .c5sq{background:#9b84c4;border-color:#9b84c4}
li.done .c5st{color:#a39d92;text-decoration:line-through}
li.cur>.c5sl .c5st{font-weight:500}
.c5st{flex:1;outline:0;border-radius:4px;padding:0 2px}
.c5st:focus{background:#faf8f4}
.c5sx{border:0;background:none;color:#c9c3b6;cursor:pointer;font-size:16px;line-height:1;padding:0 2px;opacity:0}
ul.c5steps li:hover .c5sx{opacity:1}
.c5notes li:hover .c5sx{opacity:1}
.c5atti:hover .c5sx{opacity:1}
@media (hover:none){.c5att .c5sx{opacity:1}}
@media (hover:none){.c5notes .c5sx{opacity:1}}
.c5sx:hover{color:#1c1b19}
.c5gen{width:22px;height:22px;flex-shrink:0;border:0;background:none;color:#c9c3b6;cursor:pointer;padding:0;border-radius:6px;display:inline-flex;align-items:center;justify-content:center;opacity:0}
span.c5gen{cursor:default}
ul.c5steps li:hover .c5gen,.c5gen.on,.c5gen:focus{opacity:1}
.c5gen:hover{color:#6b5a8e;background:#f3f0ea}.c5gen.on{color:#9b84c4}
@media (pointer:coarse){.c5gen{opacity:1}}
.c5new{border:0;font:inherit;flex:1;outline:0;background:transparent}
li.cur>.c5under{margin:6px 0 4px 24px}
.c5note{font-size:13px;color:#a39d92;margin:1px 0 0 24px}.c5note a{color:#6b665d}
.c5opts{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 6px}
.c5opts .btn{padding:3px 11px;border-radius:999px;border-color:#e3ded4;background:#faf9f6;white-space:normal;text-align:left;max-width:100%}
.c5opts .btn:hover{border-color:#cfc8bb;background:#f3f0ea}
.c5said{background:#faf9f6;border-radius:10px;padding:9px 12px;margin:0 0 12px}
.c5chosen{display:flex;align-items:baseline;gap:8px;color:#a39d92;margin:0}
.c5chosen button{margin-left:auto;border:0;background:none;color:#a39d92;font:inherit;cursor:pointer;padding:0;white-space:nowrap}
.c5chosen button:hover{color:#1c1b19;text-decoration:underline}
.c5her{font-weight:500;color:#1c1b19;margin:1px 0 0}
.c5result{color:#1c1b19;margin:6px 0 0}
.c5result:before{content:"\\2713  ";color:#9b84c4}
.c5under .c5said,.c5said:last-child{margin-bottom:8px}
.c5draft{width:100%;font:inherit;font-size:14px;border:1px solid #e7e3da;border-radius:8px;padding:7px 10px;background:#fff;color:#1c1b19;resize:vertical;margin:0 0 7px;display:block}
.c5draft:focus{outline:0;border-color:#c9bde0}
.c5row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:6px}
.c5row .btn.quiet{border-color:transparent;background:none;color:#6b665d}
.c5row .btn.quiet:hover{color:#1c1b19}
.c5saved{font-size:var(--c5small,13px);color:#a39d92}
.c5email{min-width:0;border:1px solid #eee9df;border-radius:10px;padding:8px 10px 2px;margin:0 0 8px}
.c5ef{display:flex;gap:8px;align-items:center;font-size:13px;color:#a39d92;margin-bottom:4px}
.c5ef input{flex:1;min-width:0;font:inherit;font-size:14px;border:0;border-bottom:1px solid #f0ede6;padding:2px 0;color:#1c1b19;outline:0}
.c5writing{color:#a39d92;font-style:italic;font-size:14px;padding:4px 0 8px}
.c5label{font-size:13px;color:#a39d92;margin:0 0 4px}
.c5done{font-size:14px;color:#6b665d;background:#f4f8f3;border-radius:8px;padding:6px 10px;margin:0 0 10px}
/* A task on its own page: the same card, the whole width */
.itempage .c5{padding:4px 0 0}
.itempage .ititle{font-size:20px;font-weight:600;margin:6px 0 2px}
.itempage .imeta{font-size:13px;color:#6b665d;margin:0 0 14px}
.itempage .imeta a{color:#6b665d}
.c5notes{margin:10px 0 2px}
.c5nt{border:0;background:none;padding:0;font:inherit;color:#a39d92;cursor:pointer}
.c5nt:hover{color:#1c1b19}
.c5car{margin-left:5px}
.c5notes ul{list-style:none;margin:6px 0 0;padding:0}
.c5notes li{display:flex;align-items:flex-start;gap:8px;padding:5px 0;border-top:1px solid #f0ece4;color:#1c1b19}
.c5notes li span{flex:1}
.c5nstep{color:#a39d92}
.c5foot{display:flex;gap:16px;margin-top:12px;padding-top:10px;border-top:1px solid #f0ede6;font-size:13px}
.c5foot button{border:0;background:none;color:#a39d92;font:inherit;cursor:pointer;padding:0}
.c5foot button:hover{color:#1c1b19}
/* One text size on a card below the title (the user, Nov 15), the labels a step smaller: what the user
   reads and writes (what was done, the draft, To and Subject, the options) in the body size, the
   quiet labels and actions (You picked, To, Subject, + Step) in the small one. The email reads as
   one sheet: no box inside the box, labels one width so To and Subject start together. */
.c5{--c5body:14px;--c5small:13px}
.c5result,.c5her,.c5draft,.c5ef input,.c5opts .btn,.c5writing,.c5done{font-size:var(--c5body);line-height:1.55}
.c5chosen,.c5chosen button,.c5ef,.c5label,.c5note,.c5att,.c5notes,.c5foot{font-size:var(--c5small)}
.c5email{padding:4px 12px}
.c5ef{margin:0;padding:6px 0;border-bottom:1px solid #f0ede6;align-items:baseline}
.c5ef span{flex:none;width:56px}
.c5ef input{border:0;padding:0}
.c5email .c5draft{border:0;border-radius:0;padding:10px 0 6px;margin:0;background:transparent}
.c5email .c5row{padding:4px 0 6px;margin:0}
`;
document.head.appendChild(CARD_CSS);
