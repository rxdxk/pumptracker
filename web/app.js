"use strict";

// ---------------------------------------------------------------- helpers
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const short = (a) => (a ? a.slice(0, 4) + "…" + a.slice(-4) : "—");
const store = {
  get(k, d) { try { const v = localStorage.getItem("pt:" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("pt:" + k, JSON.stringify(v)); } catch { /* приватный режим */ } },
};

const state = {
  solUsd: null,
  settings: null,
  status: null,
  alerts: [],
  kinds: store.get("kinds", null),
  sound: store.get("sound", false),
  volume: store.get("volume", 0.7),
  clickAxiom: store.get("clickAxiom", true),
  notify: store.get("notify", false),
  unseen: 0,
  tab: "feed",
  live: new Map(),
};

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* не JSON */ }
    throw new Error(msg);
  }
  return r.json();
}

function compact(n) {
  const a = Math.abs(n);
  if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 1 : 2) + "M";
  if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "K";
  return n.toFixed(a >= 100 ? 0 : 1);
}
const sol = (x, d = 2) => (x == null ? "—" : `${Number(x).toFixed(d)} SOL`);
function mc(x) {
  if (!x) return "—";
  return state.solUsd ? "$" + compact(x * state.solUsd) : compact(x) + " SOL";
}
function signed(x, d = 2) {
  if (x == null) return "—";
  const cls = x > 0 ? "pos" : x < 0 ? "neg" : "";
  return `<span class="${cls}">${x > 0 ? "+" : ""}${Number(x).toFixed(d)} SOL</span>`;
}
function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return Math.floor(s) + "с";
  if (s < 3600) return Math.floor(s / 60) + "м";
  if (s < 86400) return Math.floor(s / 3600) + "ч";
  return Math.floor(s / 86400) + "д";
}
const pct = (x) => (x == null ? "—" : Math.round(x * 100) + "%");

function scoreBadge(dev) {
  if (!dev) return `<span class="score neutral">—</span>`;
  if (dev.scam) return `<span class="score scam" title="${esc("Помечен как скам" + (dev.scam_note ? ": " + dev.scam_note : ""))}">СКАМ</span>`;
  const v = dev.score == null ? "—" : dev.score;
  const title = dev.launches ? `Запусков ${dev.launches}, миграций ${dev.migrated}, сливов ${dev.dumped}` : "Истории нет";
  return `<span class="score ${dev.tone || "neutral"}" title="${esc(title)}">${v}</span>`;
}
function tokenImg(t, cls = "img") {
  const letter = esc((t.symbol || t.name || "?").slice(0, 2).toUpperCase());
  if (t.image && /^https?:/.test(t.image)) {
    return `<div class="${cls}" style="background-image:url('${esc(t.image)}')"></div>`;
  }
  return `<div class="${cls}">${letter}</div>`;
}
// Страница токена на Axiom: /meme/<адрес пары>, для токенов pump.fun это bonding curve
const axiomUrl = (t) => `https://axiom.trade/meme/${(t && (t.bonding_curve || t.mint)) || ""}`;
function clickToken(t) {
  if (!t?.mint) return;
  if (state.clickAxiom) window.open(axiomUrl(t), "_blank", "noopener");
  else openToken(t.mint);
}
function links(tok) {
  const t = typeof tok === "string" ? { mint: tok } : tok || {};
  const mint = t.mint;
  return `<div class="links">
    <a class="axiom" href="${axiomUrl(t)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">axiom</a>
    <a href="https://pump.fun/coin/${mint}" target="_blank" rel="noopener" onclick="event.stopPropagation()">pump</a>
    <a href="https://gmgn.ai/sol/token/${mint}" target="_blank" rel="noopener" onclick="event.stopPropagation()">gmgn</a>
    <a href="https://solscan.io/token/${mint}" target="_blank" rel="noopener" onclick="event.stopPropagation()">scan</a>
  </div>`;
}
function socials(t) {
  const out = [];
  if (t.twitter) out.push(`<a href="${esc(t.twitter)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="X">𝕏</a>`);
  if (t.telegram) out.push(`<a href="${esc(t.telegram)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Telegram">TG</a>`);
  if (t.website) out.push(`<a href="${esc(t.website)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Сайт">www</a>`);
  return out.join(" ");
}
const copyBtn = (v) => `<button class="copy" data-copy="${esc(v)}" title="Скопировать">⧉</button>`;

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), 2400);
}

document.addEventListener("click", (e) => {
  const c = e.target.closest("[data-copy]");
  if (c) {
    e.stopPropagation();
    navigator.clipboard?.writeText(c.dataset.copy).then(() => toast("Скопировано"));
  }
});

// ---------------------------------------------------------------- tabs
function showTab(name) {
  state.tab = name;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((s) => (s.hidden = s.id !== "tab-" + name));
  if (name === "feed") { state.unseen = 0; updateBadge(); }
  if (name === "live") loadLive();
  if (name === "devs") loadDevs();
  if (name === "smart") { loadLeaderboard(); loadTracked(); }
  if (name === "filters") renderSettings();
  store.set("tab", name);
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (b) showTab(b.dataset.tab);
});
function updateBadge() {
  const b = $("#feedBadge");
  b.hidden = state.unseen === 0;
  b.textContent = state.unseen > 99 ? "99+" : state.unseen;
}

// ---------------------------------------------------------------- status
async function loadStatus() {
  try {
    const s = await api("/api/status");
    state.status = s;
    if (s.sol_usd) state.solUsd = s.sol_usd;
    const st = $("#status");
    st.classList.toggle("on", s.connected);
    st.classList.toggle("off", !s.connected);
    $("#stConn").textContent = s.connected ? "pump.fun live" : "нет связи с RPC";
    $("#stRate").textContent = `${s.rate} соб/с`;
    $("#stSol").textContent = s.sol_usd ? `SOL $${s.sol_usd.toFixed(2)}` : "SOL —";
    $("#tiles").innerHTML = [
      ["Токенов в базе", compact(s.tokens)],
      ["Девов", compact(s.devs)],
      ["Новых за сессию", s.creates],
      ["Миграций", s.migrations],
      ["Позиций кошельков", compact(s.positions)],
      ["Отслеживаю", s.tracked],
    ].map(([l, v]) => `<div class="tile"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");
  } catch {
    $("#status").classList.add("off");
    $("#stConn").textContent = "сервер недоступен";
  }
}

// ---------------------------------------------------------------- feed
const KINDS = {
  new_token: { label: "Прошли фильтры", icon: "🚀" },
  tracked_dev: { label: "Мои девы", icon: "👀" },
  confluence: { label: "Умные деньги", icon: "🔥" },
  wallet_trade: { label: "Кошельки", icon: "💼" },
  migration: { label: "Миграции", icon: "🎓" },
  dev_dump: { label: "Сливы", icon: "🩸" },
};
if (!state.kinds) state.kinds = Object.keys(KINDS);

function renderChips() {
  $("#kindChips").innerHTML = Object.entries(KINDS).map(([k, v]) =>
    `<button class="chip k-${k} ${state.kinds.includes(k) ? "on" : ""}" data-kind="${k}">${v.icon} ${v.label}</button>`).join("");
}
$("#kindChips").addEventListener("click", (e) => {
  const b = e.target.closest("[data-kind]");
  if (!b) return;
  const k = b.dataset.kind;
  state.kinds = state.kinds.includes(k) ? state.kinds.filter((x) => x !== k) : [...state.kinds, k];
  store.set("kinds", state.kinds);
  renderChips();
  renderFeed();
});

function alertCard(a, fresh = false) {
  const d = a.data || {};
  const t = d.token || {};
  const dev = d.dev;
  const kind = KINDS[a.kind] || { label: a.kind, icon: "•" };
  const meta = [];
  if (t.ath_mcap_sol && t.ath_mcap_sol > (t.mcap_sol || 0) * 1.05) meta.push(`<span>ATH <b>${mc(t.ath_mcap_sol)}</b></span>`);
  if (t.dev_buy_sol) meta.push(`<span>дев купил <b>${Number(t.dev_buy_sol).toFixed(2)} SOL</b></span>`);
  if (t.created_at) meta.push(`<span>возраст <b>${ago(t.created_at)}</b></span>`);
  meta.push(`<span class="curve"><span class="bar ${t.completed ? "done" : ""}"><i style="width:${Math.round((t.progress || 0) * 100)}%"></i></span> <b>${t.completed ? "DEX" : pct(t.progress)}</b></span>`);
  const devBlock = dev ? `
    <div class="devchip" data-dev="${dev.wallet}" title="Профиль дева">
      ${scoreBadge(dev)}
      <span><b>${dev.launches}</b> зап.</span><span class="pos"><b>${dev.migrated}</b> миг.</span>${dev.dumped ? `<span class="neg"><b>${dev.dumped}</b> слив.</span>` : ""}
      ${historyDots((dev.history || []).slice(0, 10))}
    </div>` : "";

  const extra = [];
  if (a.kind === "wallet_trade") {
    const side = d.side === "buy" ? `<span class="pos">купил</span>` : `<span class="neg">продал</span>`;
    const amount = d.sol != null
      ? `на <b>${Number(d.sol).toFixed(2)} SOL</b>`
      : `<b>${compact(Number(d.tokens || 0))}</b> токенов <span class="muted small">(не за SOL)</span>`;
    extra.push(`<span><span class="tag a">${esc(d.label || short(d.wallet))}</span> ${side} ${amount}</span>`);
  }
  if (a.kind === "confluence") {
    extra.push(`<span><b>${d.wallets.length}</b> умных кошелька за ${d.window_min} мин: ` +
      d.wallets.map((w) => `<span class="tag a">${esc(w.label || short(w.wallet))}</span>`).join(" ") + `</span>`);
  }
  if (a.kind === "migration" && t.created_at && t.completed_at && t.source === "live") {
    extra.push(`<span>дошёл до миграции за <b>${durFmt(t.completed_at - t.created_at)}</b></span>`);
  }
  if (a.kind === "dev_dump") extra.push(d.sol ? `<span>дев вывел <b>${Number(d.sol).toFixed(2)} SOL</b>, PnL дева ${signed(d.dev_pnl)}</span>` : `<span>дев продал 80%+ своих токенов</span>`);

  const tags = [];
  (d.reasons || []).forEach((r) => tags.push(`<span class="tag g">${esc(r)}</span>`));
  (dev?.flags || []).filter((f) => !/миграц/.test(f)).forEach((f) => tags.push(`<span class="tag ${/слив|серий/.test(f) ? "r" : ""}">${esc(f)}</span>`));
  const soc = socials(t);
  if (soc) tags.push(`<span class="tag">${soc}</span>`);
  if (d.checked_in) tags.push(`<span class="tag">проверен за ${d.checked_in}с</span>`);

  const el = document.createElement("div");
  el.className = `card k-${a.kind}${fresh ? " fresh" : ""}`;
  el.dataset.mint = a.mint || "";
  el.innerHTML = `
    ${tokenImg(t)}
    <div class="body">
      <div class="kind">${kind.icon} ${kind.label} <span class="time" title="${new Date(a.ts * 1000).toLocaleString()}">· ${ago(a.ts)} назад</span></div>
      <div class="title">${esc(t.name || "?")} <span class="sym">$${esc(t.symbol || "")}</span> <span class="mint mono">${short(t.mint)} ${copyBtn(t.mint || "")}</span></div>
      ${extra.length ? `<div class="meta hi">${extra.join("")}</div>` : ""}
      <div class="meta">${meta.join("")}</div>
      ${devBlock}
      ${tags.length ? `<div class="reasons">${tags.join("")}</div>` : ""}
    </div>
    <div class="right">
      <div class="mcbig"><span class="l">MC</span>${mc(t.mcap_sol)}</div>
      ${a.mint ? `<div class="right-actions">${links({ ...t, mint: t.mint || a.mint })}<button class="info-btn" data-info title="Подробности о токене и деве">ⓘ</button></div>` : ""}
    </div>`;
  el.title = state.clickAxiom ? "Открыть на Axiom" : "Подробности";
  el.addEventListener("click", (e) => {
    const dv = e.target.closest("[data-dev]");
    if (dv) { e.stopPropagation(); return openAddress(dv.dataset.dev); }
    if (e.target.closest("[data-info]")) { e.stopPropagation(); return a.mint && openToken(a.mint); }
    if (e.target.closest("[data-copy]")) return;
    clickToken({ ...t, mint: t.mint || a.mint });
  });
  return el;
}
function durFmt(s) {
  s = Math.max(0, s);
  if (s < 60) return Math.round(s) + " с";
  if (s < 3600) return Math.round(s / 60) + " мин";
  if (s < 86400) return (s / 3600).toFixed(1) + " ч";
  return (s / 86400).toFixed(1) + " д";
}

function renderFeed() {
  const feed = $("#feed");
  feed.innerHTML = "";
  const list = state.alerts.filter((a) => state.kinds.includes(a.kind)).slice(0, 200);
  list.forEach((a) => feed.appendChild(alertCard(a)));
  $("#feedEmpty").hidden = list.length > 0;
}
function pushAlert(a) {
  state.alerts.unshift(a);
  if (state.alerts.length > 500) state.alerts.length = 500;
  if (!state.kinds.includes(a.kind)) return;
  $("#feed").prepend(alertCard(a, true));
  $("#feedEmpty").hidden = true;
  if (state.tab !== "feed" || document.hidden) { state.unseen++; updateBadge(); }
  if (state.sound) beep(a.kind, a.data?.side);
  if (state.notify && document.hidden && "Notification" in window && Notification.permission === "granted") {
    const t = a.data?.token || {};
    const n = new Notification(`${KINDS[a.kind]?.icon || ""} ${t.name || ""} $${t.symbol || ""}`, {
      body: KINDS[a.kind]?.label + (t.mcap_sol ? ` · MC ${mc(t.mcap_sol)}` : ""), tag: "pt" + a.id,
    });
    n.onclick = () => { window.focus(); a.mint && openToken(a.mint); };
  }
}
// перерисовываем «N назад» раз в 20 секунд
setInterval(() => $$("#feed .card .time").forEach((el, i) => {
  const a = state.alerts.filter((x) => state.kinds.includes(x.kind))[i];
  if (a) el.textContent = "· " + ago(a.ts) + " назад";
}), 20000);

// ---------------------------------------------------------------- sound
// Небольшой синтезатор на WebAudio: у каждого типа алерта своя мелодия.
const SFX = (() => {
  let ctx, master, dry, wet;
  const midi = (m) => 440 * 2 ** ((m - 69) / 12);

  function init() {
    if (ctx) return ctx.state === "suspended" ? ctx.resume() : null;
    ctx = new AudioContext();
    master = ctx.createGain();
    const comp = ctx.createDynamicsCompressor();
    comp.threshold.value = -14; comp.ratio.value = 4;
    master.connect(comp).connect(ctx.destination);
    dry = ctx.createGain(); dry.connect(master);
    // лёгкое эхо, чтобы звук был «объёмным»
    const delay = ctx.createDelay(1), fb = ctx.createGain(), lp = ctx.createBiquadFilter();
    delay.delayTime.value = 0.16; fb.gain.value = 0.28; lp.type = "lowpass"; lp.frequency.value = 3200;
    wet = ctx.createGain(); wet.gain.value = 0.32;
    wet.connect(delay); delay.connect(lp).connect(fb).connect(delay); lp.connect(master);
    setVolume(state.volume);
  }
  function setVolume(v) { if (master) master.gain.value = Math.max(0, Math.min(1, v)) * 0.9; }
  function out(node) { node.connect(dry); node.connect(wet); }

  // колокольчик: основной тон + обертоны с быстрым затуханием
  function bell(m, t, { dur = 0.9, vel = 0.5 } = {}) {
    const f = midi(m);
    [[1, 1, dur], [2.01, 0.38, dur * 0.6], [3.02, 0.16, dur * 0.35], [4.2, 0.07, dur * 0.2]].forEach(([mul, amp, d]) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = f * mul;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(vel * amp, t + 0.006);
      g.gain.exponentialRampToValueAtTime(0.0001, t + d);
      o.connect(g); out(g); o.start(t); o.stop(t + d + 0.05);
    });
  }
  // «медь» для фанфар: пила через фильтр с раскрытием
  function brass(m, t, { dur = 0.3, vel = 0.32 } = {}) {
    const o = ctx.createOscillator(), o2 = ctx.createOscillator(), f = ctx.createBiquadFilter(), g = ctx.createGain();
    o.type = "sawtooth"; o2.type = "square"; o.frequency.value = midi(m); o2.frequency.value = midi(m) * 1.003;
    f.type = "lowpass"; f.Q.value = 2;
    f.frequency.setValueAtTime(500, t); f.frequency.exponentialRampToValueAtTime(3500, t + 0.06); f.frequency.exponentialRampToValueAtTime(1400, t + dur);
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(vel, t + 0.025);
    g.gain.setValueAtTime(vel, t + dur * 0.7); g.gain.exponentialRampToValueAtTime(0.0001, t + dur + 0.12);
    const g2 = ctx.createGain(); g2.gain.value = 0.35;
    o.connect(f); o2.connect(g2).connect(f); f.connect(g); out(g);
    [o, o2].forEach((x) => { x.start(t); x.stop(t + dur + 0.2); });
  }
  // тревожный «зуммер»: низкая пила с вибрато и падением высоты
  function buzz(m, t, { dur = 0.22, vel = 0.4, to = null } = {}) {
    const o = ctx.createOscillator(), lfo = ctx.createOscillator(), lg = ctx.createGain(), f = ctx.createBiquadFilter(), g = ctx.createGain();
    o.type = "sawtooth"; o.frequency.setValueAtTime(midi(m), t);
    if (to != null) o.frequency.exponentialRampToValueAtTime(midi(to), t + dur);
    lfo.frequency.value = 18; lg.gain.value = 6; lfo.connect(lg).connect(o.frequency);
    f.type = "lowpass"; f.frequency.value = 1100; f.Q.value = 6;
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(vel, t + 0.01); g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(f).connect(g); out(g);
    [o, lfo].forEach((x) => { x.start(t); x.stop(t + dur + 0.05); });
  }

  const SONGS = {
    // 🔥 умные деньги: яркое восходящее арпеджио + искры сверху
    confluence: (t) => {
      [72, 76, 79, 84].forEach((m, i) => bell(m, t + i * 0.07, { vel: 0.5, dur: i === 3 ? 1.4 : 0.6 }));
      [96, 100, 103, 108].forEach((m, i) => bell(m, t + 0.32 + i * 0.045, { vel: 0.12, dur: 0.35 }));
      brass(60, t + 0.28, { dur: 0.45, vel: 0.14 });
    },
    // 🚀 прошли фильтры: двойной восходящий «дзынь»
    new_token: (t) => { bell(79, t, { vel: 0.5, dur: 0.6 }); bell(86, t + 0.11, { vel: 0.55, dur: 1.0 }); },
    // 👀 мой дев запустил: три колокольчика (мажорная терция вверх)
    tracked_dev: (t) => [76, 80, 83].forEach((m, i) => bell(m, t + i * 0.12, { vel: 0.5, dur: i === 2 ? 1.2 : 0.7 })),
    // 💼 сделка кошелька: покупка вверх, продажа вниз
    buy: (t) => { bell(81, t, { vel: 0.45, dur: 0.4 }); bell(88, t + 0.08, { vel: 0.45, dur: 0.8 }); },
    sell: (t) => { bell(88, t, { vel: 0.4, dur: 0.4 }); bell(81, t + 0.09, { vel: 0.4, dur: 0.8 }); },
    // 🎓 миграция: короткие фанфары и финальный аккорд
    migration: (t) => {
      [[67, 0], [72, 0.12], [76, 0.24]].forEach(([m, d]) => brass(m, t + d, { dur: 0.13 }));
      [72, 76, 79].forEach((m) => brass(m, t + 0.38, { dur: 0.6, vel: 0.22 }));
      bell(84, t + 0.38, { vel: 0.3, dur: 1.3 });
    },
    // 🩸 слив дева: тревожный нисходящий сигнал
    dev_dump: (t) => { buzz(57, t, { to: 55 }); buzz(53, t + 0.24, { to: 50 }); buzz(50, t + 0.48, { dur: 0.4, to: 45 }); },
    default: (t) => bell(81, t, { vel: 0.4 }),
  };

  return {
    play(kind, side) {
      try {
        init();
        const key = kind === "wallet_trade" ? (side === "sell" ? "sell" : "buy") : kind;
        (SONGS[key] || SONGS.default)(ctx.currentTime + 0.03);
      } catch { /* без звука */ }
    },
    setVolume,
    unlock() { try { init(); } catch { /* без звука */ } },
  };
})();
function beep(kind, side) { SFX.play(kind, side); }
// браузеры разрешают звук только после действия пользователя
document.addEventListener("pointerdown", () => { if (state.sound) SFX.unlock(); }, { once: true });

const SOUND_PREVIEW = [
  ["confluence", "🔥 Умные деньги"], ["new_token", "🚀 Прошли фильтры"], ["tracked_dev", "👀 Мой дев"],
  ["wallet_trade:buy", "💼 Покупка"], ["wallet_trade:sell", "💼 Продажа"], ["migration", "🎓 Миграция"], ["dev_dump", "🩸 Слив"],
];
function renderSoundPop() {
  $("#soundPop").innerHTML = `
    <div class="pop-row"><span>Громкость</span><input type="range" id="volRange" min="0" max="100" value="${Math.round(state.volume * 100)}"><b id="volVal">${Math.round(state.volume * 100)}%</b></div>
    <div class="pop-title">Послушать</div>
    <div class="pop-grid">${SOUND_PREVIEW.map(([k, l]) => `<button class="chip" data-preview="${k}">${l}</button>`).join("")}</div>`;
  $("#volRange").addEventListener("input", (e) => {
    state.volume = e.target.value / 100;
    $("#volVal").textContent = e.target.value + "%";
    store.set("volume", state.volume);
    SFX.setVolume(state.volume);
  });
  $("#volRange").addEventListener("change", () => SFX.play("new_token"));
}
$("#soundPop").addEventListener("click", (e) => {
  const b = e.target.closest("[data-preview]");
  if (!b) return;
  const [k, side] = b.dataset.preview.split(":");
  SFX.play(k, side);
});
$("#btnSoundCfg").addEventListener("click", (e) => {
  e.stopPropagation();
  const p = $("#soundPop");
  p.hidden = !p.hidden;
  if (!p.hidden) renderSoundPop();
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#soundPop, #btnSoundCfg")) $("#soundPop").hidden = true;
});

function syncToggles() {
  $$(".btn-click-axiom").forEach((b) => {
    b.classList.toggle("on", state.clickAxiom);
    b.textContent = state.clickAxiom ? "↗ Клик → Axiom" : "ⓘ Клик → подробности";
  });
  $("#btnSound").classList.toggle("on", state.sound);
  $("#btnSound").textContent = state.sound ? "🔊 Звук" : "🔈 Звук";
  $("#btnNotify").classList.toggle("on", state.notify);
}
$$(".btn-click-axiom").forEach((b) => b.addEventListener("click", () => {
  state.clickAxiom = !state.clickAxiom;
  store.set("clickAxiom", state.clickAxiom);
  syncToggles();
  toast(state.clickAxiom ? "Клик по токену открывает Axiom, подробности — кнопка ⓘ" : "Клик по токену открывает подробности");
}));
$("#btnSound").addEventListener("click", () => { state.sound = !state.sound; store.set("sound", state.sound); syncToggles(); if (state.sound) SFX.play("new_token"); });
$("#btnNotify").addEventListener("click", async () => {
  if (!("Notification" in window)) return toast("Браузер не поддерживает уведомления");
  if (!state.notify && Notification.permission !== "granted") {
    const p = await Notification.requestPermission();
    if (p !== "granted") return toast("Уведомления запрещены в браузере");
  }
  state.notify = !state.notify;
  store.set("notify", state.notify);
  syncToggles();
  toast(state.notify ? "Уведомления включены, когда вкладка в фоне" : "Уведомления выключены");
});

// ---------------------------------------------------------------- live tokens
function liveRow(item, flash = false) {
  const t = item.token, dev = item.dev;
  const tr = document.createElement("tr");
  tr.dataset.mint = t.mint;
  if (flash) tr.className = "flash";
  tr.innerHTML = `
    <td><div class="tk">${tokenImg(t)}<div><div class="n">${esc(t.name || "?")}</div><div class="s">$${esc(t.symbol || "")} ${socials(t)}</div></div></div></td>
    <td class="muted" data-ts="${t.created_at}">${ago(t.created_at)}</td>
    <td>${scoreBadge(dev)} <span class="muted small">${dev ? `${dev.launches}/${dev.migrated}` : ""}</span></td>
    <td class="r mono">${t.dev_buy_sol ? Number(t.dev_buy_sol).toFixed(2) : "—"}</td>
    <td class="r mono">${mc(t.mcap_sol)}</td>
    <td><div class="bar ${t.completed ? "done" : ""}"><i style="width:${Math.round((t.progress || 0) * 100)}%"></i></div></td>
    <td class="r mono">${(t.buys || 0) + (t.sells || 0)}</td>
    <td><div class="right-actions">${links(t)}<button class="info-btn" data-info title="Подробности о токене и деве">ⓘ</button></div></td>`;
  tr.addEventListener("click", (e) => {
    if (e.target.closest("[data-info]")) return openToken(t.mint);
    clickToken(t);
  });
  return tr;
}
function renderLive() {
  const onlyKnown = $("#liveOnlyKnown").checked;
  const rows = [...state.live.values()]
    .filter((x) => !onlyKnown || (x.dev && x.dev.launches > 0))
    .sort((a, b) => (b.token.created_at || 0) - (a.token.created_at || 0)).slice(0, 150);
  const tb = $("#liveTable tbody");
  tb.innerHTML = "";
  rows.forEach((r) => tb.appendChild(liveRow(r)));
  if (!rows.length) tb.innerHTML = `<tr><td colspan="8" class="empty small">Ждём новые токены…</td></tr>`;
}
async function loadLive() {
  try {
    const list = await api("/api/tokens/recent?limit=150");
    list.forEach((x) => state.live.set(x.token.mint, x));
    renderLive();
  } catch (e) { toast(e.message); }
}
$("#liveOnlyKnown").addEventListener("change", renderLive);
setInterval(() => { if (state.tab === "live" && !document.hidden) loadLive(); }, 8000);

// ---------------------------------------------------------------- devs
let devMode = store.get("devMode", "best");
const DEV_EMPTY = {
  best: "Девов с миграциями пока нет: трекер видит только то, что произошло после запуска. Через пару часов здесь будет рейтинг. Чтобы ускорить, открой любого дева через поиск и нажми «Подтянуть историю».",
  all: "Пока нет девов с двумя и более запусками.",
  bad: "Пока никто не попался на сливе. Хороший знак, но вряд ли надолго.",
  scam: "Ты ещё никого не пометил. Нажми 🚩 в строке дева или в его профиле. Токены скам-девов не попадут в алерты.",
};

async function toggleScam(wallet, isScam) {
  if (isScam) {
    await api("/api/scam/" + wallet, { method: "DELETE" });
    toast("Пометка «скам» снята");
    return false;
  }
  const note = window.prompt("Пометить дева как скам. Заметка (необязательно): что он сделал?", "");
  if (note === null) return null;
  await api("/api/scam", { method: "POST", body: JSON.stringify({ wallet, note }) });
  toast("🚩 Дев помечен как скам. Его токены не попадут в алерты");
  return true;
}
function scoreCell(d) {
  if (d.scam) return `<div class="scorecell">${scoreBadge(d)}</div>`;
  const v = d.score ?? 0;
  return `<div class="scorecell">${scoreBadge(d)}<div class="sbar ${d.tone}"><i style="width:${v}%"></i></div></div>`;
}
function historyDots(h) {
  if (!h || !h.length) return `<span class="muted small">—</span>`;
  return `<div class="hdots">${h.slice().reverse().map((x) =>
    `<i class="hd ${x.outcome}" title="$${esc(x.symbol || "?")} · ${x.outcome === "migrated" ? "миграция" : x.outcome === "dumped" ? "дев слил" : "ATH " + mc(x.ath)}"></i>`).join("")}</div>`;
}
async function loadDevs() {
  $$("#devMode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === devMode));
  const tb = $("#devTable tbody");
  tb.innerHTML = Array.from({ length: 6 }, () => `<tr><td colspan="10"><div class="skeleton"></div></td></tr>`).join("");
  const rows = await api("/api/devs/top?mode=" + devMode).catch(() => []);
  const empty = $("#devEmpty");
  empty.hidden = rows.length > 0;
  empty.textContent = DEV_EMPTY[devMode];
  tb.innerHTML = rows.map((d, i) => `
    <tr data-wallet="${d.wallet}" class="${d.scam ? "scam" : ""}">
      <td class="c rank ${i < 3 && devMode === "best" ? "top" + (i + 1) : ""}">${i + 1}</td>
      <td><div class="who"><span class="avatar" style="--h:${hue(d.wallet)}"></span><div>
        <div class="mono">${d.label ? `<b>${esc(d.label)}</b> · ` : ""}${short(d.wallet)} ${copyBtn(d.wallet)}</div>
        <div class="flags">${d.flags.filter((f) => f !== "скам").map((f) => `<span class="tag ${/слив|серий/.test(f) ? "r" : /миграц/.test(f) ? "g" : ""}">${esc(f)}</span>`).join("")}${d.scam_note ? `<span class="tag r" title="${esc(d.scam_note)}">🚩 ${esc(d.scam_note.length > 40 ? d.scam_note.slice(0, 40) + "…" : d.scam_note)}</span>` : ""}</div>
      </div></div></td>
      <td>${scoreCell(d)}</td>
      <td class="r mono">${d.launches}</td>
      <td><div class="migcell"><span class="mono pos">${d.migrated}</span><div class="bar sm"><i style="width:${Math.round(d.migration_rate * 100)}%"></i></div><span class="muted small">${pct(d.migration_rate)}</span></div></td>
      <td class="r mono ${d.dumped ? "neg" : "muted"}">${d.dumped}</td>
      <td class="r mono">${mc(d.avg_ath_sol)}</td>
      <td>${historyDots(d.history)}</td>
      <td class="r muted">${ago(d.last_launch)}</td>
      <td class="r"><div class="actions-cell">${trackBtn(d.wallet, "dev", d.tracked)}
        <button class="icon-btn flag ${d.scam ? "on" : ""}" data-scam="${d.wallet}" data-is-scam="${d.scam ? 1 : 0}" title="${d.scam ? "Снять пометку «скам»" : "Пометить как скам"}">🚩</button></div></td>
    </tr>`).join("");
}
function hue(s) {
  let h = 2166136261;
  for (const c of s) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); }
  return (h >>> 0) % 360;
}
$("#devMode").addEventListener("click", (e) => {
  const b = e.target.closest("[data-mode]");
  if (!b) return;
  devMode = b.dataset.mode;
  store.set("devMode", devMode);
  loadDevs();
});
$("#devTable").addEventListener("click", async (e) => {
  const s = e.target.closest("[data-scam]");
  if (s) {
    e.stopPropagation();
    try { if ((await toggleScam(s.dataset.scam, s.dataset.isScam === "1")) !== null) loadDevs(); } catch (err) { toast(err.message); }
    return;
  }
  const b = e.target.closest("[data-track]");
  if (b) { e.stopPropagation(); await toggleTrack(b); loadDevs(); return; }
  const tr = e.target.closest("tr[data-wallet]");
  if (tr) openAddress(tr.dataset.wallet);
});

// ---------------------------------------------------------------- smart money
async function loadLeaderboard() {
  const tb = $("#lbTable tbody");
  tb.innerHTML = `<tr><td colspan="8"><div class="skeleton"></div></td></tr>`;
  const rows = await api("/api/smart/leaderboard").catch(() => []);
  $("#lbEmpty").hidden = rows.length > 0;
  tb.innerHTML = rows.map((r, i) => `
    <tr data-wallet="${r.wallet}">
      <td class="muted">${i + 1}</td>
      <td class="mono">${r.label ? `<b>${esc(r.label)}</b> ` : ""}${short(r.wallet)} ${copyBtn(r.wallet)}
        ${r.smart ? '<span class="tag g">умный</span>' : ""}${r.bot ? '<span class="tag">бот?</span>' : ""}</td>
      <td class="r mono">${signed(r.pnl)}</td>
      <td class="r mono">${Number(r.spent).toFixed(1)}</td>
      <td class="r mono">${r.tokens}</td>
      <td class="r mono">${pct(r.wins / r.tokens)}</td>
      <td class="muted">${ago(r.last_at)} назад</td>
      <td>${trackBtn(r.wallet, "trader", r.tracked)}</td>
    </tr>`).join("");
}
$("#lbTable").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-track]");
  if (b) { e.stopPropagation(); await toggleTrack(b); loadLeaderboard(); return; }
  const tr = e.target.closest("tr[data-wallet]");
  if (tr) openAddress(tr.dataset.wallet);
});
$("#btnLbRefresh").addEventListener("click", async () => {
  await api("/api/smart/refresh", { method: "POST" });
  loadLeaderboard();
  toast("Рейтинг пересчитан");
});

async function loadTracked() {
  const list = await api("/api/tracked").catch(() => []);
  $("#trackedList").innerHTML = list.length ? list.map((w) => `
    <div class="item" data-wallet="${w.wallet}">
      <div><div class="lbl">${esc(w.label)}</div><div class="addr">${short(w.wallet)}</div></div>
      <span class="kind">${w.kind === "dev" ? "дев" : "трейдер"}</span>
      <button data-untrack="${w.wallet}" title="Убрать">✕</button>
    </div>`).join("") : `<div class="muted small">Добавь кошельки KOL-ов, сильных трейдеров или девов. Их сделки и запуски будут приходить в ленту.</div>`;
}
$("#trackedList").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-untrack]");
  if (b) {
    e.stopPropagation();
    await api("/api/tracked/" + b.dataset.untrack, { method: "DELETE" });
    loadTracked();
    return;
  }
  const it = e.target.closest("[data-wallet]");
  if (it) openAddress(it.dataset.wallet);
});
$("#trackForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/tracked", { method: "POST", body: JSON.stringify(Object.fromEntries(f)) });
    e.target.reset();
    loadTracked();
    toast("Кошелёк добавлен");
  } catch (err) { toast(err.message); }
});
async function quickTrack(wallet, kind, label = "") {
  try {
    await api("/api/tracked", { method: "POST", body: JSON.stringify({ wallet, kind, label }) });
    toast(kind === "dev" ? "Слежу за девом" : "Слежу за кошельком");
  } catch (e) { toast(e.message); }
}
async function untrack(wallet) {
  try {
    await api("/api/tracked/" + wallet, { method: "DELETE" });
    toast("Убрано из отслеживаемых");
  } catch (e) { toast(e.message); }
}
// Кнопка-переключатель: data-tracked="1" — убрать, иначе — добавить
async function toggleTrack(btn) {
  if (btn.dataset.tracked === "1") await untrack(btn.dataset.track);
  else await quickTrack(btn.dataset.track, btn.dataset.kind);
  loadTracked();
}
const trackBtn = (wallet, kind, tracked, small = true) => tracked
  ? `<button class="ghost ${small ? "small" : ""} on untrack" data-track="${wallet}" data-kind="${kind}" data-tracked="1" title="Нажми, чтобы убрать из отслеживаемых"><span class="t-on">✓ Слежу</span><span class="t-off">✕ Убрать</span></button>`
  : `<button class="ghost ${small ? "small" : ""}" data-track="${wallet}" data-kind="${kind}" data-tracked="0">+ Следить${small ? "" : kind === "dev" ? " как за девом" : " как за трейдером"}</button>`;

// ---------------------------------------------------------------- settings
const SETTINGS_SCHEMA = [
  { key: "new_token", title: "🚀 Новые токены", toggle: "enabled", desc: "Алерт, когда новый токен на pump.fun проходит фильтры по деву.", fields: [
    ["dev_min_migrated", "Мин. миграций у дева", "int", "Главный фильтр качества"],
    ["dev_min_launches", "Мин. прошлых запусков", "int"],
    ["dev_max_launches", "Макс. запусков", "int", "0 — без лимита. Отсекает серийных лончеров"],
    ["dev_max_dump_rate", "Макс. доля сливов", "float", "0–1, где 1 — не фильтровать"],
    ["dev_min_score", "Мин. скор дева", "int", "0–100"],
    ["dev_buy_min_sol", "Мин. покупка дева, SOL", "float"],
    ["dev_buy_max_sol", "Макс. покупка дева, SOL", "float", "0 — без лимита"],
    ["require_socials", "Только с соцсетями", "bool"],
    ["keywords_include", "Ключевые слова", "text", "Через запятую. Пусто — любые"],
    ["keywords_exclude", "Исключить слова", "text"],
    ["backfill_unknown_dev", "Подтягивать историю незнакомых девов", "bool", "Запросы к RPC расходуют кредиты Helius"],
    ["backfill_per_hour", "Лимит подтягиваний в час", "int"],
    ["backfill_pages", "Глубина истории (Helius), страниц", "int", "1 страница ≈ до 100 запусков, 2–5 секунд"],
    ["backfill_depth", "Глубина без Helius, транзакций", "int"],
  ] },
  { key: "confluence", title: "🔥 Умные деньги заходят", toggle: "enabled", desc: "Несколько умных кошельков покупают один токен за короткое время.", fields: [
    ["min_wallets", "Мин. кошельков", "int"],
    ["window_min", "Окно, минут", "int"],
    ["use_top", "Учитывать топ рейтинга", "bool", "Кроме твоих отслеживаемых"],
    ["top_n", "Сколько кошельков из топа", "int"],
    ["smart_min_pnl", "Умный: мин. PnL за 7 дней, SOL", "float"],
    ["smart_min_winrate", "Умный: мин. winrate", "float", "0–1"],
    ["smart_min_tokens", "Умный: мин. токенов", "int"],
    ["smart_max_tokens", "Умный: макс. токенов", "int", "Больше — скорее всего бот"],
  ] },
  { key: "tracked", title: "💼 Отслеживаемые кошельки", desc: "Сделки и запуски кошельков из твоего списка.", fields: [
    ["buys", "Покупки", "bool"],
    ["sells", "Продажи", "bool"],
    ["min_sol", "Мин. сумма сделки, SOL", "float"],
    ["dev_launch", "Запуск токена отслеживаемым девом", "bool"],
  ] },
  { key: "migration", title: "🎓 Миграции", toggle: "enabled", desc: "Токен прошёл bonding curve и ушёл на DEX.", fields: [] },
  { key: "scout", title: "🛰 Разведчик девов", toggle: "enabled", desc: "При каждой миграции трекер сам подтягивает полную историю дева, и рейтинг «Лучшие девы» пополняется автоматически.", fields: [
    ["per_hour", "Девов в час", "int", "Ограничение расхода кредитов Helius"],
    ["pages", "Глубина, страниц", "int", "1 страница ≈ до 100 запусков"],
  ] },
  { key: "dev_dump", title: "🩸 Слив дева", toggle: "enabled", desc: "Дев продал 80%+ своих токенов. Только для токенов, по которым уже был алерт.", fields: [] },
  { key: "telegram", title: "✈️ Telegram", toggle: "enabled", desc: "Дублировать алерты в Telegram. Токен бота и chat id указываются в файле .env.", fields: [] },
];

async function renderSettings() {
  state.settings = await api("/api/settings");
  const s = state.settings;
  const form = $("#settingsForm");
  form.innerHTML = SETTINGS_SCHEMA.map((g) => {
    const v = s[g.key] || {};
    const head = g.toggle
      ? `<div class="panel-head"><h3>${g.title}</h3><label class="switch"><input type="checkbox" data-k="${g.key}.${g.toggle}" ${v[g.toggle] ? "checked" : ""}></label></div>`
      : `<h3>${g.title}</h3>`;
    const fields = g.fields.map(([k, label, type, hint]) => {
      const name = `${g.key}.${k}`;
      const h = hint ? `<span class="hint">${esc(hint)}</span>` : "";
      if (type === "bool") return `<div class="field"><label>${label}${h}</label><label class="switch" style="justify-self:end"><input type="checkbox" data-k="${name}" ${v[k] ? "checked" : ""}></label></div>`;
      if (type === "text") return `<div class="field wide"><label>${label}${h}</label><input type="text" data-k="${name}" value="${esc(v[k])}"></div>`;
      return `<div class="field"><label>${label}${h}</label><input type="number" step="${type === "float" ? "0.05" : "1"}" min="0" data-k="${name}" data-t="${type}" value="${v[k]}"></div>`;
    }).join("");
    let extra = "";
    if (g.key === "telegram") {
      const ok = state.status?.telegram_configured;
      extra = `<div class="muted small">${ok ? "✓ Бот настроен в .env" : "Бот не настроен: заполни TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env и перезапусти."}</div>
        ${ok ? `<button type="button" class="ghost small" id="tgTest" style="align-self:start">Отправить тест</button>` : ""}`;
    }
    return `<div class="panel">${head}${g.desc ? `<div class="muted small">${g.desc}</div>` : ""}${fields}${extra}</div>`;
  }).join("") + `<div class="save-bar"><span class="muted small" id="saveNote"></span><button class="primary" type="submit">Сохранить фильтры</button></div>`;
  $("#tgTest")?.addEventListener("click", async () => {
    try { await api("/api/telegram/test", { method: "POST" }); toast("Отправлено"); } catch (e) { toast(e.message); }
  });
}
$("#settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const s = structuredClone(state.settings);
  $$("[data-k]", e.target).forEach((el) => {
    const [g, k] = el.dataset.k.split(".");
    if (el.type === "checkbox") s[g][k] = el.checked;
    else if (el.type === "number") s[g][k] = el.dataset.t === "float" ? parseFloat(el.value || 0) : parseInt(el.value || 0, 10);
    else s[g][k] = el.value;
  });
  try {
    state.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(s) });
    $("#saveNote").textContent = "Сохранено " + new Date().toLocaleTimeString();
    toast("Фильтры сохранены");
  } catch (err) { toast(err.message); }
});

// ---------------------------------------------------------------- drawer
function openDrawer(html) {
  $("#drawerBody").innerHTML = html;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawerBg").hidden = false;
  $("#drawer").scrollTop = 0;
}
function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#drawerBg").hidden = true;
}
$("#drawerClose").addEventListener("click", closeDrawer);
$("#drawerBg").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDrawer();
  if (e.key === "/" && document.activeElement.tagName !== "INPUT") { e.preventDefault(); $("#searchInput").focus(); }
});
const tile = (l, v) => `<div class="tile"><div class="v">${v}</div><div class="l">${l}</div></div>`;
const loading = `<div class="skeleton" style="width:60%;height:22px;margin:10px 0 20px"></div><div class="skeleton"></div>`;

function devSection(dev, withHeader = true) {
  if (!dev) return "";
  return `<section>
    ${withHeader ? `<h4>Дев ${scoreBadge(dev)}</h4>` : ""}
    <div class="stats">
      ${tile("Запусков", dev.launches)}
      ${tile("Миграций", `<span class="pos">${dev.migrated}</span>`)}
      ${tile("Доля миграций", pct(dev.migration_rate))}
      ${tile("Сливов", `<span class="${dev.dumped ? "neg" : ""}">${dev.dumped}</span>` + (dev.dump_rate != null ? ` <span class="muted small">${pct(dev.dump_rate)}</span>` : ""))}
      ${tile("Средний ATH", mc(dev.avg_ath_sol))}
      ${tile("Лучший ATH", mc(dev.best_ath_sol))}
    </div>
    <div class="reasons" style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">${(dev.flags || []).map((f) => `<span class="tag ${/слив|серий/.test(f) ? "r" : /миграц/.test(f) ? "g" : ""}">${esc(f)}</span>`).join("")}
      ${dev.backfilled_at ? `<span class="tag">история подтянута ${ago(dev.backfilled_at)} назад</span>` : `<span class="tag a">история только из живого потока</span>`}</div>
  </section>`;
}

async function openToken(mint) {
  openDrawer(loading);
  let d;
  try { d = await api("/api/token/" + mint); } catch (e) { return openDrawer(`<h2>Токен не найден</h2><p class="muted">${esc(e.message)}</p>`); }
  const t = d.token, dev = d.dev;
  openDrawer(`
    <div class="tk" style="gap:14px" data-drawer-mint="${t.mint}">${tokenImg(t, "img")}<div><h2>${esc(t.name || "?")} <span class="muted">$${esc(t.symbol || "")}</span></h2>
      <div class="addr">${t.mint} ${copyBtn(t.mint)}</div></div></div>
    <div class="actions"><a class="primary btn-link" href="${axiomUrl(t)}" target="_blank" rel="noopener">Открыть на Axiom ↗</a>
      ${links(t)} ${socials(t) ? `<div class="links">${socials(t)}</div>` : ""}</div>
    <section><h4>Токен</h4><div class="stats">
      ${tile("Market cap", mc(t.mcap_sol))}
      ${tile("ATH", mc(t.ath_mcap_sol))}
      ${tile("Кривая", t.completed ? '<span style="color:var(--blue)">мигрировал</span>' : pct(t.progress))}
      ${tile("Объём", sol(t.volume_sol, 1))}
      ${tile("Покупки / продажи", `${t.buys || 0} / ${t.sells || 0}`)}
      ${tile("Возраст", t.created_at ? durFmt(Date.now() / 1000 - t.created_at) : "—")}
      ${tile("Покупка дева", sol(t.dev_buy_sol))}
      ${tile("Дев держит", t.dev_dumped ? '<span class="neg">слил</span>' : t.dev_tok_in ? (t.dev_holding_pct.toFixed(1) + "%") : "—")}
    </div></section>
    ${dev ? devSection(dev) : ""}
    ${dev ? `${scamBanner(dev)}<div class="actions"><button class="ghost" data-open-dev="${dev.wallet}">Профиль дева</button>
      ${trackBtn(dev.wallet, "dev", dev.tracked, false)}
      ${scamButton(dev.wallet, dev)}</div>` : ""}
    <section><h4>Крупнейшие держатели по сделкам трекера</h4>
      ${d.wallets.length ? `<table class="grid"><thead><tr><th>Кошелёк</th><th class="r">Купил</th><th class="r">Продал</th><th class="r">Держит</th></tr></thead><tbody>
      ${d.wallets.map((w) => `<tr data-wallet="${w.wallet}"><td class="mono">${w.wallet === dev?.wallet ? '<span class="tag a">дев</span> ' : ""}${w.smart ? '<span class="tag g">умный</span> ' : ""}${w.label ? `<b>${esc(w.label)}</b> ` : ""}${short(w.wallet)}</td>
        <td class="r mono">${Number(w.sol_in).toFixed(2)}</td><td class="r mono">${Number(w.sol_out).toFixed(2)}</td>
        <td class="r mono">${((w.tok_in - w.tok_out) / 1e7).toFixed(2)}%</td></tr>`).join("")}</tbody></table>` : `<div class="muted small">Сделок пока не видно.</div>`}
    </section>`);
  bindDrawer();
}

async function openAddress(wallet) {
  openDrawer(loading);
  let d, w;
  try { [d, w] = await Promise.all([api("/api/dev/" + wallet), api("/api/wallet/" + wallet)]); }
  catch (e) { return openDrawer(`<h2>Ошибка</h2><p class="muted">${esc(e.message)}</p>`); }
  renderAddress(wallet, d, w);
}
function renderAddress(wallet, d, w) {
  const dev = d.dev;
  const pos = w.positions || [];
  openDrawer(`
    <h2>${w.label ? esc(w.label) : dev.launches ? "Дев" : "Кошелёк"} ${dev.launches ? scoreBadge(dev) : ""} ${w.smart ? '<span class="tag g">в топе</span>' : ""}</h2>
    <div class="addr">${wallet} ${copyBtn(wallet)} <a href="https://solscan.io/account/${wallet}" target="_blank" rel="noopener">solscan</a> · <a href="https://gmgn.ai/sol/address/${wallet}" target="_blank" rel="noopener">gmgn</a></div>
    ${scamBanner(dev)}
    <div class="actions">
      <button class="primary" id="btnBackfill">⟳ Подтянуть историю запусков</button>
      ${w.tracked
        ? `${trackBtn(wallet, w.kind, true, false)}<span class="tag a">в отслеживаемых как ${w.kind === "dev" ? "дев" : "трейдер"}</span>`
        : trackBtn(wallet, "dev", false, false) + trackBtn(wallet, "trader", false, false)}
      ${scamButton(wallet, dev)}
    </div>
    ${devSection(dev, false)}
    <section><h4>Запуски (${dev.launches}${dev.launches > d.tokens.length ? `, показаны последние ${d.tokens.length}` : ""})</h4>
    ${d.tokens.length ? `<table class="grid"><thead><tr><th>Токен</th><th>Когда</th><th class="r">ATH</th><th>Итог</th></tr></thead><tbody>
      ${d.tokens.map((t) => `<tr data-mint="${t.mint}"><td><div class="tk">${tokenImg(t)}<div><div class="n">${esc(t.name || short(t.mint))}</div><div class="s">$${esc(t.symbol || "")}</div></div></div></td>
        <td class="muted">${ago(t.created_at)} назад</td><td class="r mono">${mc(t.ath_mcap_sol)}</td>
        <td>${t.completed ? '<span class="tag g">миграция</span>' : t.dev_dumped ? '<span class="tag r">слил</span>' : `<div class="bar"><i style="width:${Math.round(t.progress * 100)}%"></i></div>`}</td></tr>`).join("")}
      </tbody></table>` : `<div class="muted small">Запусков не найдено. Нажми «Подтянуть историю», чтобы поискать в транзакциях кошелька.</div>`}
    </section>
    <section><h4>Сделки на pump.fun · PnL ${signed(w.pnl)}</h4>
    ${pos.length ? `<table class="grid"><thead><tr><th>Токен</th><th class="r">Вложил</th><th class="r">Вывел</th><th class="r">PnL</th><th>Когда</th></tr></thead><tbody>
      ${pos.map((p) => `<tr data-mint="${p.mint}"><td>${esc(p.name || short(p.mint))} <span class="muted small">$${esc(p.symbol || "")}</span></td>
        <td class="r mono">${Number(p.sol_in).toFixed(2)}</td><td class="r mono">${Number(p.sol_out).toFixed(2)}</td>
        <td class="r mono">${signed(p.pnl)}</td><td class="muted">${ago(p.last_at)} назад</td></tr>`).join("")}
      </tbody></table>` : `<div class="muted small">Трекер ещё не видел сделок этого кошелька.</div>`}
    </section>`);
  bindDrawer();
  $("#btnBackfill").addEventListener("click", async (e) => {
    const b = e.target;
    b.disabled = true;
    b.textContent = "Ищу все запуски… у активных девов до 1–2 минут";
    try {
      const r = await api(`/api/dev/${wallet}/backfill`, { method: "POST" });
      toast(`Найдено запусков: ${r.found}`);
      openAddress(wallet);
    } catch (err) { toast(err.message); b.disabled = false; b.textContent = "⟳ Подтянуть историю запусков"; }
  });
}
function scamBanner(dev) {
  if (!dev?.scam) return "";
  return `<div class="scam-banner"><b>🚩 Помечен как скам</b> ${dev.scam_at ? `<span class="muted small">${ago(dev.scam_at)} назад</span>` : ""}
    ${dev.scam_note ? `<div>${esc(dev.scam_note)}</div>` : ""}<div class="muted small">Токены этого дева не попадают в алерты.</div></div>`;
}
function scamButton(wallet, dev) {
  return dev?.scam
    ? `<button class="ghost" data-scam="${wallet}" data-is-scam="1">Снять пометку «скам»</button>`
    : `<button class="ghost danger" data-scam="${wallet}" data-is-scam="0">🚩 Пометить как скам</button>`;
}
function bindDrawer() {
  const body = $("#drawerBody");
  $$("[data-scam]", body).forEach((b) => b.addEventListener("click", async () => {
    try {
      const r = await toggleScam(b.dataset.scam, b.dataset.isScam === "1");
      if (r === null) return;
      const mint = body.querySelector("[data-drawer-mint]")?.dataset.drawerMint;
      if (mint) openToken(mint); else openAddress(b.dataset.scam);
      if (state.tab === "devs") loadDevs();
    } catch (e) { toast(e.message); }
  }));
  $$("[data-open-dev]", body).forEach((b) => b.addEventListener("click", () => openAddress(b.dataset.openDev)));
  $$("[data-track]", body).forEach((b) => b.addEventListener("click", async () => {
    await toggleTrack(b);
    const mint = body.querySelector("[data-drawer-mint]")?.dataset.drawerMint;
    if (mint) openToken(mint); else openAddress(b.dataset.track);
    if (state.tab === "devs") loadDevs();
    if (state.tab === "smart") loadLeaderboard();
  }));
  $$("tr[data-mint]", body).forEach((tr) => tr.addEventListener("click", () => openToken(tr.dataset.mint)));
  $$("tr[data-wallet]", body).forEach((tr) => tr.addEventListener("click", () => openAddress(tr.dataset.wallet)));
}

// ---------------------------------------------------------------- search
$("#search").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#searchInput").value.trim();
  if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(q)) return toast("Вставь адрес Solana");
  $("#searchInput").blur();
  try { await api("/api/token/" + q); openToken(q); }
  catch { openAddress(q); }
});

// ---------------------------------------------------------------- stream
function connectStream() {
  const es = new EventSource("/api/stream");
  es.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "alert") pushAlert(m);
    if (m.type === "token") {
      state.live.set(m.token.mint, { token: m.token, dev: m.dev });
      if (state.tab === "live") {
        const onlyKnown = $("#liveOnlyKnown").checked;
        if (!onlyKnown || (m.dev && m.dev.launches > 0)) {
          const tb = $("#liveTable tbody");
          if (tb.querySelector(".empty")) tb.innerHTML = "";
          tb.prepend(liveRow({ token: m.token, dev: m.dev }, true));
          while (tb.children.length > 150) tb.lastChild.remove();
        }
      }
    }
  };
}

// ---------------------------------------------------------------- init
(async function init() {
  renderChips();
  syncToggles();
  await loadStatus();
  setInterval(loadStatus, 5000);
  try { state.alerts = await api("/api/alerts?limit=200"); } catch { state.alerts = []; }
  renderFeed();
  connectStream();
  showTab(store.get("tab", "feed"));
})();
