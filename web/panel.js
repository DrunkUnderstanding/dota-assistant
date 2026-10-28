// 控制面板：把 Python 推来的快照画出来，把操作发回去。局面以 Python 那边为准，这里不存状态。
import { act, connect, getJSON } from './api.js';
import { applyOverlayStyle, bindOverlayClicks, esc, portrait, renderOverlay } from './view.js';

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const SIDES = ['radiant', 'dire'];
const SIDE_NAME = { radiant: '天辉', dire: '夜魇' };

let state = null;
let heroes = [];
let lastMessage = '';

// ---------- 格子 ----------

for (const side of SIDES) {
  const box = $(`.team[data-side="${side}"] .slots`);
  for (let i = 0; i < 5; i++) {
    box.insertAdjacentHTML('beforeend', `
      <div class="slot" data-i="${i}">
        <div class="slot-art"><span class="slot-empty">空位</span></div>
        <input type="text" autocomplete="off" spellcheck="false" placeholder="第 ${i + 1} 个">
      </div>`);
    const input = $$('.slot input', box)[i];
    input.dataset.side = side;
    input.dataset.index = i;
    attachAutocomplete(input, (text) => commitSlot(side, i, text));
  }
}

function commitSlot(side, i, text) {
  const cur = state?.slots[side][i].text ?? '';
  if (text.trim() === cur) return;
  run('set_slot', { side, index: i, text });
}

const bans = $('#bans');
attachAutocomplete(bans, (text) => {
  if (text !== (state?.bans_text ?? '')) run('set_bans', { text });
}, true);

function paintSlots(s) {
  // 不知道阵营时（GSI 没连上）推荐先按 side_assumed 分友敌，标签带问号提醒去下面「阵营」手动选
  const v = s.overlay.view;
  const mine = v?.side || v?.side_assumed;
  const q = v?.side ? '' : '？';
  for (const side of SIDES) {
    const team = $(`.team[data-side="${side}"]`);
    team.classList.toggle('is-mine', mine === side);
    $('.team-tag', team).textContent = mine ? (mine === side ? '我方' : '对方') + q : '';
    $('.team-tag', team).title = q ? '不知道你在哪边，先当天辉算；在夜魇就到下面「阵营」选夜魇' : '';
    $$('.slot', team).forEach((el, i) => {
      const slot = s.slots[side][i];
      const input = $('input', el);
      if (document.activeElement !== input) input.value = input._sent = slot.text;   // 正在改的格子不覆盖
      input.placeholder = slot.guess ? `? ${slot.guess}` : `第 ${i + 1} 个`;
      el.classList.toggle('is-bad', !slot.valid);
      el.classList.toggle('has-hero', !!slot.hero_id);
      const art = $('.slot-art', el);
      const key = `${slot.hero_id || ''}|${slot.score ?? ''}|${slot.valid}`;
      if (art.dataset.key === key) return;
      art.dataset.key = key;
      const score = slot.score != null ? `<span class="slot-score">${slot.score.toFixed(2)}</span>` : '';
      art.innerHTML = slot.hero_id
        ? `<img src="${portrait(slot.hero_id)}" alt="">${score}`
        : `<span>${slot.valid ? '空位' : '名字不对'}</span>${score}`;
      art.title = slot.score != null ? `相似度 ${slot.score.toFixed(2)}` : '';
    });
  }
  if (document.activeElement !== bans) bans.value = bans._sent = s.bans_text;
}

// ---------- 队友战绩 ----------

$('#mate-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('#mate-code');
  if (!input.value.trim()) return input.focus();
  await run('mate_query', { code: input.value });
  input.value = '';
});
$('#mates').addEventListener('click', (e) => {
  const b = e.target.closest('[data-mate]');
  if (!b) return;
  const id = Number(b.dataset.mate);
  if (b.dataset.do === 'remove') run('mate_remove', { account_id: id });
  else run('mate_query', { code: String(id), force: true });
});

const pct = (w, g) => (g ? Math.round((w / g) * 100) : 0);
// 胜率上色：样本太少不上色
const rateCls = (w, g) => (g < 5 ? '' : pct(w, g) >= 55 ? 'is-good' : pct(w, g) <= 45 ? 'is-bad' : '');
const rate = (w, g) => `<span class="mate-rate ${rateCls(w, g)}">${pct(w, g)}%</span>`;

function ago(t) {
  const m = Math.floor((Date.now() / 1000 - t) / 60);
  if (m < 1) return '刚刚';
  if (m < 60) return `${m} 分钟前`;
  if (m < 60 * 24) return `${Math.floor(m / 60)} 小时前`;
  return `${Math.floor(m / 60 / 24)} 天前`;
}

function mateCard(m) {
  const btns = `
    <button class="btn btn-small" data-mate="${m.account_id}" ${m.busy ? 'disabled' : ''}>${m.busy ? '查询中…' : '刷新'}</button>
    <button class="btn btn-small btn-ghost" data-mate="${m.account_id}" data-do="remove" title="不再记这个人">×</button>`;
  const name = esc(m.name || `好友代码 ${m.account_id}`);
  const when = m.fetched_at ? `<span class="mate-time" title="数据是这时候查的">${ago(m.fetched_at)}查的</span>` : '';
  const err = m.error ? `<div class="mate-err">${esc(m.error)}</div>` : '';
  if (!m.loaded) {
    return `<div class="mate"><div class="mate-head"><b class="mate-name">${name}</b><span class="spacer"></span>${btns}</div>
      ${err || (m.busy ? '<div class="mate-note">正在查…</div>' : '')}</div>`;
  }
  const withMe = m.with_me?.[0]
    ? `<span class="mate-with" title="OpenDota 记录里你们在同一边的全部对局">和你一起 ${m.with_me[0]} 局 · ${rate(m.with_me[1], m.with_me[0])}</span>` : '';
  const head = `<div class="mate-head"><b class="mate-name" title="好友代码 ${m.account_id}">${name}</b>
    <span class="mate-rank">${esc(m.rank)}</span>${withMe}<span class="spacer"></span>${when}${btns}</div>`;
  if (m.private) {
    return `<div class="mate">${head}${err}<div class="mate-note">查不到比赛记录：对方没在游戏里公开比赛数据，或者还没打过</div></div>`;
  }
  const w = m.matches.filter((x) => x.win).length;
  const recent = m.matches.map((x) => `<span class="mate-game ${x.win ? 'is-win' : 'is-lose'}"
      title="${esc(x.name)} · ${x.win ? '胜' : '负'} · ${x.kda.join('/')} · ${x.ranked ? '天梯 · ' : ''}${ago(x.t)}"><img src="${portrait(x.hero_id)}" alt="" onerror="this.remove()"></span>`).join('');
  const heroes = m.heroes.map((h) => `<span class="mate-hero" title="${esc(h.name)}：${h.games} 局 ${h.wins} 胜">
      <img src="${portrait(h.id)}" alt="" onerror="this.remove()"><span>${h.games} 局 ${rate(h.wins, h.games)}</span></span>`).join('');
  const posGames = (m.positions || []).reduce((n, [, g]) => n + g, 0);   // 占比按分出了位置的局算
  const pos = m.positions?.length
    ? m.positions.map(([p, g, wins]) => `<span class="mate-pos" title="${g} 局 ${wins} 胜"><b>${p} 号位</b> ${pct(g, posGames)}% · 胜 ${rate(wins, g)}</span>`).join('')
    : `<span class="mate-note">${esc(m.positions_note || '没有位置数据')}</span>`;
  return `<div class="mate">${head}${err}
    <div class="mate-row"><span class="mate-label">最近 ${m.matches.length} 局<b class="mate-wl">${w} 胜 ${m.matches.length - w} 负</b></span><div class="mate-recent">${recent}</div></div>
    <div class="mate-row"><span class="mate-label" title="最近 ${m.sample} 局里玩得最多的">常玩</span><div class="mate-heroes">${heroes}</div></div>
    <div class="mate-row"><span class="mate-label" title="最近 ${m.positions_sample || 100} 局天梯（STRATZ），其中 ${posGames} 局分出了位置">位置</span><div class="mate-poss">${pos}</div></div>
  </div>`;
}

function paintMates(list) {
  const box = $('#mates');
  const key = JSON.stringify(list) + Math.floor(Date.now() / 60000);   // 每分钟更新一次「几分钟前」
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.innerHTML = list.length ? list.map(mateCard).join('')
    : '<div class="mate-empty">还没查过谁。好友代码在 Steam 好友列表「添加好友」那页、或者他的 Dota 个人资料里能看到</div>';
}
setInterval(() => { if (state) paintMates(state.mates); }, 60000);

// ---------- 设置 ----------

function bindSeg(id, key, cast = (v) => v) {
  $(`#${id}`).addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (b) run('set', { key, value: cast(b.dataset.v) });
  });
}
bindSeg('seg-position', 'position', Number);
bindSeg('seg-side', 'side_mode');
bindSeg('seg-mode', 'mode');
$('#opt-voice').addEventListener('change', (e) => run('set', { key: 'voice', value: e.target.checked }));
$('#opt-scan').addEventListener('change', (e) => run('set', { key: 'scan_without_gsi', value: e.target.checked }));

function paintSettings(st) {
  const mark = (id, v) => $$(`#${id} button`).forEach((b) => b.classList.toggle('on', b.dataset.v === String(v)));
  mark('seg-position', st.position);
  mark('seg-side', st.side_mode);
  mark('seg-mode', st.mode);
  $('#opt-voice').checked = st.voice;
  $('#opt-scan').checked = st.scan_without_gsi;
}

// ---------- 设置窗口（账号、AI 来源、热键） ----------
// 密钥不从 Python 发过来，只知道设没设、末 4 位；框里留空 = 不改

const dlg = $('#settings');
const form = $('#settings-form');
const PROVIDER_HINT = {
  claude_code: '用这台电脑上的 Claude Code。Key 和地址都留空 = 用它自己登录的账号；用中转站（例如 DuckCoding 的 Claude Code 分组）就填中转站给的 Key 和地址。',
  anthropic: '自己的 Anthropic API Key（console.anthropic.com），按用量付费，地址留空；支持直接 API 调用的中转站也可以填它的地址。',
  openai: 'OpenAI 兼容接口：DeepSeek、通义千问、Kimi 等。填那家的接口地址、Key 和模型名。不是 Claude，打法质量没验证过。',
};
const DEFAULT_MODEL = { claude_code: 'claude-opus-5-5', anthropic: 'claude-opus-5-5', openai: '' };

function secretPlaceholder(m, empty) {
  return m.set ? `已设置${m.tail ? `（…${m.tail}）` : ''}，留空不改` : empty;
}

const URL_PLACEHOLDER = {
  claude_code: '留空；中转站填它给的地址，例如 https://api.duckcoding.ai',
  anthropic: '留空 = Anthropic 官方',
  openai: '例如 https://api.deepseek.com/v1',
};

function showProvider() {
  const p = form.ai_provider.value;
  form.ai_base_url.placeholder = URL_PLACEHOLDER[p] || '';
  if (!form.dataset.keySet) form.ai_api_key.placeholder = p === 'claude_code' ? '留空 = 用 Claude Code 自己的登录' : '必填';
  $$('[data-for]', form).forEach((el) => { el.hidden = !el.dataset.for.split(' ').includes(p); });
  $('#provider-hint').textContent = PROVIDER_HINT[p] || '';
}

function openSettings() {
  const c = state?.config;
  if (!c) return;
  form.account_id.value = c.account_id || '';
  form.stratz_token.value = '';
  form.stratz_token.placeholder = secretPlaceholder(c.stratz_token, '没有可以不填');
  form.ai_plan.checked = c.ai_plan;
  form.ai_provider.innerHTML = Object.entries(c.providers).map(([k, v]) => `<option value="${k}">${v}</option>`).join('');
  form.ai_provider.value = c.ai_provider;
  form.ai_api_key.value = '';
  form.ai_api_key.placeholder = secretPlaceholder(c.ai_api_key, '必填');
  form.dataset.keySet = c.ai_api_key.set ? '1' : '';
  form.ai_base_url.value = c.ai_base_url;
  form.ai_model.value = c.ai_model;
  form.dota_dir.value = c.dota_dir;
  form.dota_dir.placeholder = c.dota_found ? `留空 = 自动找（现在用的：${c.dota_found}）` : '没自动找到，填 dota 2 beta 那个文件夹';
  form.dataset.provider = c.ai_provider;
  showProvider();
  paintHotkeyForm(c.hotkeys);
  dlg.showModal();
}

// 热键：只给 F1~F12 选。单个字母虽然能注册，但会把 Dota 里这个键整个吃掉
const KEY_CHOICES = Array.from({ length: 12 }, (_, i) => `F${i + 1}`);

function paintHotkeyForm(list) {
  $('#hotkeys').innerHTML = list.map((h) => {
    const opts = KEY_CHOICES.includes(h.key) ? KEY_CHOICES : [h.key, ...KEY_CHOICES];   // config 里手写了别的键也留着
    return `<label class="row"><span>${h.text}</span>
      <select data-hotkey="${h.field}">${opts.map((k) => `<option${k === h.key ? ' selected' : ''}>${k}</option>`).join('')}</select>
    </label>`;
  }).join('');
  markDupKeys();
}

function markDupKeys() {
  const sels = $$('#hotkeys select');
  const count = {};
  sels.forEach((x) => { count[x.value] = (count[x.value] || 0) + 1; });
  sels.forEach((x) => x.classList.toggle('is-dup', count[x.value] > 1));
  return sels.some((x) => count[x.value] > 1);
}

$('#hotkeys').addEventListener('change', markDupKeys);

form.ai_provider.addEventListener('change', () => {
  // 换来源时，模型名还是上一家的默认值就跟着换（换到 OpenAI 兼容接口要自己填）
  const prev = form.dataset.provider;
  const next = form.ai_provider.value;
  if (!form.ai_model.value.trim() || form.ai_model.value === DEFAULT_MODEL[prev]) form.ai_model.value = DEFAULT_MODEL[next];
  form.dataset.provider = next;
  showProvider();
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const payload = {
    account_id: form.account_id.value.trim(),
    ai_plan: form.ai_plan.checked,
    ai_provider: form.ai_provider.value,
    ai_model: form.ai_model.value.trim(),
    ai_base_url: form.ai_base_url.value.trim(),
    dota_dir: form.dota_dir.value.trim(),
  };
  // 有输入才发：只输了空白 = 清掉，什么都没输 = 不改
  if (form.stratz_token.value) payload.stratz_token = form.stratz_token.value.trim();
  if (form.ai_api_key.value) payload.ai_api_key = form.ai_api_key.value.trim();
  if (markDupKeys()) { showMessage('有两个功能绑了同一个键，换一个再保存', true); return; }
  payload.hotkeys = Object.fromEntries($$('#hotkeys select').map((x) => [x.dataset.hotkey, x.value]));
  const res = await act('save_settings', payload);
  if (!res.ok) { showMessage(res.message, true); return; }
  showMessage(res.message);
  dlg.close();
});

$('#settings-cancel').addEventListener('click', () => dlg.close());
$('#btn-settings').addEventListener('click', openSettings);
// 点顶栏的快捷键直接跳到设置里的热键
$('#status').addEventListener('click', (e) => {
  if (e.target.closest('.chip-dota')) {         // 没找到 Dota 目录：点状态直接去填
    openSettings();
    $('#dota-head').scrollIntoView({ block: 'start' });
    form.dota_dir.focus();
    return;
  }
  if (!e.target.closest('.chip-key')) return;
  openSettings();
  $('#hotkeys-head').scrollIntoView({ block: 'start' });
});

// 第一次打开、什么都没填时主动弹一次（只弹一次，之后点右上角「设置」）
let firstRunChecked = false;
function maybeFirstRun(c) {
  if (firstRunChecked || !c) return;
  firstRunChecked = true;
  let seen = false;
  try { seen = localStorage.getItem('settings_seen') === '1'; localStorage.setItem('settings_seen', '1'); } catch { /* 存不了就每次都看情况 */ }
  if (!seen && !c.account_id && !c.stratz_token.set) openSettings();
}

// ---------- 状态 / 按钮 ----------

$$('[data-act]').forEach((b) => b.addEventListener('click', () => run(b.dataset.act)));

function paintStatus(s) {
  const st = s.status;
  const gsiDot = { ok: 'dot-ok', off: '', disabled: '', busy: 'dot-bad', nodota: 'dot-bad' }[st.gsi.state];
  const keys = st.hotkeys.map((h) => {
    const failed = st.hotkeys_failed.includes(h.key);
    const tip = `${h.key} ${h.text}${failed ? '（注册失败，被别的程序占用）' : ''}，点这里改键`;
    return `<span class="chip chip-key ${failed ? 'chip-fail' : ''}" title="${tip}"><kbd>${h.key}</kbd><span class="key-full">${h.text}</span><span class="key-short">${h.short || h.text}</span></span>`;
  }).join('');
  $('#status').innerHTML = `
    <span class="chip ${st.gsi.state === 'nodota' ? 'chip-dota' : ''}" title="${st.gsi.tip || ''}"><i class="dot ${gsiDot}"></i>${st.gsi.text}</span>
    <span class="chip"><i class="dot ${st.calibrated ? 'dot-ok' : 'dot-warn'}"></i>${st.calibrated ? '已校准' : '未校准'}</span>
    <span class="chip" title="${st.last_scan ? `上次识别 ${st.last_scan}` : '还没识别过'}">${st.last_scan ? `<span class="chip-text">上次识别 </span>${st.last_scan}` : '未识别'}</span>
    <span class="status-sep"></span>
    ${keys}`;
  $('#btn-toggle').textContent = s.overlay.visible ? '隐藏叠加层' : '显示叠加层';
  if (st.message && st.message !== lastMessage) showMessage(st.message);
  lastMessage = st.message;
}

function showMessage(text, error = false) {
  const f = $('#footer');
  f.textContent = text;
  f.classList.toggle('is-error', error);
}

async function run(type, payload) {
  const res = await act(type, payload);
  if (!res.ok) showMessage(res.message, true);
  else if (res.message) showMessage(res.message);
}

// ---------- 英雄名自动补全 ----------

const ac = $('#ac');
let acState = null;   // { input, items, on, onCommit, multi }

function tokenOf(input, multi) {
  if (!multi) return input.value.trim();
  const parts = input.value.split(/[,，]/);
  return parts[parts.length - 1].trim();
}

function match(q) {
  if (!q) return [];
  const lq = q.toLowerCase();
  const scored = [];
  for (const h of heroes) {
    const i = h.name.indexOf(q);
    const j = h.en.toLowerCase().indexOf(lq);
    if (i < 0 && j < 0) continue;
    scored.push([i === 0 || j === 0 ? 0 : 1, h]);
  }
  return scored.sort((a, b) => a[0] - b[0]).slice(0, 8).map((x) => x[1]);
}

function openAc(input, onCommit, multi) {
  const items = match(tokenOf(input, multi));
  if (!items.length) return closeAc();
  acState = { input, items, on: 0, onCommit, multi };
  ac.innerHTML = items.map((h, k) => `
    <div class="ac-item ${k === 0 ? 'on' : ''}" data-k="${k}"><img src="${portrait(h.id)}" alt="">${h.name}<small>${h.en}</small></div>`).join('');
  const r = input.getBoundingClientRect();
  ac.style.left = `${r.left}px`;
  ac.style.top = `${r.bottom + 4}px`;
  ac.style.width = `${Math.max(200, multi ? 260 : r.width + 40)}px`;
  ac.hidden = false;
}

function closeAc() {
  acState = null;
  ac.hidden = true;
}

function pick(k) {
  const { input, items, onCommit, multi } = acState;
  const name = items[k].name;
  if (multi) {
    const parts = input.value.split(/[,，]/).map((p) => p.trim());
    parts[parts.length - 1] = name;
    input.value = parts.filter(Boolean).join('，') + '，';
  } else {
    input.value = name;
  }
  closeAc();
  onCommit(multi ? input.value.replace(/[，,]\s*$/, '') : input.value);
  if (!multi) focusNext(input);
}

function focusNext(input) {
  const all = $$('.slot input');
  const k = all.indexOf(input);
  if (k >= 0 && k + 1 < all.length) all[k + 1].focus();
  else input.blur();
}

function attachAutocomplete(input, commit, multi = false) {
  // 选补全项、回车、失焦都会提交，同一个值只发一次（paint 写回服务器的值时也会更新 _sent）
  const onCommit = (text) => {
    if (text === input._sent) return;
    input._sent = text;
    commit(text);
  };
  input.addEventListener('input', () => openAc(input, onCommit, multi));
  input.addEventListener('keydown', (e) => {
    if (acState?.input === input) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const n = acState.items.length;
        acState.on = (acState.on + (e.key === 'ArrowDown' ? 1 : n - 1)) % n;
        $$('.ac-item', ac).forEach((el, k) => el.classList.toggle('on', k === acState.on));
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        if (tokenOf(input, multi)) { e.preventDefault(); pick(acState.on); return; }
      }
      if (e.key === 'Escape') { closeAc(); return; }
    }
    if (e.key === 'Enter') { onCommit(input.value); if (!multi) focusNext(input); }
  });
  input.addEventListener('blur', () => {
    setTimeout(() => {               // 让点击补全项先生效
      if (acState?.input === input) closeAc();
      onCommit(input.value);
    }, 120);
  });
}

ac.addEventListener('mousedown', (e) => {
  const item = e.target.closest('.ac-item');
  if (item && acState) { e.preventDefault(); pick(Number(item.dataset.k)); }
});

// 右侧预览和叠加层一样能点：推荐英雄 → 选它怎么打，标签 → 翻页，× → 关卡片
bindOverlayClicks($('#preview'), run);

// ---------- 叠加层外观：拖的时候只改内存（游戏里的叠加层马上跟着变），松手 / 点色块才写 config.toml ----------

const OV_COLORS = [
  ['#0a0d12', '默认（深蓝黑）'], ['#000000', '纯黑'], ['#1d2025', '深灰'],
  ['#0b1a2e', '深蓝'], ['#0d1f16', '深绿'], ['#22100e', '深红'], ['#1a1226', '深紫'],
];
$('#ov-swatches').innerHTML = OV_COLORS.map(([c, name]) =>
  `<button type="button" class="swatch" data-c="${c}" style="--c:${c}" title="${name}"></button>`).join('');

// 每个设置项只留最新的一次，前一次没回来就不发，免得拖滑块时把工作线程塞满
const styleQueue = new Map();
let styleBusy = false;
async function setStyle(key, value, save) {
  styleQueue.set(key, { value, save: save || styleQueue.get(key)?.save });
  if (styleBusy) return;
  styleBusy = true;
  while (styleQueue.size) {
    const [k, v] = styleQueue.entries().next().value;
    styleQueue.delete(k);
    const res = await act('set', { key: k, value: v.value, save: !!v.save });
    if (!res.ok) showMessage(res.message, true);
    else if (v.save) showMessage(res.message);
  }
  styleBusy = false;
}

const opacityInput = $('#ov-opacity');
const colorInput = $('#ov-color');
const showOpacity = (v) => { $('#ov-opacity-val').textContent = `${Math.round(v * 100)}%`; };
opacityInput.addEventListener('input', () => { showOpacity(opacityInput.value); setStyle('overlay_opacity', Number(opacityInput.value), false); });
opacityInput.addEventListener('change', () => setStyle('overlay_opacity', Number(opacityInput.value), true));
colorInput.addEventListener('input', () => setStyle('overlay_color', colorInput.value, false));
colorInput.addEventListener('change', () => setStyle('overlay_color', colorInput.value, true));
$('#ov-swatches').addEventListener('click', (e) => {
  const b = e.target.closest('.swatch');
  if (b) setStyle('overlay_color', b.dataset.c, true);
});

function paintOverlayStyle(style) {
  if (!style) return;
  applyOverlayStyle($('#preview'), style);
  if (document.activeElement !== opacityInput) { opacityInput.value = style.opacity; showOpacity(style.opacity); }
  if (document.activeElement !== colorInput) colorInput.value = style.color;
  $$('#ov-swatches .swatch').forEach((b) => b.classList.toggle('on', b.dataset.c === style.color));
  $('.swatch-custom').classList.toggle('on', !OV_COLORS.some(([c]) => c === style.color));
}

// ---------- 启动 ----------

getJSON('/api/heroes').then((h) => { heroes = h; });

connect((s) => {
  state = s;
  maybeFirstRun(s.config);
  paintSettings(s.settings);
  paintSlots(s);
  paintMates(s.mates);
  paintStatus(s);
  renderOverlay($('#preview'), s.overlay.view);
  $('#preview').classList.toggle('is-hidden', !s.overlay.visible);
  paintOverlayStyle(s.overlay.style);
}, (ok) => {
  if (!ok) showMessage('和助手断开了（python 那边退出了？），正在重连…', true);
  else if ($('#footer').classList.contains('is-error')) showMessage('已重新连上助手');
});
