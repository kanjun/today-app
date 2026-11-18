// A task's date, in one place (the user, 2026-11-15): the date spot at the top right of its row says
// the date ("Nov 29"), "today" or "overdue"; a task with no date shows nothing until its card is
// open, then a small calendar. Clicking it opens the one picker: Today, Tomorrow, a box to type
// in (the green line under it shows the date it understood), a calendar, and Clear.
// Loaded before the page's main script; uses its helpers (api, esc, toastUndo, whenHtml,
// LIST_VIEWS, isProjView) only when called.

const DUE_CAL_SVG = '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" aria-hidden="true"><rect x="2" y="3" width="12" height="11" rx="1.5"/><path d="M2 6.5h12M5.5 1.5v3M10.5 1.5v3"/></svg>';
// The user's home zone (the timezone setting), put on the page by the server.
const DUE_TZ = document.documentElement.dataset.tz || Intl.DateTimeFormat().resolvedOptions().timeZone;
const DUE = { pop: null, seq: 0, timer: null };

// The date spot on a row. Rows that aren't a task (a goal's move, a placeholder) keep plain words.
function dueHtml(r, viewName){
  const settable = (LIST_VIEWS.includes(viewName) || isProjView(viewName)) && !r.move && !r.placeholder;
  if(!settable) return r.when ? whenHtml(r.when) : "";
  if(r.when) return `<button class="when duebtn${r.when==="today"?" hot":""}" data-due="${esc(r.key)}" title="Change the date">${esc(r.when)}</button>`;
  return `<button class="duebtn dueicon" data-due="${esc(r.key)}" title="Set a date" aria-label="Set a date">${DUE_CAL_SVG}</button>`;
}

const dueIso = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
const dueDate = iso => { const [y, m, d] = String(iso).split("-").map(Number); return new Date(y, m-1, d); };
// Today in the home zone, whatever the device's clock says.
const dueToday = () => new Date().toLocaleDateString("en-CA", {timeZone: DUE_TZ});
const dueLabel = iso => { const d = dueDate(iso); return d.toLocaleDateString("en-US", {weekday:"short", month:"short", day:"numeric", ...(d.getFullYear()!==dueDate(dueToday()).getFullYear()?{year:"numeric"}:{})}).replace(/,/g, ""); };

function closeDuePicker(){
  if(DUE.pop){ DUE.pop.remove(); DUE.pop = null; }
  clearTimeout(DUE.timer); DUE.seq++;
  document.removeEventListener("mousedown", dueOutside, true);
}
function dueOutside(e){ if(DUE.pop && !DUE.pop.contains(e.target) && !e.target.closest("[data-due]")) closeDuePicker(); }

function openDuePicker(btn, r, reload){
  const again = DUE.pop && DUE.pop.dataset.key === r.key;
  closeDuePicker();
  if(again) return;  // a second click on the date closes it
  const today = dueToday(), current = r.due || "";
  const st = { month: dueDate(current || today), cand: "" };
  st.month.setDate(1);
  const pop = document.createElement("div");
  pop.className = "duepop"; pop.dataset.key = r.key;
  pop.innerHTML = `<div class="duepicks"><button data-pick="${today}">Today</button><button data-pick="${dueIso(new Date(dueDate(today).getTime()+86400000))}">Tomorrow</button></div>`
    + `<input class="duetype" placeholder="type a date: fri, dec 15, in 2 weeks" aria-label="Type a date">`
    + `<div class="duehint"></div><div class="duecal"></div>`
    + `<div class="duefoot">${current?`<button data-clear title="Take the date off. Nothing is sent.">Clear</button>`:""}</div>`;
  document.body.appendChild(pop); DUE.pop = pop;
  const at = btn.getBoundingClientRect(), w = pop.offsetWidth;
  pop.style.left = Math.max(8, Math.min(at.right + scrollX - w, scrollX + document.documentElement.clientWidth - w - 8)) + "px";
  pop.style.top = (at.bottom + scrollY + 6) + "px";
  const input = pop.querySelector(".duetype"), hint = pop.querySelector(".duehint");

  const drawCal = () => {
    const m = st.month, first = (new Date(m.getFullYear(), m.getMonth(), 1)).getDay();
    const n = new Date(m.getFullYear(), m.getMonth()+1, 0).getDate();
    let h = `<div class="duecalh"><button data-mon="-1" aria-label="Previous month">&lsaquo;</button>${m.toLocaleDateString("en-US", {month:"long", year:"numeric"})}<button data-mon="1" aria-label="Next month">&rsaquo;</button></div><div class="duegrid">`;
    h += "SMTWTFS".split("").map(d => `<b>${d}</b>`).join("") + "<span></span>".repeat(first);
    for(let d = 1; d <= n; d++){
      const iso = dueIso(new Date(m.getFullYear(), m.getMonth(), d));
      h += `<button data-pick="${iso}" class="${iso===today?"today":""}${iso===current?" on":""}${iso===st.cand?" cand":""}">${d}</button>`;
    }
    pop.querySelector(".duecal").innerHTML = h + "</div>";
  };
  const showCand = iso => {
    st.cand = iso; hint.textContent = iso ? dueLabel(iso) : "";
    if(iso){ const d = dueDate(iso); st.month = new Date(d.getFullYear(), d.getMonth(), 1); }
    drawCal();
  };
  // Plain words are read at once; only words that can't be read go to the small model.
  const understand = async (text, model) => {
    const seq = ++DUE.seq;
    const res = await fetch("/api/due/parse", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({text, model})}).then(x => x.json()).catch(() => ({}));
    if(seq !== DUE.seq || !DUE.pop) return;
    if(res.date || model || text.trim().length < 3){ showCand(res.date || ""); if(!res.date && model) hint.textContent = text.trim() ? "Not sure what date that is" : ""; return; }
    hint.textContent = "…";
    DUE.timer = setTimeout(() => understand(text, true), 500);
  };
  input.oninput = () => { clearTimeout(DUE.timer); const t = input.value; DUE.timer = setTimeout(() => understand(t, false), 150); };
  input.onkeydown = e => {
    if(e.key === "Enter" && st.cand){ e.preventDefault(); saveDue(r, st.cand, reload); }
    if(e.key === "Escape") closeDuePicker();
  };
  pop.onclick = e => {
    e.stopPropagation();
    const b = e.target.closest("button"); if(!b) return;
    if(b.dataset.mon){ st.month = new Date(st.month.getFullYear(), st.month.getMonth() + Number(b.dataset.mon), 1); drawCal(); return; }
    if(b.dataset.pick) saveDue(r, b.dataset.pick, reload);
    if(b.hasAttribute("data-clear")) saveDue(r, "", reload);
  };
  pop.onkeydown = e => { if(e.key === "Escape") closeDuePicker(); };
  drawCal();
  setTimeout(() => document.addEventListener("mousedown", dueOutside, true));
  input.focus();
}

async function saveDue(r, iso, reload){
  closeDuePicker();
  if(iso === (r.due || "")) return;
  try{
    const res = await api("/api/card/due", {key: r.key, due: iso});
    toastUndo(iso ? `Date set to ${dueLabel(iso)}` : "Date cleared", async () => { await api("/api/undo", {batch: res.batch}); reload(); });
  } catch(err){ toast(err.message); }
  reload();
}

const DUE_CSS = document.createElement("style");
DUE_CSS.textContent = `
.duebtn{border:0;background:none;font:inherit;font-size:15px;color:#6b665d;cursor:pointer;padding:0}
.duebtn.hot{color:#b4541f}
.duebtn:hover{color:#1c1b19}
.item .head .dueicon{border:0;background:none;cursor:pointer}
.duepop{position:absolute;z-index:60;background:#fff;border:1px solid #d9d4c8;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.08);padding:12px;width:290px;font-size:15px;color:#1c1b19}
.duepop button{font:inherit;font-size:15px;cursor:pointer}
.duepicks{display:flex;gap:6px;margin-bottom:9px}
.duepicks button{border:1px solid #e7e3da;border-radius:7px;padding:3px 10px;background:#fff;color:#1c1b19}
.duepicks button:hover{border-color:#a9a397}
.duetype{font:inherit;font-size:15px;border:1px solid #d9d4c8;border-radius:7px;padding:3px 8px;width:100%;box-sizing:border-box}
.duetype:focus{outline:none;border-color:#1c1b19}
.duehint{color:#2f6b35;margin:5px 2px 0;min-height:22px}
.duecal{margin-top:6px;border-top:1px solid #eeebe4;padding-top:8px}
.duecalh{display:flex;justify-content:space-between;align-items:center;color:#1c1b19;margin-bottom:4px}
.duecalh button{border:0;background:none;color:#6b665d;padding:0 6px}
.duecalh button:hover{color:#1c1b19}
.duegrid{display:grid;grid-template-columns:repeat(7,1fr);text-align:center;row-gap:2px}
.duegrid b{font-weight:400;color:#a39d92}
.duegrid button{border:0;background:none;border-radius:6px;padding:1px 0;color:#1c1b19}
.duegrid button:hover{background:#f1eee7}
.duegrid button.today{text-decoration:underline}
.duegrid button.cand{outline:1.5px solid #2f6b35;color:#2f6b35}
.duegrid button.on{background:#1c1b19;color:#fff}
.duefoot{display:flex;justify-content:flex-end;margin-top:8px;min-height:4px}
.duefoot button{border:0;background:none;color:#6b665d;text-decoration:underline;padding:0}
.duefoot button:hover{color:#1c1b19}
`;
document.head.appendChild(DUE_CSS);
