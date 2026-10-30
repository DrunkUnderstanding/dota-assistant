// 叠加层的内容（选人推荐卡片 / 提醒）。叠加层窗口和控制面板右侧的预览共用这一份。
import { getJSON } from './api.js';

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export const portrait = (id) => `/portraits/${id}.png`;

// 一行字里的装备显示成图标（itembuild.line_view 的 parts：str 原样，{item} 一件装备，{group} 并列几件），名字在悬停提示里；
// 图标没下载到时显示名字。★ = 针对这局、· = 之后常见、✚ = 按死法补的，和文字版同一套记号
const MARK = { counter: '★', common: '·', death: '✚' };
function itemChip(c) {
  const mark = c.mark ? `<i class="ic-mark">${MARK[c.mark] || ''}</i>` : '';
  return `<span class="ic ${c.mark ? `ic-${esc(c.mark)}` : ''}" title="${esc(c.name)}">${mark}`
    + `<img src="/items/${encodeURIComponent(c.item)}.png" alt="" onerror="this.remove()"><span class="ic-name">${esc(c.name)}</span>`
    + `${c.sub ? `<small>${esc(c.sub)}</small>` : ''}</span>`;
}
function partsHTML(parts) {
  const one = (p) => typeof p === 'string' ? esc(p)
    : p.group ? `<span class="ic-group">${p.group.map(itemChip).join('')}</span>` : itemChip(p);
  // 出装顺序的「→」和后面那件粘在一起，折行时箭头跟着那件到下一行，不会孤零零挂在上一行行尾
  return parts.map((p, i) => (p === ' → ' ? '' : parts[i - 1] === ' → ' ? `<span class="ic-step"><i>→</i>${one(p)}</span>` : one(p))).join('');
}
const lineBody = (l) => (l.parts ? partsHTML(l.parts) : esc(l.text));

// 胜率条：45% 以下空、60% 满（推荐里的胜率基本落在这个区间）
const barPct = (wr) => Math.max(4, Math.min(100, ((wr - 45) / 15) * 100));

function face(h, cls = '') {
  return `<span class="face ${cls}" title="${esc(h.name)}"><img src="${portrait(h.id)}" alt="" onerror="this.remove()"><span class="face-name">${esc(h.name)}</span></span>`;
}

function lineup(label, heroes, cls) {
  if (!heroes.length) return '';
  return `<div class="lineup ${cls}"><span class="lineup-label">${label}</span><div class="lineup-faces">${heroes.map((h) => face(h)).join('')}</div></div>`;
}

// 顶栏对照：双方 5 格按 Dota 顶栏的顺序（Live.slots_view），玩家一眼看出哪格没认出。
// 金色「?」= 这格有人选了但没认出；虚线「空」= 看着还没选；认出的头像点了也能改（认错了）。点开下面接「这格是谁」：
// 先点属性再列这个属性的全部（和局内顶栏的菜单一样，玩家 2026-10-28 要的），候选顺序 Python 排好（选人网格里被选走的在前，再按像不像）
const SIDE_CN = { radiant: '天辉', dire: '夜魇' };
const ATTRS = [['str', '力量'], ['agi', '敏捷'], ['int', '智力'], ['all', '全才']];
let slotPick = null;       // 打开的「这格是谁」：{ side, i, attr, order, taken }；打开时定住候选，快照每秒更新也不在鼠标底下换按钮
let heroIndex = null;      // id → { id, name, key, attr }，第一次打开时从 /api/heroes 拿
function loadHeroes(root) {
  if (heroIndex) return;
  heroIndex = {};
  getJSON('/api/heroes').then((list) => {
    heroIndex = Object.fromEntries(list.map((h) => [h.id, h]));
    renderOverlay(root, root._view);
  }).catch(() => { heroIndex = null; });
}

function slotCell(c, side, i) {
  const where = `${SIDE_CN[side]}第 ${i + 1} 格`;
  if (c.state === 'me') {
    return `<span class="slot is-me" title="你">${c.hero ? face(c.hero) : '<i>你</i>'}</span>`;
  }
  const open = slotPick && slotPick.side === side && slotPick.i === i ? 'is-open' : '';
  const attr = `data-slot="${side}:${i}"`;
  if (c.state === 'ok') {
    const tip = `${where}：${c.hero.name}${c.manual ? '（你告诉我的）' : ''}\n认错了就点一下改`;
    return `<span class="slot is-ok ${open}" ${attr} title="${esc(tip)}">${face(c.hero)}</span>`;
  }
  if (c.state === 'ask') {
    const tip = `${where}有人选了，但我没认出来${c.guess ? `（最像${c.guess}，没把握）` : ''}\n点一下告诉我是谁，我记住这格头像，下次认得`;
    return `<span class="slot is-ask ${open}" ${attr} title="${esc(tip)}"><b>?</b></span>`;
  }
  const tip = `${where}：看着还没选\n游戏里这格已经有英雄了？点一下告诉我是谁`;
  return `<span class="slot is-empty ${open}" ${attr} title="${esc(tip)}"><i>空</i></span>`;
}

function slotRow(label, side, cells, cls) {
  return `<div class="lineup slot-row ${cls}"><span class="lineup-label" title="${SIDE_CN[side]}，从左到右和游戏顶栏一样">${label}</span>`
    + `<div class="lineup-faces">${cells.map((c, i) => slotCell(c, side, i)).join('')}</div></div>`;
}

function pickerHTML(slots) {
  const cell = slotPick && slots[slotPick.side]?.[slotPick.i];
  if (!cell || cell.state === 'me') return '';
  const title = cell.hero ? `${cell.hero.name}认错了？${SIDE_CN[slotPick.side]}第 ${slotPick.i + 1} 格是谁`
    : `${SIDE_CN[slotPick.side]}第 ${slotPick.i + 1} 格是谁？`;
  const all = slotPick.order.map((id) => heroIndex?.[id]).filter(Boolean);
  const taken = new Set(slotPick.taken);
  const tabs = ATTRS.map(([a, name]) => `<button class="sp-tab sp-${a} ${slotPick.attr === a ? 'is-on' : ''}" type="button" data-attr="${a}">`
    + `${name}<em>${all.filter((h) => h.attr === a).length}</em></button>`).join('');
  let body;
  if (!all.length) body = `<div class="sp-tip">${heroIndex ? '没有候选，到控制面板这一格填' : '英雄表还没拿到…'}</div>`;
  else if (!slotPick.attr) body = '<div class="sp-tip">先点他是哪种属性，再从里面挑</div>';
  else {
    body = `<div class="sp-grid">${all.filter((h) => h.attr === slotPick.attr).map((h) => {
      const tk = taken.has(h.id);
      return `<button class="sp-hero ${tk ? 'is-taken' : ''}" type="button" data-hero="${h.id}" title="${esc(tk ? `${h.name}（选人网格里被选走了）` : h.name)}">`
        + `<img src="/icons/heroes/icons/${encodeURIComponent(h.key)}.png" alt="" onerror="this.remove()"><span>${esc(h.name)}</span></button>`;
    }).join('')}</div>`;
  }
  return `
    <section class="slot-picker">
      <div class="sp-head"><span>${esc(title)}</span><button class="sp-x" type="button" title="收起">×</button></div>
      <div class="sp-tabs">${tabs}</div>${body}
      <div class="sp-note">金边 = 选人网格里被选走的，排在前面；挑了会记住这格头像，下次认得</div>
    </section>`;
}

function slotsHTML(v) {
  const s = v.slots;
  const enemy = s.ally === 'radiant' ? 'dire' : 'radiant';
  const tip = s.asks ? `<div class="slot-tip"><b>${s.asks}</b> 格有人选了但我没认出（金色 ?），点一下告诉我是谁</div>` : '';
  return slotRow('敌', enemy, s[enemy], 'lineup-enemy') + slotRow('友', s.ally, s[s.ally], 'lineup-ally') + tip;
}

// 对位克制：推荐英雄对每个已选敌人的胜率差，±1 个百分点以内算没明显关系
const VS_EDGE = 1.0;
// 「配合」一行和「对位」一样：我方每个已选队友一格，正的（配得好）蓝、负的（相互拖累，例如抢同一个位置）红（玩家 2026-10-30：负的配合也要显示）
function vsRow(item, syn = false) {
  const list = syn ? item.syn : item.vs;
  if (!list?.length) return '';
  const cells = list.map((e) => {
    const cls = e.delta >= VS_EDGE ? (syn ? 'vs-syn' : 'vs-good') : e.delta <= -VS_EDGE ? 'vs-bad' : '';
    const num = `${e.delta > 0 ? '+' : ''}${e.delta.toFixed(1)}`;
    const word = syn ? (e.delta >= VS_EDGE ? '（配）' : e.delta <= -VS_EDGE ? '（不配）' : '')
      : (e.delta >= VS_EDGE ? '（克）' : e.delta <= -VS_EDGE ? '（被克）' : '');
    const tip = `${item.name} ${syn ? '和' : '对'} ${e.name}：${num}%${word}`;
    return `<span class="vs ${cls}" title="${esc(tip)}">${face(e, syn ? 'face-xs face-ally' : 'face-xs')}<b>${num}</b></span>`;
  }).join('');
  return `<div class="pick-vs"><span class="pick-vs-label">${syn ? '配合' : '对位'}</span>${cells}</div>`;
}

// 对面分路（按常见位置猜）：三路各一行，我这路高亮；我选好英雄后下面接我这路的对线提示（原句在悬停提示里）
function laneFace(h) {
  const tip = `${h.name}：猜 ${h.pos} 号位（${Math.round(h.conf * 100)}% 把握）`;
  return `<span class="lane-hero ${h.sure ? '' : 'is-unsure'}" title="${esc(tip)}">${face(h, 'face-sm')}<b>${h.pos}${h.sure ? '' : '?'}</b></span>`;
}

function lanesHTML(l) {
  if (!l) return { rows: '', tips: '' };
  const rows = l.rows.map((r) => {
    const empty = '<span class="lane-empty" title="对面这个位置还没选">?</span>'.repeat(r.missing);
    const mine = r.mine ? `<em>你这路${l.partner ? ` · 队友 ${esc(l.partner.name)}` : ''}</em>` : '';
    return `<div class="lane ${r.mine ? 'is-mine' : ''}"><span class="lane-label">${esc(r.label)}</span><div class="lane-faces">${r.heroes.map(laneFace).join('')}${empty}</div>${mine}</div>`;
  }).join('');
  const tips = (l.tips || []).map((w) => `<p class="why why-${esc(w.kind)}" title="${esc(w.quote)}">${w.kind === 'good' ? '✓' : '✗'} ${esc(w.text)}</p>`).join('');
  return {
    rows: `
    <section class="ov-lanes" title="对面谁走哪路是按每个英雄在本分段最常打的位置猜的，数字是猜的位置，? = 没把握">
      <h3>对面分路<small>按常见位置猜</small></h3>${rows}
    </section>`,
    tips: tips ? `<section class="lane-tips"><h4>你这路怎么打</h4>${tips}</section>` : '',
  };
}

// 我方五个位置：队友占了的显示队友（猜的标 ?），空着的给「推荐 3 + 针对这局 3」（advisor.TEAM_TOP / TEAM_TARGETED）。
// 平时只露第一推荐，点行尾「▾」展开两排；展开了哪些位置记在这里，重画后保持（和要点卡片的展开一个思路）。
// 我还没选：推荐是给我挑位置的，点空位置切过去；我选好以后：我那格是我的英雄，空位置的推荐是给队友的（不能点），一直留到进游戏
const teamUnfolded = new Set();
// 胜率怎么来的、熟不熟都直接写出来，不靠悬停（玩家 2026-10-30：「不能用悬停的方式，应该是直接显示」）。
// 收起时只露第一推荐：头像 + 胜率 + 简短算法（自身＋克制＋配合）；展开后一个英雄一行：名字 + 胜率 + 完整算法（自身拆成整体 · 位置 · 冷门打折）。
// 不熟的（生涯局数不到门槛，advisor.team_pick 的 unfamiliar = [局数, 胜场]）头像变暗、写「不熟 15 局」
const unfamiliarText = (u) => (u[0] ? `不熟 ${u[0]} 局 ${Math.round((u[1] / u[0]) * 100)}%` : '不熟 没玩过');
function teamPick(p, full) {
  const u = p.unfamiliar;
  const name = full ? `<span class="team-pick-name">${esc(p.name)}</span>` : '';
  return `<span class="team-pick ${full ? 'is-line' : ''} ${u ? 'is-unfamiliar' : ''}">${face(p, 'face-sm')}${name}`
    + `<b>${p.winrate.toFixed(1)}</b>${u ? `<i>${unfamiliarText(u)}</i>` : ''}${calcRow(p, !full)}</span>`;
}
function teamPicksHTML(t) {
  const targeted = t.targeted || [];
  if (!t.picks.length) return { body: '<span class="team-none">无候选</span>', fold: '' };
  const open = teamUnfolded.has(t.position);
  const more = t.picks.length + targeted.length > 1;
  const fold = more ? `<button class="team-fold" type="button" data-team-fold="${t.position}" title="${open ? '收起' : '展开：推荐 + 针对这局'}">`
    + `${open ? '▴ 收起' : `▾ ${t.picks.length}+${targeted.length}`}</button>` : '';
  const row = (label, ps) => `<div class="team-picks is-list"><i class="team-grp">${label}</i>${ps.map((p) => teamPick(p, true)).join('')}</div>`;
  const body = open
    ? `<div class="team-groups">${row('推荐', t.picks)}${targeted.length ? row('针对', targeted) : ''}</div>`
    : `<div class="team-picks">${teamPick(t.picks[0], false)}</div>`;
  return { body, fold, open };
}

function teamHTML(team, picked, duo) {
  if (!team) return '';
  const rows = team.map((t) => {
    if (t.me) {
      return `<div class="team-row is-me" title="你选的${esc(t.ally.name)}"><span class="team-label">${esc(t.name)}</span>`
        + `<span class="lane-hero">${face(t.ally, 'face-sm')}</span><em class="team-cur">你</em></div>`;
    }
    const cur = t.current && !picked;
    const flags = cur ? '<em class="team-cur">当前</em>' : '';
    if (t.mine) {          // 我还没选的当前位置：不列英雄，我的推荐看大卡片（玩家 2026-10-30：这里只看队友的）
      return `<div class="team-row is-mine is-cur" title="你的位置：推荐看下面的卡片"><span class="team-label">${esc(t.name)}</span>`
        + `<span class="team-none">你 · 推荐看下面</span>${flags}</div>`;
    }
    if (t.ally) {
      const tip = `队友${t.ally.name}${t.ally.sure ? '' : `（猜的，${Math.round(t.ally.conf * 100)}% 把握）`}占了${t.name}`;
      const warn = cur ? '<span class="team-warn">队友占了你的位置</span>' : '';
      return `<div class="team-row is-taken ${cur ? 'is-cur' : ''}" title="${esc(tip)}"><span class="team-label">${esc(t.name)}</span>`
        + `<span class="lane-hero ${t.ally.sure ? '' : 'is-unsure'}">${face(t.ally, 'face-sm')}${t.ally.sure ? '' : '<b>?</b>'}</span>${warn}${flags}</div>`;
    }
    const { body, fold, open } = teamPicksHTML(t);
    const unfolded = open ? 'is-unfolded' : '';
    if (picked) {
      return `<div class="team-row is-ally-pick ${unfolded}" title="${esc(`队友打${t.name}可以选这些`)}">`
        + `<span class="team-label">${esc(t.name)}</span>${body}${fold}</div>`;
    }
    const tip = `这里是给队友的推荐；点一下：改成你打${t.name}`;
    return `<div class="team-row is-open ${unfolded}" data-pos="${t.position}" title="${esc(tip)}">`
      + `<span class="team-label">${esc(t.name)}</span>${body}${flags}${fold}</div>`;
  }).join('');
  // 空位置都是给队友的推荐（我的推荐在下面的大卡片），同一个英雄只出现一次
  const hint = '队友占了哪几个位置：选人界面认出来的优先，其余按常打位置猜；空位置后面是给队友的推荐（▾ 展开看推荐 + 针对这局）'
    + (duo ? `；按双排队友${duo}生涯打过的英雄分「推荐 / 针对」，他不熟的标「不熟」` : '')
    + (picked ? '' : '；点空位置可以把你的位置换过去');
  const small = duo ? `给队友的推荐 · 按${duo}会玩的分` : '给队友的推荐';
  return `
    <section class="ov-team" title="${esc(hint)}">
      <h3>剩余位置<small>${esc(small)}</small></h3>${rows}
    </section>`;
}

// 胜率怎么来的（advisor.calc_view）：自身（整体 → 位置 → 冷门打折）＋克制＋配合＝最终，依次在 logit 里相加、每步换回胜率算差，
// 所以这几项加起来正好等于卡片上的胜率；「对位」里单个敌人的数是按 50% 附近换算的，直接加会差一点
// compact = 剩余位置收起时那一行：不拆自身、不写「＝」（胜率就在旁边）；
const CALC_SIGN = (v) => (v >= 0 ? '＋' : '－');
function calcRow(item, compact = false) {
  if (!item.calc) return '';
  const c = Object.fromEntries(item.calc.map((x) => [x.kind, x]));
  const sub = [`整体 ${c.start.value.toFixed(1)}`]
    .concat(['pos', 'shrink'].filter((k) => Math.abs(c[k].value) >= 0.05)
      .map((k) => `${c[k].name} ${c[k].value > 0 ? '+' : ''}${c[k].value.toFixed(1)}`));
  const terms = ['counter', 'syn', 'other'].filter((k) => Math.abs(c[k].value) >= 0.05)
    .map((k) => `<span class="calc-${k}${c[k].value < 0 ? ' is-neg' : ''}">${CALC_SIGN(c[k].value)}${esc(c[k].name)} <b>${Math.abs(c[k].value).toFixed(1)}</b></span>`).join('');
  const base = `<span class="calc-base">自身 <b>${c.base.value.toFixed(1)}</b>${compact ? '' : `<small>（${esc(sub.join(' · '))}）</small>`}</span>`;
  const eq = compact ? '' : `<span class="calc-eq">＝ <b>${item.winrate.toFixed(1)}</b></span>`;
  return `<span class="pick-calc ${compact ? 'is-compact' : ''}">${base}${terms}${eq}</span>`;
}

// targeted =「针对这局」：右上角胜率前面醒目地写这一手的克制、配合（这组就是按它俩挑的，玩家 2026-10-30 要一眼看到，
// 而且要分开、不同颜色：克制绿、配合蓝，和理由标签「克 / 配」同色）。数值取 calc 里的克制 / 配合两步（百分点）
const situBadge = (c, kind, label) => {
  const v = c[kind]?.value ?? 0;
  return `<span class="pick-situ situ-${kind} ${v < 0 ? 'is-neg' : ''}">${label} <b>${v >= 0 ? '+' : ''}${v.toFixed(1)}</b></span>`;
};
function card(item, rank, targeted = false) {
  const tags = item.tags.map((t) => `<span class="tag tag-${t.kind}">${esc(t.text)}</span>`).join('');
  // 机制上为什么克 / 怕（知识库原句放在悬停提示里）
  const why = (item.why || []).map((w) => `<p class="why why-${esc(w.kind)}" title="${esc(w.quote)}">${w.kind === 'good' ? '✓' : '✗'} ${esc(w.text)}</p>`).join('');
  return `
    <div class="pick ${rank === 0 ? 'pick-top' : ''}" data-id="${item.id}" title="点一下：看选${esc(item.name)}怎么打这套阵容">
      <div class="pick-art"><img src="${portrait(item.id)}" alt="" onerror="this.remove()"><button class="pick-skip" type="button" data-skip="${item.id}" title="这局不想玩${esc(item.name)}：去掉，后面的顺位补上来">×</button></div>
      <div class="pick-body">
        <div class="pick-head">
          <span class="pick-name">${esc(item.name)}</span>
          ${targeted && item.calc ? (() => {
    const c = Object.fromEntries(item.calc.map((x) => [x.kind, x]));
    return `<span class="pick-situs">${situBadge(c, 'counter', '克制')}${situBadge(c, 'syn', '配合')}</span>`;
  })() : ''}
          <span class="pick-wr">${item.winrate.toFixed(1)}<small>%</small></span>
        </div>
        <div class="pick-bar"><i style="width:${barPct(item.winrate)}%"></i></div>
        <div class="pick-tags">${tags}</div>
        ${calcRow(item)}
        ${vsRow(item)}
        ${vsRow(item, true)}
        ${why ? `<div class="pick-why">${why}</div>` : ''}
      </div>
    </div>`;
}

// 「推荐」「针对这局」等推荐组可以收起（玩家 2026-10-30 要的）：收起后一个英雄一个小头像 + 胜率，点了照样看「选它怎么打」。
// 收起了哪几组按组名记在浏览器本地存储里，换局 / 重开助手都保持（存不了就只在这次开着时记）
const FOLD_KEY = 'ov-folded-groups';
const groupFolded = (() => {
  try { return new Set(JSON.parse(localStorage.getItem(FOLD_KEY) || '[]')); } catch { return new Set(); }
})();
function toggleGroup(title) {
  if (!groupFolded.delete(title)) groupFolded.add(title);
  try { localStorage.setItem(FOLD_KEY, JSON.stringify([...groupFolded])); } catch { /* 存不了就算了 */ }
}
function mini(item, rank) {
  return `<span class="pick-mini ${rank === 0 ? 'is-top' : ''}" data-id="${item.id}" title="${esc(`${item.name} ${item.winrate.toFixed(1)}%\n点一下：看选${item.name}怎么打这套阵容`)}">`
    + `${face(item)}<b>${item.winrate.toFixed(1)}</b></span>`;
}

// 内容分两栏：A = 阵容 + 分路，B = 我这路怎么打 / 推荐 / 威胁。窄的时候上下接着排（顺序不变），
// 叠加层拉宽到 640px 以上左右并排（base.css 的容器查询），整张卡片矮一半，不容易超出屏幕
function draftHTML(v) {
  const sideCls = v.side === 'radiant' ? 'side-radiant' : v.side === 'dire' ? 'side-dire' : 'side-unknown';
  const head = `
    <header class="ov-head">
      <span class="ov-pos">${esc(v.position_name)}</span>${v.position_auto ? '<span class="ov-auto" title="从选人界面名字下面的位置自动认出来的">自动</span>' : ''}
      <span class="ov-side ${sideCls}">${esc(v.side_name)}</span>
      ${v.draft_rate ? `<span class="ov-rate" title="${esc(`阵容胜率：${v.draft_rate.note}`)}">阵容 <b>${v.draft_rate.winrate.toFixed(1)}</b>%</span>` : ''}
      <span class="ov-brand">选人助手</span>
    </header>`;
  let a = '';
  let html = '';
  if (v.side_assumed) {                              // GSI 没连上：友 / 敌是按默认的天辉分的
    a += `<div class="ov-sidewarn">不知道你在哪边，先当<b>天辉（左边）</b>是我方。在夜魇的话到控制面板「阵营」选夜魇</div>`;
  }
  const lines = (v.slots ? slotsHTML(v) : lineup('敌', v.enemies, 'lineup-enemy') + lineup('友', v.allies, 'lineup-ally'))
    + lineup('禁', v.bans || [], 'lineup-ban');
  if (lines) a += `<section class="ov-lineups">${lines}</section>`;
  if (v.slots) a += pickerHTML(v.slots);
  a += teamHTML(v.team, !!v.my_hero, v.duo);
  const lanes = lanesHTML(v.lanes);
  a += lanes.rows;
  html += lanes.tips;
  if (v.my_hero) {
    html += `
      <section class="ov-picked">
        <div class="pick-art"><img src="${portrait(v.my_hero.id)}" alt="" onerror="this.remove()"></div>
        <div><div class="ov-picked-label">你已选</div><div class="ov-picked-name">${esc(v.my_hero.name)}</div></div>
      </section>`;
  }
  let rank = 0;
  for (const g of v.groups) {
    const note = g.note ? `<small>${esc(g.note)}</small>` : '';
    const folded = groupFolded.has(g.title);
    const fold = `<button class="group-fold" type="button" data-group-fold="${esc(g.title)}" title="${folded ? '展开：看每个推荐的理由' : '收起：只留头像和胜率'}">`
      + `${folded ? `▾ 展开 ${g.items.length}` : '▴ 收起'}</button>`;
    const items = folded ? `<div class="pick-minis">${g.items.map((it) => mini(it, rank++)).join('')}</div>`
      : g.items.map((it) => card(it, rank++, g.targeted)).join('');
    html += `<section class="ov-group ${folded ? 'is-folded' : ''}"><h3><span>${esc(g.title)}${note}</span>${fold}</h3>${items}</section>`;
  }
  if (v.skipped?.length) {                           // 这局点 × 去掉的：点名字恢复，新一局自动清空
    html += `<div class="ov-skipped"><span>这局不想玩</span>${v.skipped.map((h) => `<button type="button" data-unskip="${h.id}" title="恢复推荐${esc(h.name)}">${esc(h.name)} ↺</button>`).join('')}</div>`;
  }
  if (v.groups.length) html += `<div class="ov-tip">点推荐英雄看「选它怎么打」· 点 × 这局不推荐它</div>`;
  if (!v.groups.length && !v.my_hero) {
    html += `<div class="ov-empty">暂无推荐（数据没拉或者位置下没有候选）</div>`;
  }
  if (v.threat) {
    html += `<div class="ov-threat">${face(v.threat, 'face-sm')}<span>注意 <b>${esc(v.threat.name)}</b> 对我方威胁最大</span><em>${v.threat.delta > 0 ? '+' : ''}${v.threat.delta.toFixed(1)}%</em></div>`;
  }
  // 标题栏下面的都放进 .ov-body：叠加层窗口放不下时只有这一块上下滚（overlay.html），标题栏和右上角按钮不动
  if (!a || !html) return `${head}<div class="ov-body">${a}${html}</div>`;   // 只有一栏的内容，不用分
  return `${head}<div class="ov-body"><div class="ov-cols"><div class="ov-col">${a}</div><div class="ov-col">${html}</div></div></div>`;
}

// closable：局内提醒，带关闭按钮（点它或按显隐热键关掉）
function noticeHTML(v) {
  const close = v.closable ? '<button class="ov-close" type="button" title="关闭这条提醒">×</button>' : '';
  return `
    <header class="ov-head"><span class="ov-pos">${esc(v.title)}</span><span class="ov-brand">选人助手</span>${close}</header>
    <section class="ov-notice">${v.lines.map((l) => `<p class="notice-${esc(l.kind)}">${lineBody(l)}</p>`).join('')}</section>`;
}

// 本局要点：按阶段分页（对线期：分路 + 对线 / 起手 / 对面要防；中期：出装 / 强势期；各页再接 AI 写的这一阶段打法），热键翻页
// 每个小节只显示前几条，其余（more）折叠，点「展开」看；展开了哪些记在这里，重画（例如 AI 写完补进来）后保持
const unfolded = new Set();
const foldKey = (v, s) => `${v.title}|${v.page}|${s.title}`;
const briefLine = (l) => `<p class="brief-${esc(l.kind)}"${l.quote ? ` title="${esc(l.quote)}"` : ''}>${lineBody(l)}${l.note ? `<small>${esc(l.note)}</small>` : ''}</p>`;
function briefHTML(v) {
  const close = v.closable ? '<button class="ov-close" type="button" title="关闭">×</button>' : '';
  const tabs = (v.tabs || []).map((t) => `<span class="brief-tab ${t.on ? 'is-on' : ''}" data-page="${esc(t.page)}">${esc(t.label)}</span>`).join('');
  const sections = v.sections.map((s) => {
    const more = s.more || [];
    const key = foldKey(v, s);
    const open = more.length && unfolded.has(key);
    const fold = more.length ? `<button class="brief-fold" type="button" data-fold="${esc(key)}">${open ? '▴ 收起' : `▾ 展开另 ${more.length} 条`}</button>` : '';
    return `
    <section class="brief-sec"><h3>${esc(s.title)}</h3>
      ${s.lines.map(briefLine).join('')}${open ? `<div class="brief-more">${more.map(briefLine).join('')}</div>` : ''}${fold}</section>`;
  }).join('');
  return `
    <header class="ov-head"><span class="ov-pos">${esc(v.title)}</span><span class="ov-brand">选人助手</span>${close}</header>
    ${tabs ? `<div class="brief-tabs">${tabs}<em>${esc(v.hint || '')}</em></div>` : ''}
    <div class="brief-body">${sections}</div>`;
}

// 只在内容真的变了时重画，避免动画每秒重放
// 叠加层外观（config 里的 overlay_color / overlay_opacity）：只改卡片底色，文字和标签不跟着透明
export function applyOverlayStyle(el, style) {
  if (!style) return;
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(style.color.slice(i, i + 2), 16));
  el.style.setProperty('--ov-fill', `rgba(${r}, ${g}, ${b}, ${style.opacity})`);
}

// 野区提醒（封野 / 拉野 / 屯野）：弹着别的卡片时叠在顶上一行，不挤掉那张卡片
function cueHTML(c) {
  return `<div class="ov-cue"><b>${esc(c.title)}</b><span>${esc(c.text)}</span></div>`;
}

export function renderOverlay(root, view) {
  const html = !view || !view.kind ? '<div class="ov-empty">等待助手…</div>'
    : (view.cue ? cueHTML(view.cue) : '')
      + (view.kind === 'notice' ? noticeHTML(view) : view.kind === 'brief' ? briefHTML(view) : draftHTML(view));
  root._view = view;                                  // 点「展开 / 收起」时按这份重画
  if (root._html === html) return false;
  root._html = html;
  const top = root.querySelector('.ov-body')?.scrollTop || 0;   // 重画（每次局面变了）不把滚到的位置弹回顶上
  root.innerHTML = html;
  const body = root.querySelector('.ov-body');
  if (body && top) body.scrollTop = top;
  return true;
}

// 叠加层和控制面板预览共用的点击：推荐英雄 → 「如果选它」，推荐英雄的 × → 这局不想玩，「这局不想玩」里的名字 → 恢复，
// 剩余位置 → 切位置，要点卡片的标签 → 翻页，
// 要点小节 / 剩余位置 / 推荐组的「展开 / 收起」→ 只在本地重画，× → 关卡片
export function bindOverlayClicks(root, act) {
  root.addEventListener('click', (e) => {
    const slot = e.target.closest('.slot[data-slot]');
    if (slot) {
      const [side, i] = [slot.dataset.slot.split(':')[0], Number(slot.dataset.slot.split(':')[1])];
      const cell = root._view?.slots?.[side]?.[i];
      const same = slotPick && slotPick.side === side && slotPick.i === i;
      slotPick = same || !cell ? null
        : { side, i, attr: null, order: cell.pick?.order || [], taken: cell.pick?.taken || [] };
      if (slotPick) loadHeroes(root);
      return renderOverlay(root, root._view);
    }
    const spTab = e.target.closest('.sp-tab[data-attr]');
    if (spTab && slotPick) {
      slotPick.attr = spTab.dataset.attr;
      return renderOverlay(root, root._view);
    }
    const spHero = e.target.closest('.sp-hero[data-hero]');
    if (spHero && slotPick) {
      act('name_slot', { side: slotPick.side, index: slotPick.i, hero_id: Number(spHero.dataset.hero) });
      slotPick = null;
      return renderOverlay(root, root._view);
    }
    if (e.target.closest('.sp-x')) {
      slotPick = null;
      return renderOverlay(root, root._view);
    }
    const fold = e.target.closest('.brief-fold[data-fold]');
    if (fold) {
      const key = fold.dataset.fold;
      if (!unfolded.delete(key)) unfolded.add(key);
      return renderOverlay(root, root._view);
    }
    const teamFold = e.target.closest('.team-fold[data-team-fold]');
    if (teamFold) {                                     // 在行里面，先拦下，不当成「切到这个位置」
      const p = Number(teamFold.dataset.teamFold);
      if (!teamUnfolded.delete(p)) teamUnfolded.add(p);
      return renderOverlay(root, root._view);
    }
    const groupFold = e.target.closest('.group-fold[data-group-fold]');
    if (groupFold) {
      toggleGroup(groupFold.dataset.groupFold);
      return renderOverlay(root, root._view);
    }
    const tab = e.target.closest('.brief-tab[data-page]');
    const pos = e.target.closest('.team-row[data-pos]');
    if (pos) return act('set', { key: 'position', value: Number(pos.dataset.pos) });
    if (tab) return act('brief_page', { page: tab.dataset.page });
    const skip = e.target.closest('.pick-skip[data-skip]');   // 在卡片里面，先拦下，不当成「如果选它」
    if (skip) return act('skip_hero', { hero_id: Number(skip.dataset.skip) });
    const unskip = e.target.closest('[data-unskip]');
    if (unskip) return act('unskip_hero', { hero_id: Number(unskip.dataset.unskip) });
    const pick = e.target.closest('.pick[data-id], .pick-mini[data-id]');
    if (pick) return act('explain_pick', { hero_id: Number(pick.dataset.id) });
    if (e.target.closest('.ov-close')) act('dismiss_coach');
  });
}
