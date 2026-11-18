// Projects (the Projects tab and each project's page) and the feedback bar on the Today list.
// Loaded before the page's main script; everything here runs only when that script calls it,
// and uses its helpers (api, esc, toast, toastUndo, itemHtml, todoHtml, bindItems, menu, go...).
// The user approved both on a hand mock, 2026-11-13. One font size; weight and grey only; the
// person's own words; tooltips for explanations; Undo instead of confirm pop-ups; nothing sends.

const ASK_SVG = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M8 9h8"/><path d="M8 13h5"/></svg>`;
const ASK_TIP_LIST = "Something wrong with this? Tell Today. Shift-click to add several.";
const ASK_TIP_PROJ = "Not the right next move? Tell Today what this task needs.";
const LIST_VIEWS = ["now", "replies", "answers", "todos", "later", "waiting"];

const isProjView = v => String(v || "").startsWith("proj:");
const PJ = { data: null, changed: new Set(), about: null, box: "", working: false, wordsOpen: false, editWords: false,
  showRest: false, addq: "", stepOpen: {}, showSugg: false, openSugg: null, showDone: false, list: null };
const DOCK = { text: "", refs: [], shut: localStorage.getItem("todayDockShut") === "1", working: false, view: null, keys: [] };

// ---- pieces the Today page's item component draws ---------------------------------------
// The row's status column (progress, project name) and its icon column, which is always there in
// a list so the icons line up whether or not a row has a date.
function rowRight(r, viewName){
  if(!r || !(LIST_VIEWS.includes(viewName) || isProjView(viewName))) return "";
  const name = !isProjView(viewName) && r.project
    ? `<button class="projname" data-openproj="${esc(r.project.id)}" title="${esc("Open your project: "+r.project.full)}">${esc(r.project.title)}</button>` : "";
  return progressRing(r) + name;
}
function rowAsk(r, viewName){
  if(!r || !(LIST_VIEWS.includes(viewName) || isProjView(viewName))) return viewName === "filed" ? `<span class="askslot"></span>` : "";
  return `<button class="askicon" data-ask="${esc(r.key)}" title="${esc(isProjView(viewName)?ASK_TIP_PROJ:ASK_TIP_LIST)}" aria-label="Tell Today">${ASK_SVG}</button>`;
}
function rowMarks(r, viewName){
  return (isProjView(viewName) && PJ.changed.has(r.key) ? " changed" : "") + (!isProjView(viewName) && DOCK.refs.includes(r.key) ? " reffed" : "");
}
// The ask itself darker, the rest of their message grey (in a project's open task).
// Their message, word for word, read the way they wrote it: an email's hard line breaks (about
// every 70 characters) are joined back into paragraphs, and the earlier messages it quotes
// ("On ... wrote:" and below) stay in "Whole thread" (the user, 2026-11-14).
function tidyMsg(text){
  let t = String(text || "").replace(/\r/g, "");
  const quoted = t.search(/\n[^\n]{0,40}On [^\n]{4,160}(\n[^\n]{0,120})?wrote:\s*(\n|$)/);
  if(quoted > 0) t = t.slice(0, quoted);
  t = t.split("\n").filter(l => !/^\s*>/.test(l)).join("\n");
  return t.replace(/([^\n]{55,})\n(?=[^\s\n])/g, "$1 ").replace(/\n{3,}/g, "\n\n").trim();
}
function askWords(r, words){
  const ask = String(r.ask || "").trim(), w = tidyMsg(words);
  const at = ask.length > 8 ? w.indexOf(ask) : -1;
  if(at < 0) return esc(w);
  return `<span class="soft">${esc(w.slice(0, at))}</span>${esc(ask)}<span class="soft">${esc(w.slice(at + ask.length))}</span>`;
}
// A task's steps: a compact checklist under its title (Things-style, the user 2026-11-14). The first
// step not done is the current one; it alone shows its note and its one way to do it.
// The current step's one way to do it, by its kind.
function progressRing(r){
  if(!r || !r.step_total) return "";
  const d = r.steps_done || 0, n = r.step_total, a = n ? d / n : 0, rad = 7, c = 2 * Math.PI * rad;
  return `<span class="prog" title="${d} of ${n} steps done"><svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><circle cx="9" cy="9" r="${rad}" fill="none" stroke="#e7e3da" stroke-width="2"/><circle cx="9" cy="9" r="${rad}" fill="none" stroke="#2f6b35" stroke-width="2" stroke-dasharray="${(c * a).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 9 9)" stroke-linecap="round"/></svg>${d}/${n}</span>`;
}
function guessHtml(r, viewName){
  if(isProjView(viewName) || !r.guess) return "";
  return `<div class="guess">Part of ${esc(r.guess.title)}? <button class="link" data-guess="${esc(r.guess.id)}" title="Puts it in ${esc(r.guess.full)}. You can move it out again.">Yes</button></div>`;
}
// An open task of the user's own in a project: its step, one at a time, and the rare actions in a menu.
function bindRowExtras(el, reload){
  const key = el.dataset.item, viewName = el.dataset.view;
  el.querySelectorAll("[data-ask]").forEach(b => b.onclick = e => {
    e.stopPropagation();
    dockRef(key, e.shiftKey);  // the bar at the bottom, on every page (the user, 2026-11-14)
  });
  el.querySelectorAll("[data-openproj]").forEach(b => b.onclick = e => { e.stopPropagation(); go("#p:" + encodeURIComponent(b.dataset.openproj)); });
  const g = el.querySelector("[data-guess]");
  if(g) g.onclick = async e => { e.stopPropagation(); const res = await api("/api/projects/file", {key, id: g.dataset.guess});
    toastUndo("Moved into the project", async () => { await api("/api/undo", {batch: res.batch}); reload(); }); reload(); };
}
// Editing works like a plain checklist: tick, click to edit, Enter for a new step, Backspace on an
// empty step removes it, x removes with Undo, drag to reorder. Every change is an event.
// The rare actions, in a real menu attached to the page so no list clips it.

// ---- the Projects tab --------------------------------------------------------------------
async function drawProjects(){
  const main = $("main"); if(!main) return;
  // Drawn at once from the last copy; the fresh one redraws it only if anything changed.
  if(PJ.list) drawProjectList(PJ.list); else main.innerHTML = `<div class="empty">Opening&hellip;</div>`;
  let d; try{ d = await api("/api/projects"); }catch(e){ if(!PJ.list) main.innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
  const changed = JSON.stringify(d) !== JSON.stringify(PJ.list);
  PJ.list = d;
  if(changed) whenPointerFree(() => drawProjectList(d));
}
function drawProjectList(d){
  const main = $("main"); if(!main) return;
  if(TAB !== "projects" || view.page !== "list" || searchQ) return;
  let h = "";
  if(!d.ready){
    main.innerHTML = `<div class="empty">Your projects show here once Today's new storage is set up on this data.</div>`; return;
  }
  h += d.projects.map(p => `<div class="prow3" data-pid="${esc(p.id)}" title="Open it"><span class="what">${esc(p.title)}<span class="sub">${p.next && p.next_key ? `<a class="tasklink" href="#${encodeURIComponent(p.next_key)}" title="Open this task">${esc(p.next)}</a>` : esc(p.next || "Nothing next yet")}</span></span>${p.when ? `<span class="pwhen${p.when === "today" || p.when === "overdue" ? " hot" : ""}">${esc(p.when)}</span>` : ""}</div>`).join("");
  h += `<input class="add" id="newproj" placeholder="New project" title="Name it in your words. Today files tasks and messages that belong to it from then on.">`;
  if(d.suggestions.length){
    h += `<button class="fold${PJ.showSugg?" open":""}" id="psugg" title="Groupings Today noticed in your list. Make one a project, or wave it off.">Today suggests &middot; ${d.suggestions.length}</button>`;
    if(PJ.showSugg) h += d.suggestions.map(sg => { const open = PJ.openSugg === sg.id;
      return `<div class="prow3 sugg" data-sugg="${sg.id}" title="${open ? "" : "Click to see its tasks"}"><span class="what">${esc(sg.title)}<span class="sub">${open ? sg.tasks.map((t, n) => (sg.keys || [])[n] ? `<a class="sline tasklink" href="#${encodeURIComponent(sg.keys[n])}" title="Open this task">${esc(t)}</a>` : `<span class="sline">${esc(t)}</span>`).join("") : `${sg.tasks.length} tasks`}</span></span>`
        + `<button class="link keepsugg" data-keep="${sg.id}" title="Makes it one of your projects with these tasks in it. Rename it any time.">Make a project</button><button class="x" data-wave="${sg.id}" title="Not a project. Today won't suggest it again." aria-label="Not a project">&times;</button></div>`; }).join("");
  }
  if(d.completed.length){
    h += `<button class="fold${PJ.showDone?" open":""}" id="pdone">Completed &middot; ${d.completed.length}</button>`;
    if(PJ.showDone) h += d.completed.map(p => `<div class="prow3 donep" data-pid="${esc(p.id)}"><span class="what">${esc(p.title)}</span></div>`).join("");
  }
  main.innerHTML = h;
  main.querySelectorAll("[data-pid]").forEach(r => r.onclick = e => { if(e.target.closest("a")) return; go("#p:" + encodeURIComponent(r.dataset.pid)); });
  main.querySelectorAll("a.tasklink").forEach(a => a.onclick = e => e.stopPropagation());
  const np = $("newproj");
  np.onkeydown = async e => { if(e.key !== "Enter" || !np.value.trim()) return;
    try{ const res = await api("/api/projects/new", {title: np.value.trim()});
      toastUndo("Added to your projects", async () => { await api("/api/undo", {batch: res.batch}); drawProjects(); }); }
    catch(err){ toast(err.message); } drawProjects(); };
  if($("psugg")) $("psugg").onclick = () => { PJ.showSugg = !PJ.showSugg; drawProjectList(PJ.list); };
  main.querySelectorAll("[data-sugg]").forEach(r => r.onclick = e => { if(e.target.closest("button")) return; const id = +r.dataset.sugg; PJ.openSugg = PJ.openSugg === id ? null : id; drawProjectList(PJ.list); });
  if($("pdone")) $("pdone").onclick = () => { PJ.showDone = !PJ.showDone; drawProjectList(PJ.list); };
  main.querySelectorAll("[data-keep]").forEach(b => b.onclick = async () => { b.disabled = true;
    const res = await api("/api/projects/suggestion", {id: b.dataset.keep, keep: true});
    toastUndo("Now one of your projects", async () => { await api("/api/undo", {batch: res.batch}); drawProjects(); }); drawProjects(); });
  main.querySelectorAll("[data-wave]").forEach(b => b.onclick = async () => { b.disabled = true;
    const res = await api("/api/projects/suggestion", {id: b.dataset.wave, keep: false});
    toastUndo("Won't suggest that again", async () => { await api("/api/undo", {batch: res.batch}); drawProjects(); }); drawProjects(); });
}

// ---- one project ---------------------------------------------------------------------------
async function renderProjectPage(){
  const id = view.id;
  let d;
  try{
    if(!S) S = await api("/api/state");
    d = await api("/api/projects/page?id=" + encodeURIComponent(id));
  }catch(e){ toast(e.message); go(""); return; }
  if(view.page !== "proj" || view.id !== id) return;
  if(!PJ.data || PJ.data.id !== d.id){ PJ.changed = new Set(); PJ.about = null; PJ.box = ""; PJ.showRest = false; PJ.wordsOpen = false; PJ.editWords = false;
    PJ.view = await api("/api/assistant/view", {scope: {kind: "project", pid: d.id}}).catch(() => null); }
  PJ.data = d;
  drawProjectPage();
}
function reloadProj(){ return view.page === "proj" ? renderProjectPage() : loadList(); }
function projRow(r, pview){ return (r.kind === "todo" || r.manual) ? todoHtml(r, pview) : itemHtml(r, pview); }
function drawProjectPage(){
  const d = PJ.data, app = $("app"), pview = "proj:" + d.id;
  const first = (d.next || d.tasks)[0] || d.tasks[0];
  if(openIn[pview] === undefined) openIn[pview] = first ? first.key : null;
  const about = PJ.about && [...d.tasks, ...d.waiting, ...d.later].find(t => t.cid === PJ.about);
  let h = tabsHtml("") + `<span class="back" id="pback">&larr; Projects</span>`;
  h += `<div class="phead"><span class="ptitle edtitle" id="ptitle" contenteditable="true" spellcheck="true" title="Click to rename">${esc(d.title)}</span>`
    + (d.status === "open" ? `<button class="link" id="pcomplete" title="Marks the whole project done and clears its open tasks with it. Nothing is sent. 10 seconds to undo.">Complete</button>` : `<span class="muted">Complete</span>`) + `</div>`;
  if(PJ.editWords) h += `<textarea class="rdraft pwordsedit" id="pwordstext" rows="5" title="Your words on what this project is for. Saved exactly as you write them.">${esc(d.words)}</textarea><div class="actions"><button class="btn primary" id="pwordssave">Save</button><button class="link" id="pwordscancel">Cancel</button></div>`;
  else if(d.words) h += `<div class="pwords${PJ.wordsOpen ? "" : " clamp"}" id="pwords" title="${PJ.wordsOpen ? "" : "Your words. Click to read all or edit."}">${esc(d.words)}${PJ.wordsOpen ? ` <button class="link" id="pwordsedit">Edit</button>` : ""}</div>`;
  else h += `<div class="pwords"><button class="link" id="pwordsedit" title="Say in your words what this project is for.">What is this project for?</button></div>`;
  h += `<div class="think${about ? " about" : ""}" id="thinkbox">${about ? `<div class="refchip" title="What you write is about this task">${esc(about.title || about.words)}<button class="x" id="unabout" title="Make it about the whole project again" aria-label="Remove">&times;</button></div>` : ""}`
    + `<textarea id="think" rows="3" placeholder="${about ? "What should change about this task?" : "What's on your mind for this project?"}"${PJ.working ? " readonly" : ""}>${esc(PJ.box)}</textarea>`
    + `<div class="tbar"><button class="btn" id="pgo" title="Today does what you said: adds, rewrites or finishes tasks, changes a next move, brings in messages. Nothing is sent; you can undo."${PJ.working ? " disabled" : ""}>${PJ.working ? "Working on it…" : "Help me with this"}</button></div></div>`
    // What the user asked for, what Today did and its suggestions sit in their own box under the chat box,
    // so Help me with this stays right under where the user types (the user, 2026-11-15)
    + `<div class="passist" id="passist">${assistHtml(PJ.view, PJ.working ? "Working on it…" : "")}</div>`;
  if(d.notes.length) h += foldHtml(pview + ":notes", `What you've said &middot; ${d.notes.length}`,
    d.notes.slice().reverse().map(n => `<div class="saidnote"><span class="muted">${esc(fmtDay(n.at))}${n.task_title ? ` &middot; ${esc(n.task_title)}` : ""}</span><div>${esc(n.text)}</div></div>`).join(""));
  // Next and Then as the page data splits them (the user's own split once the user has made one), drawn as
  // the same rounded lists as the Today page; drag a row to reorder it or move it across.
  const shown = d.next || d.tasks.slice(0, 3), rest = d.then || d.tasks.slice(3);
  if(rest.some(t => t.key === openIn[pview])) PJ.showRest = true;
  h += `<h2 class="sec">Next</h2>` + (shown.length ? listHtml(shown, pview, projRow, " dnd").replace('<div class="list', '<div data-sec="next" class="list')
    : `<div class="list dnd dropempty" data-sec="next"><div class="empty">Nothing open. Add a task below, or tell Today above.</div></div>`);
  if(rest.length && PJ.showRest)
    h += `<h2 class="sec thenhead"><span>Then</span><button class="link" id="pless" title="Fold these away again">Show less</button></h2>` + listHtml(rest, pview, projRow, " dnd").replace('<div class="list', '<div data-sec="then" class="list');
  else if(rest.length) h += `<button class="fold" id="pthen" data-sec="then" title="Drop a task here to put it in Then">Then &middot; ${rest.length}</button>`;
  else if(shown.length > 1) h += `<h2 class="sec">Then</h2><div class="list dnd dropempty" data-sec="then"><div class="empty">Drag a task here to do it after Next.</div></div>`;
  h += `<input class="add" id="padd" placeholder="Add a task, or pick one from your list" value="${esc(PJ.addq)}" title="Type a new task, or start typing to find one already in Today."><div id="paddhits"></div>`;
  if(d.waiting.length) h += foldHtml(pview + ":waiting", `Waiting on others &middot; ${d.waiting.length}`, listHtml(d.waiting, pview, projRow));
  if(d.later.length) h += foldHtml(pview + ":later", `Later &middot; ${d.later.length}`, `<div class="list">` + d.later.map(r => `<div class="wrow"><span class="who2">${esc(whoOf(r))}</span><span class="words">${esc(r.title || r.words)}</span><button class="link" data-back="${esc(r.key)}" title="Back on the list. Nothing is sent.">Bring back</button></div>`).join("") + `</div>`);
  if(d.done.length) h += foldHtml(pview + ":done", `Done &middot; ${d.done.length}`, `<div class="list">` + d.done.map(r => `<div class="wrow done"><span class="words">${esc(r.title || r.words)}</span></div>`).join("") + `</div>`);
  const links = [];
  if(d.has_sources) links.push(`<a href="/projects/sources?id=${encodeURIComponent(d.id)}" title="Your Goal.md and the project agent's notes, as Today reads them">What Today has read for this project</a>`);
  if(links.length) h += `<div class="pfoot">${links.join("")}</div>`;
  h += dockHtml();  // the same bar as on the Today list; a task's chat icon attaches it here
  const y = scrollY; app.innerHTML = h; scrollTo(0, y);
  bindTabs(app);
  bindItems(app, reloadProj);
  bindDock(app);
  bindProjectPage(d, pview);
  // After Enter on a step (or the Add line), the caret goes where the user would type next.
  if(PJ.focusStep){ const t = app.querySelector(`[data-sedit="${CSS.escape(PJ.focusStep)}"]`); PJ.focusStep = null;
    if(t){ t.focus(); const r = document.createRange(); r.selectNodeContents(t); r.collapse(false); const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); } }
  if(PJ.focusAdd){ const a = app.querySelector(`[data-addstep="${CSS.escape(PJ.focusAdd)}"]`); PJ.focusAdd = null; if(a) a.focus(); }
}
function bindProjectPage(d, pview){
  $("pback").onclick = () => { TAB = "projects"; go(""); };
  // Keep the page's copy in step, so a redraw from it (opening a step, pointing the box) never
  // shows the old name again.
  editInPlace($("ptitle"), async t => { await api("/api/projects/rename", {id: d.id, title: t}); d.title = t; for(const p of (PJ.list ? [...PJ.list.projects, ...PJ.list.completed] : [])) if(p.id === d.id) p.title = t; });
  const comp = $("pcomplete");
  if(comp) comp.onclick = async () => { comp.disabled = true;
    try{ const res = await api("/api/projects/complete", {id: d.id}); TAB = "projects"; go("");
      toastUndo("Project complete. Its open tasks are cleared.", async () => { await api("/api/undo", {batch: res.batch}); if(view.page === "list") drawProjects(); else reloadProj(); }); }
    catch(err){ comp.disabled = false; toast(err.message); } };
  const words = $("pwords");
  if(words) words.onclick = e => { if(e.target.id === "pwordsedit") return; if(!PJ.wordsOpen){ PJ.wordsOpen = true; drawProjectPage(); } };
  const we = $("pwordsedit"); if(we) we.onclick = e => { e.stopPropagation(); PJ.editWords = true; drawProjectPage(); $("pwordstext").focus(); };
  if($("pwordscancel")) $("pwordscancel").onclick = () => { PJ.editWords = false; drawProjectPage(); };
  if($("pwordssave")) $("pwordssave").onclick = async () => { await api("/api/projects/words", {id: d.id, text: $("pwordstext").value}); PJ.editWords = false; toast("Saved"); reloadProj(); };
  const think = $("think");
  think.oninput = () => { PJ.box = think.value; };
  think.onkeydown = e => { if(e.key === "Enter" && (e.metaKey || e.ctrlKey)){ e.preventDefault(); helpMe(d, pview); } };
  $("pgo").onclick = () => helpMe(d, pview);
  bindAssist($("passist"), PJ.view, (say, label) => helpMe(d, pview, say, label), async () => {
    PJ.working = true; drawProjectPage();
    try{ PJ.view = await api("/api/assistant/back", {scope: {kind: "project", pid: d.id}}); }catch(err){ toast(err.message); }
    PJ.working = false; PJ.changed = new Set(); await renderProjectPage(); });
  if($("unabout")) $("unabout").onclick = () => { PJ.about = null; drawProjectPage(); };
  if($("pthen")) $("pthen").onclick = () => { PJ.showRest = true; drawProjectPage(); };
  if($("pless")) $("pless").onclick = () => { PJ.showRest = false; if((d.then || d.tasks.slice(3)).some(t => t.key === openIn[pview])) openIn[pview] = ((d.next || d.tasks)[0] || {}).key || null; drawProjectPage(); };
  const add = $("padd");
  let timer = null;
  add.oninput = () => { PJ.addq = add.value; clearTimeout(timer); timer = setTimeout(() => addHits(d), 200); };
  add.onkeydown = async e => { if(e.key === "Enter" && add.value.trim()){ e.preventDefault(); await addNew(d, add.value.trim()); } if(e.key === "Escape"){ PJ.addq = ""; add.value = ""; $("paddhits").innerHTML = ""; } };
  if(PJ.addq) addHits(d);
  dragSort($("app"), async ({key, sec, before}) => {
    const shown = d.next || d.tasks.slice(0, 3), rest = d.then || d.tasks.slice(3);
    const cid = new Map([...shown, ...rest].map(t => [t.key, t.cid]));
    const o = movedOrder({next: shown.map(t => t.key), then: rest.map(t => t.key)}, key, sec, before);
    try{
      const res = await api("/api/projects/order", {id: d.id, next: o.next.map(k => cid.get(k)), then: o.then.map(k => cid.get(k))});
      await reloadProj();
      if(res.batch) toastUndo(sec === "then" && !rest.some(t => t.key === key) ? "Moved to Then" : sec === "next" && rest.some(t => t.key === key) ? "Moved to Next" : "Moved",
        async () => { await api("/api/undo", {batch: res.batch}); reloadProj(); });
    }catch(err){ toast(err.message); reloadProj(); }
  });
  document.querySelectorAll("[data-back]").forEach(b => b.onclick = async () => { const res = await api("/api/projects/task", {id: d.id, key: b.dataset.back, action: "back"});
    toastUndo(res.message, async () => { await api("/api/undo", {batch: res.batch}); reloadProj(); }); reloadProj(); });
}
async function addHits(d){
  const box = $("paddhits"), q = PJ.addq.trim(); if(!box) return;
  if(!q){ box.innerHTML = ""; return; }
  const inside = new Set([...d.tasks, ...d.waiting, ...d.later].map(t => t.key));
  let hits = [];
  if(q.length >= 2){ try{ const r = await api("/api/search?q=" + encodeURIComponent(q)); hits = r.items.filter(i => i.status === "open" && !inside.has(i.key)).slice(0, 4); }catch(e){ hits = []; } }
  if(PJ.addq.trim() !== q) return;
  box.innerHTML = `<div class="addsugg">${hits.map(i => `<div data-pick="${esc(i.key)}" title="Moves it into this project">${i.who ? `<b>${esc(i.who)}</b> ` : ""}${esc(i.title)}<span class="muted">in Today</span></div>`).join("")}<div data-newtask="1">Add &ldquo;${esc(q)}&rdquo; as a new task</div></div>`;
  box.querySelectorAll("[data-pick]").forEach(el => el.onclick = async () => {
    const res = await api("/api/projects/add", {id: d.id, key: el.dataset.pick}); PJ.addq = "";
    PJ.changed = new Set([el.dataset.pick]);
    toastUndo("Moved into this project", async () => { await api("/api/undo", {batch: res.batch}); reloadProj(); }); reloadProj(); });
  box.querySelector("[data-newtask]").onclick = () => addNew(d, q);
}
async function addNew(d, title){
  try{ const res = await api("/api/projects/add", {id: d.id, title}); PJ.addq = "";
    toastUndo("Added", async () => { await api("/api/undo", {batch: res.batch}); reloadProj(); }); }
  catch(err){ toast(err.message); }
  reloadProj();
}
async function helpMe(d, pview, say, label){
  const text = (say ?? PJ.box).trim();
  // With a task pointed at, an empty box asks Today to sort that task out from what it has.
  if(!text && !PJ.about){ toast("Say what's on your mind first"); $("think").focus(); return; }
  if(PJ.working) return;
  PJ.working = true; drawProjectPage();
  let res;
  try{ res = await api("/api/assistant", {scope: {kind: "project", pid: d.id, task: say === undefined ? (PJ.about || "") : ""}, say: text, label: label || ""}); }
  catch(err){ PJ.working = false; drawProjectPage(); toast(err.message); return; }
  PJ.working = false; if(say === undefined){ PJ.box = ""; PJ.about = null; }
  PJ.view = {turns: res.turns, next: res.next};
  PJ.changed = new Set(res.changed || []);
  const first = (res.changed || [])[0];
  if(first) openIn[pview] = first;
  await renderProjectPage();
  toast(res.summary);
}

// ---- the feedback bar, docked under the Today list -----------------------------------------
function dockHtml(){
  if(!((TAB === "today" && view.page === "list") || view.page === "proj") || searchQ){ document.body.classList.remove("docked"); return ""; }
  document.body.classList.add("docked");
  if(DOCK.shut && !DOCK.refs.length) return `<button class="dockpill" id="dockopen" title="Tell Today something" aria-label="Tell Today something">${ASK_SVG}</button>`;
  const live = DOCK.refs.length || DOCK.text.trim();
  const chips = DOCK.refs.map(k => { const r = rowsByKey[k] || {}; const t = String(r.title || r.words || "").replace(/\s+/g, " ");
    return `<span class="refchip">${esc(whoOf(r) ? whoOf(r) + ": " : "")}${esc(t.slice(0, 38))}${t.length > 38 ? "…" : ""}<button class="x" data-unref="${esc(k)}" aria-label="Remove">&times;</button></span>`; }).join("");
  const said = DOCK.view || DOCK.working ? `<div class="dockassist" id="dockassist">${DOCK.view && !DOCK.working ? `<button class="x" id="dockdone" title="Put this away" aria-label="Put this away">&times;</button>` : ""}${assistHtml(DOCK.view, DOCK.working ? "Working on it…" : "")}</div>` : "";
  return `<div class="dock"><div class="dockwrap">${said}<div class="dockin${live ? " live" : ""}">${chips}<div class="dockrow">`
    + `<textarea id="fb" rows="1" placeholder="${dockPlaceholder()}"${DOCK.working ? " readonly" : ""}>${esc(DOCK.text)}</textarea>`
    + `<button class="btn${live ? " primary" : ""}" id="fbgo" title="Today makes the change and remembers it for next time. Nothing is sent; you can undo."${DOCK.working ? " disabled" : ""}>${DOCK.working ? "Working…" : "Go"}</button>`
    // The tuck-away chevron sits outside the bar, to the right of Go and centred on its row.
    + `<button class="iconbtn dockshut" id="dockshut" title="Tuck the bar away" aria-label="Tuck the bar away"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></button></div></div>`
    + `</div></div>`;
}
function bindDock(app){
  if($("dockopen")) $("dockopen").onclick = () => { DOCK.shut = false; localStorage.setItem("todayDockShut", "0"); dockRedraw(); $("fb").focus(); };
  if($("dockshut")) $("dockshut").onclick = () => { DOCK.shut = true; DOCK.refs = []; localStorage.setItem("todayDockShut", "1"); dockRedraw(); };
  if($("dockdone")) $("dockdone").onclick = () => { DOCK.view = null; dockRedraw(); };
  bindAssist($("dockassist"), DOCK.view, (say, label) => dockGo(say, label), async () => {
    DOCK.working = true; dockRedraw();
    try{ DOCK.view = await api("/api/assistant/back", {scope: {kind: "tasks", keys: DOCK.keys}}); }catch(err){ toast(err.message); }
    DOCK.working = false; await loadList(); dockRedraw(); });
  app.querySelectorAll("[data-unref]").forEach(b => b.onclick = () => { DOCK.refs = DOCK.refs.filter(k => k !== b.dataset.unref); dockRedraw(); $("fb").focus(); });
  const fb = $("fb");
  if(fb){
    fb.oninput = () => { DOCK.text = fb.value; fb.style.height = "auto"; fb.style.height = Math.min(160, fb.scrollHeight) + "px";
      const go = $("fbgo"); if(go) go.classList.toggle("primary", !!(DOCK.refs.length || DOCK.text.trim())); };
    fb.onkeydown = e => { if(e.key === "Enter" && !e.shiftKey){ e.preventDefault(); dockGo(); } };
    $("fbgo").onclick = () => dockGo();
  }
  app.querySelectorAll("[data-unlearn]").forEach(b => b.onclick = async () => { const id = +b.dataset.unlearn;
    await api("/api/learned/remove", {id}); toastUndo("Today won't use that any more", async () => { await api("/api/learned/remove", {id, removed: false}); loadList(); }); loadList(); });
}
// With one task attached the bar talks to that card's assistant, so it says what it can do.
function dockPlaceholder(){
  if(!DOCK.refs.length) return "Tell Today something";
  if(DOCK.refs.length > 1) return "What's wrong with these, or what should change?";
  const d = CARD.data[DOCK.refs[0]];
  return d && (d.draft || d.nudge) ? "What should change in the reply? Or say what to do" : "What should happen with this?";
}
const dockRedraw = () => view.page === "proj" ? drawProjectPage() : renderList();
function dockRef(key, add){
  DOCK.shut = false;
  if(add){ if(!DOCK.refs.includes(key)) DOCK.refs.push(key); }
  else DOCK.refs = DOCK.refs.includes(key) && DOCK.refs.length === 1 ? [] : [key];
  dockRedraw();
  const fb = $("fb"); if(fb) fb.focus({preventScroll: true});
}
async function dockGo(say, label){
  const text = (say ?? DOCK.text).trim();
  if(!text){ toast(DOCK.refs.length ? "Say what's wrong first" : "Say something first"); return; }
  if(DOCK.working) return;
  // One task attached: the card's own assistant does it, and the card shows what came of it.
  if(say === undefined && DOCK.refs.length === 1){
    const key = DOCK.refs[0], d = CARD.data[key];
    const reload = view.page === "proj" ? reloadProj : loadList;
    const cur = d && (d.steps || []).find(s => !s.done);
    const opened = d ? [...openedSteps(key)].filter(c => d.steps.some(s => s.cid === c)) : [];
    const focus = opened.length ? opened[opened.length - 1] : (cur ? cur.cid : "");
    DOCK.text = ""; DOCK.refs = []; dockRedraw();
    const el = document.querySelector(`.item[data-item="${CSS.escape(key)}"]`);
    if(el && !el.classList.contains("open")) el.querySelector(".head").click();
    await cardAct(document.querySelector(`.item[data-item="${CSS.escape(key)}"]`), reload, key, text, text, focus);
    return;
  }
  // Several tasks attached, or none: the same assistant, about them (or the user's whole list).
  if(say === undefined) DOCK.keys = DOCK.refs.slice();
  DOCK.working = true; dockRedraw();
  let res;
  try{ res = await api("/api/assistant", {scope: {kind: "tasks", keys: DOCK.keys}, say: text, label: label || ""}); }
  catch(err){ DOCK.working = false; dockRedraw(); toast(err.message); return; }
  DOCK.working = false; if(say === undefined){ DOCK.text = ""; DOCK.refs = []; }
  DOCK.view = {turns: res.turns, next: res.next};
  await loadList();
  dockRedraw();
  toast(res.summary);
}
function learnedHtml(){
  const rows = (S && S.learned) || [];
  if(!rows.length) return "";
  return `<div class="bottom learned">` + foldHtml("learned", `What Today has learned &middot; ${rows.length}`,
    rows.map(l => `<div class="wrow"><span class="words">&ldquo;${esc(l.text)}&rdquo;${l.items.length ? `<span class="muted"> &middot; ${esc(l.items.map(i => i.who || i.title).filter(Boolean).slice(0, 3).join(", "))}</span>` : ""}</span><button class="x" data-unlearn="${l.id}" title="Stop using this. You can undo." aria-label="Remove">&times;</button></div>`).join(""),
    "Your corrections from the bar below. Today reads new messages with them in mind.") + `</div>`;
}

const PJ_CSS = document.createElement("style");
PJ_CSS.textContent = `
.projname{color:#a39d92;white-space:nowrap;flex-shrink:0;border:0;background:none;font:inherit;padding:0;cursor:pointer}.projname:hover{color:#1c1b19;text-decoration:underline}
.item .head .askicon{border:0;background:none;cursor:pointer;flex-shrink:0}
.askicon.in{border:0;background:none;color:#a39d92;cursor:pointer;padding:2px;display:inline-flex;margin:0 0 0 auto;align-self:center}
.item.changed{border-left:3px solid #2f6b35}
.item.reffed{background:#f6f2fb}
.check2.ticked{background:#2f6b35;border-color:#2f6b35;transition:background .08s,border-color .08s}
.soft{color:#6b665d}
.cl{list-style:none;margin:0 0 10px 0}
.cl li{display:grid;grid-template-columns:14px 16px 1fr 44px;gap:0 10px;align-items:center;padding:3px 0;margin-left:-24px}
.cl .sbtns{display:flex;justify-content:flex-end;gap:2px}
.cl .chev{border:0;background:none;color:#a39d92;cursor:pointer;padding:0 4px;font:inherit;opacity:0}
.cl li:hover .chev,.cl li.sopen .chev{opacity:1}.cl .chev:hover{color:#1c1b19}
.cl li .st-t:not([contenteditable]){cursor:pointer}
.cl li.sopen .st-t{color:#1c1b19}
.cl li.done.sopen .st-t{color:#6b665d}
.cl li .grip{color:#c9c3b6;cursor:grab;opacity:0;font-size:11px;letter-spacing:-2px;user-select:none}
.cl li:hover .grip{opacity:1}
.cl li .sx{opacity:0}.cl li:hover .sx{opacity:1}
@media (pointer:coarse){.cl .chev,.cl li .sx{opacity:1}}
.cl .sq{width:16px;height:16px;border:1.5px solid #c9c3b6;border-radius:4px;background:#fff;cursor:pointer;padding:0;position:relative}
.cl .sq:hover{border-color:#2f6b35}
.cl .sq.on{background:#2f6b35;border-color:#2f6b35}
.cl .sq.on::after{content:"";position:absolute;left:4px;top:1px;width:4px;height:8px;border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg)}
.cl .sq.ghost{border-style:dashed;cursor:default}
.cl .st-t{outline:none;border-radius:4px;padding:1px 4px;margin-left:-4px;color:#6b665d;min-height:1.4em}
.cl .st-t:focus{background:#f6f3ec;color:#1c1b19}
.cl li.cur .st-t{color:#1c1b19;font-weight:600}
.cl li.done .st-t{color:#a39d92;text-decoration:line-through}
.cl .st-do{grid-column:3 / 5;margin:2px 0 8px}
.cl .st-note{color:#6b665d}
.cl .st-do .actions{margin-top:8px}
.cl li.addstep input{font:inherit;border:0;outline:none;background:transparent;color:#1c1b19;padding:1px 0}
.cl li.addstep input::placeholder{color:#a39d92}
.cl li.dragging{opacity:.4}.cl li.over{box-shadow:0 -2px 0 #9b84c4}
.calc{width:100%;border-collapse:collapse;margin-top:6px}
.calc td,.calc th{text-align:left;padding:5px 8px;border-bottom:1px solid #f0ede6;font-weight:400}
.calc th{color:#6b665d}.calc td.pct{color:#2f6b35}
.calc input{font:inherit;width:100%;border:0;background:transparent;outline:none}
.sdraft{margin-top:8px}
.prog{display:inline-flex;align-items:center;gap:6px;color:#6b665d;white-space:nowrap;flex-shrink:0;align-self:center}
.item.open .head .prog{display:none}
.guess{color:#6b665d;margin:0 0 10px}.guess .link{text-decoration:underline}
.prow3{display:flex;align-items:center;gap:14px;background:#fff;border:1px solid #e7e3da;border-radius:10px;padding:12px 16px;margin-bottom:6px;cursor:pointer}
.prow3:hover{border-color:#cfc9bc}.prow3 .what{flex:1;min-width:0}.prow3 .sub{display:block;color:#6b665d}
.prow3 .pwhen{color:#6b665d;white-space:nowrap;align-self:flex-start}.prow3 .pwhen.hot{color:#b4541f}.prow3.sugg .sline{display:block}a.tasklink{color:inherit;text-decoration:none}a.tasklink:hover{color:#1c1b19;text-decoration:underline}.keepsugg{white-space:nowrap;opacity:.8}.prow3.sugg:hover .keepsugg{opacity:1;color:#1c1b19}.prow3.donep{color:#6b665d}
.x{color:#a39d92;cursor:pointer;border:0;background:none;font:inherit;font-size:18px;line-height:1;padding:0 4px}.x:hover{color:#1c1b19}
.phead{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.ptitle{font-weight:600;border-radius:4px;padding:1px 4px;margin-left:-4px;cursor:text;outline:none}
.ptitle:hover{background:#ece8df}.ptitle.edtitle:focus{background:#fff;box-shadow:0 0 0 1px #d9d4c8}
.pwords{margin-top:14px;color:#1c1b19;white-space:pre-line}
.pwords.clamp{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;cursor:pointer}
.pwordsedit{margin-top:12px}
.think{margin-top:22px;background:#fff;border:1px solid #e7e3da;border-radius:12px;padding:14px 16px}
.think.about{border-color:#9b84c4}
.think textarea{width:100%;font:inherit;border:0;outline:none;resize:none;min-height:72px;background:transparent;color:#1c1b19}
.passist{margin-top:10px;background:#fff;border:1px solid #e7e3da;border-radius:12px;padding:12px 16px}
.passist:empty{display:none}
.dockassist{position:relative;background:#fff;border:1px solid #e7e3da;border-radius:12px;padding:10px 34px 4px 14px;margin:0 0 8px;max-height:40vh;overflow:auto}
.dockassist .x{position:absolute;top:6px;right:8px}
.think .tbar{display:flex;justify-content:flex-end;gap:8px;margin-top:6px;align-items:center}
.refchip{display:inline-flex;gap:8px;align-items:center;background:#f1edf7;color:#4b3d6b;border-radius:6px;padding:2px 4px 2px 10px;margin:0 6px 8px 0;max-width:100%}
.refchip .x{font-size:15px;color:#7b6a9b}
.saidnote{color:#55514a;border-left:3px solid #e7e3da;padding-left:10px;margin:8px 0;white-space:pre-line}
.thenhead{display:flex;justify-content:space-between;align-items:baseline}.thenhead .link{font-weight:400}
.addsugg{background:#fff;border:1px solid #e7e3da;border-radius:10px;margin:-2px 0 6px;overflow:hidden}
.addsugg div{padding:9px 16px;cursor:pointer;display:flex;gap:8px}.addsugg div:hover{background:#f6f3ec}.addsugg .muted{margin-left:auto}
.wrow{display:flex;gap:12px;align-items:baseline;background:#fff;border:1px solid #e7e3da;border-radius:10px;padding:10px 16px;margin-bottom:6px}
.wrow .who2{font-weight:600;width:96px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wrow .who2:empty{display:none}.wrow .words{flex:1;min-width:0}
.wrow.done .words{color:#6b665d;text-decoration:line-through}
/* Inside a rounded list the rows share its edge, as on the Today page. */
.list .wrow{border:0;border-top:1px solid #f0ede6;border-radius:0;margin:0}.list .wrow:first-child{border-top:0}
/* Drag to reorder (project pages and Now): the row being moved, and where it would land. */
.list.dnd .item .head{touch-action:pan-y}
.item.lifted{opacity:.45}
.dropline{height:0;border-top:2px solid #1c1b19;margin:-1px 14px}
.list.dropempty .empty{padding:14px 16px;color:#6b665d}
.dropover{outline:2px dashed #b0aa9f;outline-offset:2px}
body.dragging-row,body.dragging-row *{cursor:grabbing!important;user-select:none!important}
.nudgebox{background:#fff;border:1px solid #e7e3da;border-radius:10px;padding:12px 16px;margin:-2px 0 8px}
.pfoot{margin-top:28px;display:flex;gap:18px}.pfoot a{color:#6b665d;text-decoration:none}.pfoot a:hover{color:#1c1b19}
.iconbtn{border:0;background:none;color:#6b665d;cursor:pointer;display:inline-flex;padding:6px;border-radius:6px;align-items:center}
.iconbtn:hover{color:#1c1b19;background:#ece8df}
.tabs .iconbtn.tabsearch{margin-left:auto}
.learned{margin-top:0;border-top:0}
body.docked{padding-bottom:140px}
body.docked .toast{bottom:96px}
.dock{position:fixed;left:0;right:0;bottom:0;padding:12px 24px 18px;background:linear-gradient(to top,#f6f4ef 70%,rgba(246,244,239,0));z-index:20}
.dockwrap{max-width:680px;margin:0 auto;position:relative}
.dockin{background:#fff;border:1px solid #e7e3da;border-radius:14px;padding:8px 8px 8px 14px;box-shadow:0 4px 18px rgba(0,0,0,.05)}
.dockin.live{border-color:#9b84c4}
.dockin .refchip{margin:2px 6px 6px 0}
.dockrow{display:flex;align-items:center;gap:6px;position:relative}
.dockrow textarea{flex:1;font:inherit;border:0;outline:none;resize:none;background:transparent;padding:4px 0;color:#1c1b19}
.dockshut{position:absolute;left:calc(100% + 15px);top:50%;transform:translateY(-50%)}
.dockpill{position:fixed;right:28px;bottom:24px;width:44px;height:44px;border-radius:50%;border:1px solid #e7e3da;background:#fff;color:#6b665d;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 14px rgba(0,0,0,.08);z-index:20}
.dockpill:hover{color:#1c1b19}
@media (max-width:760px){.dock{padding:10px 52px 14px 12px}.dockshut{margin-left:4px}}
`;
document.head.appendChild(PJ_CSS);
