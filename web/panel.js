// 控制面板：把 Python 推来的快照画出来，把操作发回去。局面以 Python 那边为准，这里不存状态。
import { act, connect, getJSON } from './api.js';
import { portrait, renderOverlay } from './view.js';

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
  const mine = s.overlay.view?.side;
  for (const side of SIDES) {
    const team = $(`.team[data-side="${side}"]`);
    team.classList.toggle('is-mine', mine === side);
    $('.team-tag', team).textContent = mine ? (mine === side ? '我方' : '对方') : '';
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

// ---------- 状态 / 按钮 ----------

$$('[data-act]').forEach((b) => b.addEventListener('click', () => run(b.dataset.act)));

function paintStatus(s) {
  const st = s.status;
  const gsiDot = { ok: 'dot-ok', off: '', disabled: '', busy: 'dot-bad' }[st.gsi.state];
  const keys = st.hotkeys.map((h) => {
    const failed = st.hotkeys_failed.includes(h.key);
    return `<span class="chip ${failed ? 'chip-fail' : ''}" title="${failed ? '注册失败，被别的程序占用' : ''}"><kbd>${h.key}</kbd>${h.text}</span>`;
  }).join('');
  $('#status').innerHTML = `
    <span class="chip"><i class="dot ${gsiDot}"></i>${st.gsi.text}</span>
    <span class="chip"><i class="dot ${st.calibrated ? 'dot-ok' : 'dot-warn'}"></i>${st.calibrated ? '已校准' : '未校准'}</span>
    <span class="chip">${st.last_scan ? `上次识别 ${st.last_scan}` : '未识别'}</span>
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

// 右侧预览里的局内提醒也能直接关
$('#preview').addEventListener('click', (e) => { if (e.target.closest('.ov-close')) act('dismiss_coach'); });

// ---------- 启动 ----------

getJSON('/api/heroes').then((h) => { heroes = h; });

connect((s) => {
  state = s;
  paintSettings(s.settings);
  paintSlots(s);
  paintStatus(s);
  renderOverlay($('#preview'), s.overlay.view);
  $('#preview').classList.toggle('is-hidden', !s.overlay.visible);
}, (ok) => {
  if (!ok) showMessage('和助手断开了（python 那边退出了？），正在重连…', true);
  else if ($('#footer').classList.contains('is-error')) showMessage('已重新连上助手');
});
