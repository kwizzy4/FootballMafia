/* ⚽🕶️ Футбольная мафия — клиент */
"use strict";

let ws = null;
let room = null;          // публичное состояние комнаты
let you = null;           // приватное состояние меня
let mafiaChat = [];       // сообщения мафии
let tab = "create";
let chatChannel = "public";
let timeOffset = 0;       // serverTime - localTime
let phaseTotal = 60;
let lastState = "";
let seenMsgIds = new Set();

const $ = (id) => document.getElementById(id);

/* ---------------- вход ---------------- */

function setTab(t) {
  tab = t;
  $("tabCreate").classList.toggle("on", t === "create");
  $("tabJoin").classList.toggle("on", t === "join");
  $("codeField").style.display = t === "join" ? "" : "none";
  $("goBtn").textContent = t === "create" ? "🏟️ Создать комнату" : "🚪 Войти в игру";
  $("loginErr").textContent = "";
}

function connect() {
  const nick = $("nick").value.trim();
  if (!nick) { $("loginErr").textContent = "Введите ник — друзья должны знать, кто их подставляет."; return; }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    const payload = tab === "create"
      ? { op: "create", name: nick }
      : { op: "join", name: nick, code: ($("code").value || "").trim().toUpperCase() };
    if (tab === "join" && !payload.code) { $("loginErr").textContent = "Укажите код комнаты."; try { ws.close(); } catch (e) {} return; }
    ws.send(JSON.stringify(payload));
    $("goBtn").disabled = true;
    $("goBtn").textContent = "⏳ Подключение…";
  };
  ws.onmessage = (ev) => handle(JSON.parse(ev.data));
  ws.onclose = () => {
    if (room && room.state !== "gameover") toast("Соединение потеряно 😢 Обновите страницу.");
    $("goBtn").disabled = false; $("goBtn").textContent = tab === "create" ? "🏟️ Создать комнату" : "🚪 Войти в игру";
  };
  ws.onerror = () => { $("loginErr").textContent = "Не удалось подключиться к серверу."; };
}

function handle(m) {
  if (m.type === "fatal") {
    $("loginErr").textContent = m.message;
    $("goBtn").disabled = false; $("goBtn").textContent = tab === "create" ? "🏟️ Создать комнату" : "🚪 Войти в игру";
    return;
  }
  if (m.type === "sync") {
    const stateChanged = !room || room.state !== m.room.state || room.round !== m.room.round;
    room = m.room; you = m.you;
    if (m.mafiaChat) mafiaChat = m.mafiaChat;
    timeOffset = room.serverTime - Date.now() / 1000;
    $("landing").style.display = "none";
    $("game").style.display = "block";
    $("roomTag").style.display = "";
    $("roomCodeLbl").textContent = room.code;
    if (stateChanged) onPhaseChange();
    renderAll();
    return;
  }
  if (m.type === "chat") { addChatBubble(m.msg); return; }
  if (m.type === "mafia_msg") { mafiaChat.push(m.msg); if (chatChannel === "mafia") addChatBubble(m.msg); return; }
}

function onPhaseChange() {
  document.body.className = room.state === "night" ? "night-mode" : "day-mode";
  if (room.state === "night") buzz("🌙 Ночь наступила");
  if (room.state === "day") buzz("☀️ Рассвет — обсуждайте!");
  if (room.state === "voting") buzz("🗳️ Голосование началось");
  if (room.state === "gameover") buzz(room.winner === "citizens" ? "🏆 Город победил!" : "🕶️ Мафия победила!");
  rebuildChatLog();
}

function buzz(text) {
  if (!("Notification" in window)) return;
  if (Notification.permission === "granted") { try { new Notification("⚽🕶️ Футбольная мафия", { body: text }); } catch (e) {} }
  else if (Notification.permission !== "denied") Notification.requestPermission();
}

/* ---------------- рендер ---------------- */

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function fmtBold(s) { return esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>"); }
function now() { return Date.now() / 1000 + timeOffset; }

function renderAll() { renderPhase(); renderHost(); renderMyCard(); renderPlayers(); renderLog(); renderChanTabs(); renderFinale(); }

function renderPhase() {
  const bar = $("phaseBar");
  const S = {
    lobby:   ["🏟️", "Лобби", "Ждём игроков. Хост запускает игру, когда соберётся минимум 4."],
    dealing: ["📰", "Раздача ролей из Википедии…", "Скачиваем досье легенд футбола"],
    night:   ["🌙", `Ночь · раунд ${room.round}`, you?.isMafia ? "Мафия выбирает жертву кнопкой 🔪" : "Город спит… ждём утро"],
    day:     ["☀️", `День · раунд ${room.round}`, "Дебаты в чате: кто ваш футболист и кто врёт?"],
    voting:  ["🗳️", "Голосование", "Выберите подозреваемого кнопкой 🗳"],
    reveal:  ["🎭", "Раскрытие", "Смотрим, кого изгнали…"],
    gameover:[room.winner === "citizens" ? "🏆" : "🕶️",
              room.winner === "citizens" ? "Горожане победили!" : "Мафия захватила город!", "Итоги партии ниже"],
  }[room.state] || ["❓", room.state, ""];
  let timerHtml = "";
  if (room.timerEndsAt > 0) {
    const left = Math.max(0, Math.ceil(room.timerEndsAt - now()));
    const totals = { night: 40, day: 150, voting: 40, reveal: 10 };
    phaseTotal = totals[room.state] || 60;
    const pct = Math.min(100, (left / phaseTotal) * 100);
    timerHtml = `<div class="timer"><b>${fmtTime(left)}</b><small>осталось</small></div>`;
    bar.className = "phase-bar " + (room.state === "night" ? "night" : room.state === "day" ? "day" : "");
    bar.innerHTML = `<div class="phase-icon">${S[0]}</div><div class="phase-info"><b>${esc(S[1])}</b><span>${esc(S[2])}</span>
      <div class="prog"><i style="width:${pct}%"></i></div></div>${timerHtml}`;
  } else {
    bar.className = "phase-bar";
    bar.innerHTML = `<div class="phase-icon">${S[0]}</div><div class="phase-info"><b>${esc(S[1])}</b><span>${esc(S[2])}</span></div>`;
  }
  $("roundLbl").textContent = room.round ? `Раунд ${room.round}` : "";
}

function fmtTime(s) { return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }

setInterval(() => { if (room) renderPhase(); }, 1000);

function renderHost() {
  const bar = $("hostBar");
  if (!you || !room) return;
  const isHost = room.hostSid === you.sid;
  if (!isHost) { bar.style.display = "none"; return; }
  bar.style.display = "flex";
  const alive = room.players.filter(p => p.alive).length;
  let btns = "";
  if (room.state === "lobby") {
    btns = `<button class="btn primary" ${alive < 4 ? "disabled" : ""} onclick="send({type:'start'})">
      🎬 Начать игру (${alive}/12${alive < 4 ? " · нужно минимум 4" : ""})</button>
      <button class="btn dark" onclick="copyInvite()">📋 Скопировать приглашение</button>`;
  } else if (["night", "day", "voting"].includes(room.state)) {
    const label = { night: "🌅 Быстрее — рассвет!", day: "🗳 К голосованию", voting: "✅ Итоги голосования" }[room.state];
    btns = `<button class="btn dark" onclick="send({type:'skip'})">${label}</button>
      <button class="btn ghost" onclick="copyInvite()">📋 Код комнаты</button>`;
  } else if (room.state === "gameover") {
    btns = `<button class="btn primary" onclick="send({type:'restart'})">🔄 Новая игра (в лобби)</button>`;
  }
  bar.innerHTML = btns;
}

function renderMyCard() {
  const box = $("myCardBox");
  if (room.state === "lobby" || room.state === "dealing" || !you || !you.footballer) {
    if (room.state === "dealing") {
      box.innerHTML = `<div class="my-card"><div class="deal-overlay"><div class="spinner"></div>
        Википедия выдаёт вам легенду футбола…</div><div class="card-body" style="min-height:150px"></div></div>`;
    } else box.innerHTML = "";
    return;
  }
  const roleStrip = you.isMafia
    ? `<div class="role-strip mafia">🕶️ Вы — мафия · вы играете за ${esc(you.footballer)}</div>`
    : `<div class="role-strip citizen">🛡️ Вы — горожанин · вы ${esc(you.footballer)}</div>`;
  const img = you.image
    ? `<img class="avatar" src="${esc(you.image)}" alt="" loading="lazy" onerror="this.outerHTML='<div class=\\'avatar-ph\\'>⚽</div>'">`
    : `<div class="avatar-ph">⚽</div>`;
  const mates = you.isMafia && you.mates.length
    ? `<div class="mates">👥 Ваши подельники: <b>${you.mates.map(esc).join(", ")}</b></div>` : "";
  const target = you.isMafia && you.nightTarget ? `<div class="mates">🎯 Цель ночи: <b>${esc(you.nightTarget)}</b></div>` : "";
  const deadNote = !you.alive ? `<div class="hint" style="border-color:var(--red)">💀 Вы выбыли. Наблюдайте за живыми — роли мёртвых открыты всем.</div>` : "";
  box.innerHTML = `<div class="my-card ${you.isMafia ? "mafia" : ""}">
    ${roleStrip}
    <div class="card-body">${img}
      <div class="who">
        <div class="tag">Ваша тайная роль</div>
        <h2>${esc(you.footballer)}</h2>
        <div class="hint">📖 <b>Подсказка из Википедии:</b> ${esc(you.hint)}</div>
        ${mates}${target}${deadNote}
      </div>
    </div></div>`;
}

function renderPlayers() {
  const list = $("playersList");
  const st = room.state;
  const canKill = st === "night" && you.isMafia && you.alive;
  const canVote = st === "voting" && you.alive;
  const spectatorDead = !you.alive;
  $("aliveCount").textContent = `${room.players.filter(p => p.alive).length} живых из ${room.players.length}`;

  list.innerHTML = room.players.map(p => {
    const isMe = p.sid === you.sid;
    const initial = esc((p.name[0] || "?").toUpperCase());
    let badges = "";
    if (p.isHost) badges += `<span class="badge b-host">ХОСТ</span>`;
    badges += p.alive ? `<span class="badge b-alive">жив</span>` : `<span class="badge b-dead">💀 выбыл</span>`;
    if (st === "voting" && p.voted && p.alive && p.sid !== you.sid) badges += `<span class="badge b-voted">проголосовал</span>`;
    if ((p.revealedRole || spectatorDead || st === "gameover")) {
      badges += p.wasMafia ? `<span class="badge b-mafia">🕶 мафия</span>` : `<span class="badge b-citizen">🛡 мирный</span>`;
    }
    let sub = "";
    if (p.footballer) sub = `<small>⚽ ${esc(p.footballer)}</small>`;
    else if (st !== "lobby" && st !== "dealing") sub = `<small>роль скрыта</small>`;
    let acts = "";
    if (canKill && p.alive && !isMe) {
      const sel = you.nightTarget === p.name;
      acts += `<button class="mini killbtn ${sel ? "sel" : ""}" title="Убить ночью" onclick="send({type:'kill',target:'${p.sid}'})">🔪</button>`;
    }
    if (canVote && p.alive && !isMe) {
      const sel = you.voteFor === p.name;
      acts += `<button class="mini ${sel ? "vsel" : ""}" title="Голосовать" onclick="send({type:'vote',target:'${p.sid}'})">${sel ? "✓" : "🗳"}</button>`;
    }
    return `<div class="prow ${p.alive ? "" : "dead"} ${isMe ? "me" : ""}">
      <div class="ava">${initial}</div>
      <div class="nm"><b>${esc(p.name)}${isMe ? " (вы)" : ""}</b>${sub}</div>
      <div style="display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end">${badges}</div>
      ${acts ? `<div class="act">${acts}</div>` : ""}
    </div>`;
  }).join("");
}

function renderLog() {
  const el = $("eventLog");
  el.innerHTML = room.log.map(l => `<div class="msg evt">${fmtBold(l.text)}</div>`).join("");
  el.scrollTop = el.scrollHeight;
}

/* ---------------- чат ---------------- */

function renderChanTabs() {
  const tabs = $("chanTabs");
  if (you && you.isMafia) {
    tabs.style.display = "flex";
    tabs.innerHTML = `<button class="${chatChannel === "public" ? "on" : ""}" onclick="setChan('public')">💬 Общий</button>
      <button class="mtab ${chatChannel === "mafia" ? "on" : ""}" onclick="setChan('mafia')">🕶 Тайный</button>`;
  } else { tabs.style.display = "none"; chatChannel = "public"; }
}
function setChan(c) { chatChannel = c; renderChanTabs(); rebuildChatLog(); }

function currentMsgs() {
  return chatChannel === "mafia" ? mafiaChat : room.messages;
}

function rebuildChatLog() {
  const el = $("chatLog");
  el.innerHTML = "";
  currentMsgs().forEach(addChatBubble);
  el.scrollTop = el.scrollHeight;
}

function addChatBubble(m) {
  if (seenMsgIds.has(m.id) && chatChannel === "public") { /* дубликаты из sync пропускаем при потоковой добавке */ }
  const el = $("chatLog");
  const isSys = m.sender === "система";
  const isMe = you && m.sender === you.name;
  const div = document.createElement("div");
  div.className = "msg" + (isSys ? " sys" : "") + (isMe ? " me" : "") + (m.channel === "mafia" ? " mafia-msg" : "");
  div.innerHTML = isSys ? fmtBold(m.text) : `${isMe ? "" : `<b class="who">${esc(m.sender)}</b>`}${fmtBold(m.text)}`;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

function sendChat() {
  const inp = $("chatInput");
  const text = inp.value.trim();
  if (!text || !ws) return;
  ws.send(JSON.stringify({ type: "chat", text, channel: chatChannel }));
  inp.value = "";
}

/* ---------------- утилиты ---------------- */

function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

function copyInvite() {
  const txt = `Играй со мной в Футбольную мафию! ⚽🕶️ Открой ${location.origin} → «Присоединиться» → код ${room.code}`;
  navigator.clipboard?.writeText(txt).then(() => toast("Приглашение скопировано 📋"), () => toast(`Код комнаты: ${room.code}`));
}
$("roomCodeLbl")?.addEventListener("click", () => { if (room) copyInvite(); });

function toast(t) {
  const el = $("toast");
  el.textContent = t; el.classList.add("show");
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove("show"), 2600);
}

function openRules() { $("rulesModal").classList.add("open"); }
function closeRules() { $("rulesModal").classList.remove("open"); }

/* финальная таблица */
function renderFinale() {
  const fb = $("finaleBox");
  if (!room || room.state !== "gameover") { fb.innerHTML = ""; return; }
  const win = room.winner === "citizens";
  fb.innerHTML = `<div class="finale ${win ? "win" : "lose"}">
    <h2>${win ? "🏆 ГОРОД СПАСЁН!" : "🕶 ГОРОД ЗАХВАЧЕН"}</h2>
    <p>${win ? "Все мафиози разоблачены и изгнаны. Легенды футбола поют гимн на трибунах."
             : "Мафия сравнялась силами с горожанами. Футбольная мафия диктует правила…"}</p>
    <p>Раундов сыграно: <b>${room.round}</b></p>
    <div class="reveal-grid">${room.players.map(p => `
      <div class="rv"><b>${esc(p.name)}</b>
        <small>${p.alive ? "выжил" : "выбыл"}${p.isHost ? " · хост" : ""}</small>
        <div class="rl">${p.wasMafia ? '<span class="badge b-mafia">🕶 мафия</span>' : '<span class="badge b-citizen">🛡 горожанин</span>'}
        ${p.footballer ? `<span class="badge" style="background:rgba(245,197,24,.12);color:var(--gold)">⚽ ${esc(p.footballer)}</span>` : ""}</div>
      </div>`).join("")}
    </div></div>`;
}

document.addEventListener("keydown", e => { if (e.key === "Escape") closeRules(); });
