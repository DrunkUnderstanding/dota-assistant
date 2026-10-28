// 队友战绩（独立页）：左边查过的人，右边一个人的简况 + 最近 20 局（每局 KDA、表现分），点一局看那一整局 10 个人。
// 地址栏 #p=好友代码 是看谁，#p=…&m=比赛编号 是看哪一局；浏览器 / 窗口的后退能回到上一页。数据都在 Python 那边（/api/mates、/api/match）。
import { getJSON } from './api.js';
import { esc, portrait } from './view.js';

const $ = (sel, el = document) => el.querySelector(sel);

let mates = [];
const pending = new Set();        // 正在查的好友代码
const autoTried = new Set();      // 自动重查过的（旧版数据），一个人只自动查一次
const matchCache = new Map();     // 比赛编号 → 整局数据（打完的比赛不会变）

// ---------- 小工具 ----------

const pct = (w, g) => (g ? Math.round((w / g) * 100) : 0);
// 胜率上色：样本太少不上色
const rateCls = (w, g) => (g < 5 ? '' : pct(w, g) >= 55 ? 'is-good' : pct(w, g) <= 45 ? 'is-bad' : '');
const rate = (w, g) => `<span class="mt-rate ${rateCls(w, g)}">${pct(w, g)}%</span>`;
const dur = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
const num = (n) => (n == null ? '-' : n >= 10000 ? `${(n / 1000).toFixed(1)}k` : String(n));

function ago(t) {
  const m = Math.floor((Date.now() / 1000 - t) / 60);
  if (m < 1) return '刚刚';
  if (m < 60) return `${m} 分钟前`;
  if (m < 60 * 24) return `${Math.floor(m / 60)} 小时前`;
  return `${Math.floor(m / 60 / 24)} 天前`;
}

function when(t) {
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 表现分：和同英雄所有对局比，0~100；悬停看每一项排百分之几
function perf(p) {
  if (!p) return '<span class="mt-faint" title="这个英雄没有分位数据">-</span>';
  const cls = p.score >= 65 ? 'is-good' : p.score <= 35 ? 'is-bad' : '';
  const tip = `和同英雄所有对局比（0~100，50 = 一般）\n${p.parts.map(([n, v]) => `${n}：比 ${v}% 的局好`).join('\n')}`;
  return `<span class="mt-perf ${cls}" title="${esc(tip)}"><b>${p.score}</b><i style="--w:${p.score}%"></i></span>`;
}

// STRATZ 评分（可选：填了 token、STRATZ 也解析了那局才有）：imp 0 = 平均，加奖项
const STRATZ_TIP = 'STRATZ 的表现评分：0 = 平均，正数好于平均（填了 STRATZ token、它也解析了这局才有）';
function stratz(s) {
  if (!s) return '<span class="mt-faint" title="STRATZ 还没解析这局">-</span>';
  const cls = s.imp >= 10 ? 'is-good' : s.imp <= -10 ? 'is-bad' : '';
  const tip = STRATZ_TIP + (s.pos ? `
STRATZ 判他这局打 ${s.pos} 号位` : '');
  return `<span class="mt-sz ${cls}" title="${esc(tip)}">${s.imp > 0 ? '+' : ''}${s.imp}</span>${s.award ? `<span class="mt-award">${esc(s.award)}</span>` : ''}`;
}
const szHead = (on) => (on ? `<th title="${STRATZ_TIP}">STRATZ</th>` : '');

const kda = ([k, d, a]) => `<span class="mt-kda"><b>${k}</b>/<b class="mt-d">${d}</b>/<b>${a}</b></span>`;

function showMessage(text, error = false) {
  const f = $('#footer');
  f.textContent = text;
  f.classList.toggle('is-error', error);
}

// ---------- 地址栏 ----------

function route() {
  const q = new URLSearchParams(location.hash.slice(1));
  return { p: Number(q.get('p')) || null, m: q.get('m') || null };
}

function go(p, m = null) {
  location.hash = m ? `p=${p}&m=${m}` : p ? `p=${p}` : '';
}

// ---------- 和 Python 通信 ----------

async function op(body) {
  try {
    const res = await fetch('/api/mates', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    return await res.json();
  } catch (e) {
    return { ok: false, message: `连不上助手：${e.message}` };
  }
}

async function query(code, force = false) {
  const guess = Number(String(code).trim());
  if (guess) pending.add(guess);
  render();
  showMessage(`正在查 ${code} …`);
  const res = await op({ op: 'query', code, force });
  if (guess) pending.delete(guess);
  if (res.mates) mates = res.mates;
  showMessage(res.message || (res.ok ? '查好了' : '没查到'), !res.ok);
  if (res.account_id) go(res.account_id);
  render();
}

// ---------- 左边：查过的人 ----------

function paintPeople() {
  const cur = route().p;
  $('#people').innerHTML = mates.length ? mates.map((m) => {
    const w = m.matches?.filter((x) => x.win).length ?? 0;
    const sub = pending.has(m.account_id) ? '查询中…'
      : m.error && !m.loaded ? esc(m.error)
        : m.private ? '没公开比赛数据'
          : m.loaded ? `${esc(m.rank)} · 近 ${m.matches.length} 局 ${w} 胜` : '';
    return `<div class="mt-person ${m.account_id === cur ? 'on' : ''}" data-p="${m.account_id}">
      <div class="mt-person-text"><b>${esc(m.name || `好友代码 ${m.account_id}`)}</b><small>${sub}</small></div>
      <button class="mt-x" data-remove="${m.account_id}" title="不再记这个人">×</button></div>`;
  }).join('') : '<div class="mt-faint">还没查过谁。好友代码在 Steam 好友列表「添加好友」那页、或者他的 Dota 个人资料里能看到</div>';
}

$('#people').addEventListener('click', async (e) => {
  const x = e.target.closest('[data-remove]');
  if (x) {
    const id = Number(x.dataset.remove);
    const res = await op({ op: 'remove', account_id: id });
    if (res.mates) mates = res.mates;
    if (route().p === id) go(mates[0]?.account_id);
    render();
    return;
  }
  const row = e.target.closest('[data-p]');
  if (row) go(Number(row.dataset.p));
});

// ---------- 右边：一个人的简况 + 最近的局 ----------

function overview(m) {
  if (!m) return '<div class="mt-blank">在上面填好友代码查一个人</div>';
  const busy = pending.has(m.account_id);
  const refresh = `<button class="btn btn-small" data-refresh="${m.account_id}" ${busy ? 'disabled' : ''}>${busy ? '查询中…' : '刷新'}</button>`;
  const name = esc(m.name || `好友代码 ${m.account_id}`);
  if (!m.loaded) {
    return `<div class="card"><div class="mt-head"><h1>${name}</h1><span class="spacer"></span>${refresh}</div>
      ${m.error ? `<div class="mt-err">${esc(m.error)}</div>` : '<div class="mt-faint">正在查…</div>'}</div>`;
  }
  const withMe = m.with_me?.[0]
    ? `<span class="mt-tag" title="OpenDota 记录里你们在同一边的全部对局">和你一起 ${m.with_me[0]} 局 · ${rate(m.with_me[1], m.with_me[0])}</span>` : '';
  const head = `<div class="mt-head"><h1 title="好友代码 ${m.account_id}">${name}</h1><span class="mt-rank">${esc(m.rank)}</span>${withMe}
    <span class="spacer"></span><span class="mt-faint" title="数据是这时候查的">${ago(m.fetched_at)}查的</span>${refresh}</div>
    ${m.error ? `<div class="mt-err">刷新没成功，下面是上次的数据：${esc(m.error)}</div>` : ''}`;
  if (m.private) {
    return `<div class="card">${head}<div class="mt-faint">查不到比赛记录：对方没在游戏里公开比赛数据，或者还没打过</div></div>`;
  }
  const n = m.matches.length;
  const w = m.matches.filter((x) => x.win).length;
  const avg = (f) => (n ? m.matches.reduce((s, x) => s + f(x), 0) / n : 0);
  const scored = m.matches.filter((x) => x.perf);
  const avgPerf = scored.length ? Math.round(scored.reduce((s, x) => s + x.perf.score, 0) / scored.length) : null;
  const heroes = m.heroes.map((h) => `<span class="mt-hero" title="${esc(h.name)}：${h.games} 局 ${h.wins} 胜">
      <img src="${portrait(h.id)}" alt="" onerror="this.remove()"><span>${h.games} 局 ${rate(h.wins, h.games)}</span></span>`).join('');
  const posGames = (m.positions || []).reduce((s, [, g]) => s + g, 0);   // 占比按分出了位置的局算
  const pos = m.positions?.length
    ? m.positions.map(([p, g, wins]) => `<span class="mt-pos" title="${g} 局 ${wins} 胜"><b>${p} 号位</b> ${pct(g, posGames)}% · 胜 ${rate(wins, g)}</span>`).join('')
    : `<span class="mt-faint">${esc(m.positions_note || '没有位置数据')}</span>`;
  const stats = `<div class="mt-stats">
      <div><small>最近 ${n} 局</small><b>${w} 胜 ${n - w} 负</b><span>${rate(w, n)}</span></div>
      <div><small>平均 KDA</small><b>${avg((x) => x.kda[0]).toFixed(1)} / ${avg((x) => x.kda[1]).toFixed(1)} / ${avg((x) => x.kda[2]).toFixed(1)}</b></div>
      <div title="每局和同英雄所有对局比的分数取平均（0~100，50 = 一般）"><small>平均表现</small><b>${avgPerf ?? '-'}</b></div>
    </div>
    <div class="mt-line"><span class="mt-label" title="最近 ${m.sample} 局里玩得最多的">常玩</span><div class="mt-heroes">${heroes}</div></div>
    <div class="mt-line"><span class="mt-label" title="最近 ${m.positions_sample || 100} 局天梯（STRATZ），其中 ${posGames} 局分出了位置">位置</span><div class="mt-poss">${pos}</div></div>`;
  const sz = m.matches.some((x) => x.stratz);   // 一局都没有就不出这一列（没填 token / STRATZ 都没解析）
  const rows = m.matches.map((x) => `
    <tr class="mt-row ${x.win ? 'is-win' : 'is-lose'}" data-m="${x.match_id}" title="点开看这一整局">
      <td><div class="mt-herocell"><img src="${portrait(x.hero_id)}" alt="" onerror="this.remove()"><span>${esc(x.name)}</span></div></td>
      <td class="mt-res">${x.win ? '胜' : '负'}${x.abandon ? '<small title="这局他中途离开了">离开</small>' : ''}</td>
      <td>${kda(x.kda)}</td>
      <td>${perf(x.perf)}</td>${sz ? `<td>${stratz(x.stratz)}</td>` : ''}
      <td class="mt-numcell">${x.lh ?? '-'} / ${x.dn ?? '-'}</td>
      <td class="mt-numcell">${x.gpm ?? '-'} / ${x.xpm ?? '-'}</td>
      <td class="mt-numcell">${num(x.damage)}</td>
      <td class="mt-numcell">${x.level ?? '-'}</td>
      <td class="mt-numcell">${dur(x.duration)}</td>
      <td class="mt-mode" title="${esc(x.mode)}">${esc(x.mode_short || x.mode)}${x.party > 1 ? ` <small>组 ${x.party} 人</small>` : ''}</td>
      <td class="mt-faint" title="${when(x.t)}">${ago(x.t)}</td>
    </tr>`).join('');
  return `<div class="card">${head}${stats}</div>
    <div class="card mt-matches">
      <div class="card-head"><h2>最近 ${n} 局</h2><span class="card-hint">点一局看那一整局 10 个人的数据；表现分是和同英雄所有对局比（0~100，50 = 一般），悬停看每一项</span></div>
      <table class="mt-table"><thead><tr><th>英雄</th><th>结果</th><th>击杀 / 死亡 / 助攻</th><th>表现</th>${szHead(sz)}<th>正 / 反补</th><th>经济 / 经验每分</th>
        <th>英雄伤害</th><th>等级</th><th>时长</th><th>模式</th><th>时间</th></tr></thead><tbody>${rows}</tbody></table>
    </div>`;
}

// ---------- 右边：一整局 ----------

function itemIcon(it, cls = '') {
  if (!it) return `<span class="mt-item mt-item-empty ${cls}"></span>`;
  return `<span class="mt-item ${cls}" title="${esc(it.name)}"><img src="/items/${encodeURIComponent(it.key)}.png" alt="${esc(it.name)}" onerror="this.replaceWith(document.createTextNode(this.alt.slice(0, 2)))"></span>`;
}

function playerRow(p, sz) {
  const tags = (p.me ? '<span class="mt-you">你</span>' : '') + (p.focus && !p.me ? '<span class="mt-you is-focus">他</span>' : '')
    + (p.party ? `<span class="mt-party mt-party-${p.party % 4}" title="组队 ${p.party}（同一个颜色是一起排的）"></span>` : '')
    + (p.abandon ? '<span class="mt-faint" title="中途离开了">离开</span>' : '');
  const name = p.account_id
    ? `<a class="mt-name" data-query="${p.account_id}" title="查他的战绩">${esc(p.name)}</a>`
    : `<span class="mt-name mt-faint" title="没公开比赛数据">${esc(p.name)}</span>`;
  const items = p.items.map((it) => itemIcon(it)).join('') + itemIcon(p.neutral, 'mt-neutral')
    + (p.scepter ? '<span class="mt-buff" title="吃了阿哈利姆神杖">杖</span>' : '')
    + (p.shard ? '<span class="mt-buff" title="有阿哈利姆魔晶">晶</span>' : '');
  const back = p.backpack.length ? `<div class="mt-backpack" title="背包">${p.backpack.map((it) => itemIcon(it, 'mt-small')).join('')}</div>` : '';
  return `<tr class="${p.focus ? 'is-focus' : ''} ${p.me ? 'is-me' : ''}">
    <td><div class="mt-playercell"><img src="${portrait(p.hero_id)}" alt="" title="${esc(p.hero)}" onerror="this.remove()">
      <div><div class="mt-pname">${name}${tags}</div><small>${esc(p.hero)}${p.stratz?.pos ? ` · ${p.stratz.pos} 号位` : ''}${p.rank ? ` · ${esc(p.rank)}` : ''}</small></div></div></td>
    <td class="mt-numcell">${p.level ?? '-'}</td>
    <td>${kda(p.kda)}</td>
    <td>${perf(p.perf)}</td>${sz ? `<td>${stratz(p.stratz)}</td>` : ''}
    <td class="mt-numcell">${p.lh ?? '-'} / ${p.dn ?? '-'}</td>
    <td class="mt-numcell">${p.gpm ?? '-'} / ${p.xpm ?? '-'}</td>
    <td class="mt-numcell">${num(p.damage)}</td>
    <td class="mt-numcell">${num(p.tower)} / ${num(p.heal)}</td>
    <td class="mt-numcell mt-gold">${num(p.net_worth)}</td>
    <td><div class="mt-items">${items}</div>${back}</td>
  </tr>`;
}

function teamTable(side, players, won, sz) {
  const label = side === 'radiant' ? '天辉' : '夜魇';
  return `<div class="mt-team mt-team-${side}">
    <div class="mt-team-head"><b>${label}</b>${won ? '<span class="mt-won">胜利</span>' : ''}</div>
    <table class="mt-table mt-table-match"><thead><tr><th>玩家</th><th>等级</th><th>击杀 / 死亡 / 助攻</th><th>表现</th>${szHead(sz)}<th>正 / 反补</th>
      <th>经济 / 经验每分</th><th>英雄伤害</th><th>建筑伤害 / 治疗</th><th>净资产</th><th>装备</th></tr></thead>
      <tbody>${players.map((p) => playerRow(p, sz)).join('')}</tbody></table></div>`;
}

function matchView(p, mid, data) {
  const who = mates.find((x) => x.account_id === p);
  const back = `<button class="btn btn-small" data-back="${p || ''}">← 返回${who ? ` ${esc(who.name)} 的战绩` : ''}</button>`;
  if (!data) return `<div class="card"><div class="mt-head">${back}<span class="spacer"></span></div><div class="mt-faint">正在拉比赛 ${esc(mid)} …</div></div>`;
  if (data.error) return `<div class="card"><div class="mt-head">${back}</div><div class="mt-err">${esc(data.error)}</div></div>`;
  const m = data;
  const sz = [...m.teams.radiant, ...m.teams.dire].some((x) => x.stratz);
  return `<div class="card">
      <div class="mt-head">${back}<span class="spacer"></span><span class="mt-faint">比赛 ${m.match_id} · ${esc(m.mode)} · ${when(m.start_time)}</span></div>
      <div class="mt-score">
        <span class="mt-side-radiant ${m.radiant_win ? 'is-won' : ''}">天辉</span>
        <b>${m.score[0] ?? '-'}</b><span class="mt-dur">${dur(m.duration)}</span><b>${m.score[1] ?? '-'}</b>
        <span class="mt-side-dire ${m.radiant_win ? '' : 'is-won'}">夜魇</span>
      </div>
      <div class="mt-faint mt-center">表现分是和同英雄所有对局比（0~100，50 = 一般），悬停看每一项；点名字查那个人的战绩；同色小圆点是一起排的</div>
      ${m.stratz_note ? `<div class="mt-faint mt-center">${esc(m.stratz_note)}</div>` : ''}
    </div>
    ${teamTable('radiant', m.teams.radiant, m.radiant_win, sz)}
    ${teamTable('dire', m.teams.dire, !m.radiant_win, sz)}`;
}

async function loadMatch(mid, p) {
  if (matchCache.has(mid)) return;
  matchCache.set(mid, null);
  try {
    const res = await getJSON(`/api/match/${mid}${p ? `?p=${p}` : ''}`);
    matchCache.set(mid, res.ok ? res.match : { error: res.message });
  } catch (e) {
    matchCache.set(mid, { error: `连不上助手：${e.message}` });
  }
  if (route().m === mid) render();
  if (!matchCache.get(mid) || matchCache.get(mid).error) matchCache.delete(mid);   // 出错的下次再试
}

$('#view').addEventListener('click', (e) => {
  const t = e.target;
  const r = t.closest('[data-refresh]');
  if (r) return query(r.dataset.refresh, true);
  const b = t.closest('[data-back]');
  if (b) return history.length > 1 && route().m ? history.back() : go(Number(b.dataset.back) || null);
  const q = t.closest('[data-query]');
  if (q) return query(q.dataset.query);
  const row = t.closest('[data-m]');
  if (row) go(route().p, row.dataset.m);
});

// ---------- 画 ----------

function render() {
  const { p, m } = route();
  document.body.classList.toggle('is-match', !!m);   // 看整局时收起左边，表格宽
  paintPeople();
  const view = $('#view');
  if (m) {
    const data = matchCache.get(m);
    view.innerHTML = matchView(p, m, data);
    if (data === undefined) loadMatch(m, p);
  } else {
    const who = mates.find((x) => x.account_id === p);
    view.innerHTML = overview(who || (pending.has(p) ? { account_id: p, loaded: false } : null));
    // 旧版存的数据（没有比赛编号）或者还没查到过：自动查一次
    if (who && (who.stale || (!who.loaded && !who.error)) && !pending.has(p) && !autoTried.has(p)) {
      autoTried.add(p);
      query(String(p), true);
    }
  }
}

window.addEventListener('hashchange', () => { render(); $('#view').scrollTop = 0; });
setInterval(render, 60000);   // 「几分钟前」

$('#form').addEventListener('submit', (e) => {
  e.preventDefault();
  const input = $('#code');
  if (!input.value.trim()) return input.focus();
  query(input.value);
  input.value = '';
});

getJSON('/api/mates').then((res) => {
  mates = res.mates || [];
  if (!route().p && mates.length) history.replaceState(null, '', `#p=${mates[0].account_id}`);
  render();
}).catch((e) => showMessage(`连不上助手：${e.message}`, true));
