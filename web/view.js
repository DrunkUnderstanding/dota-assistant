// 叠加层的内容（选人推荐卡片 / 提醒）。叠加层窗口和控制面板右侧的预览共用这一份。
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export const portrait = (id) => `/portraits/${id}.png`;

// 胜率条：45% 以下空、60% 满（推荐里的胜率基本落在这个区间）
const barPct = (wr) => Math.max(4, Math.min(100, ((wr - 45) / 15) * 100));

function face(h, cls = '') {
  return `<span class="face ${cls}" title="${esc(h.name)}"><img src="${portrait(h.id)}" alt="" onerror="this.remove()"><span class="face-name">${esc(h.name)}</span></span>`;
}

function lineup(label, heroes, cls) {
  if (!heroes.length) return '';
  return `<div class="lineup ${cls}"><span class="lineup-label">${label}</span><div class="lineup-faces">${heroes.map((h) => face(h)).join('')}</div></div>`;
}

// 对位克制：推荐英雄对每个已选敌人的胜率差，±1 个百分点以内算没明显关系
const VS_EDGE = 1.0;
function vsRow(item) {
  if (!item.vs?.length) return '';
  const cells = item.vs.map((e) => {
    const cls = e.delta >= VS_EDGE ? 'vs-good' : e.delta <= -VS_EDGE ? 'vs-bad' : '';
    const num = `${e.delta > 0 ? '+' : ''}${e.delta.toFixed(1)}`;
    const tip = `${item.name} 对 ${e.name}：${num}%${e.delta >= VS_EDGE ? '（克）' : e.delta <= -VS_EDGE ? '（被克）' : ''}`;
    return `<span class="vs ${cls}" title="${esc(tip)}">${face(e, 'face-xs')}<b>${num}</b></span>`;
  }).join('');
  return `<div class="pick-vs"><span class="pick-vs-label">对位</span>${cells}</div>`;
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

// 我方五个位置：队友占了的显示队友（猜的标 ?），空着的给两个推荐。
// 我还没选：推荐是给我挑位置的，点空位置切过去；我选好以后：我那格是我的英雄，空位置的推荐是给队友的（不能点），一直留到进游戏
function teamHTML(team, picked) {
  if (!team) return '';
  const rows = team.map((t) => {
    if (t.me) {
      return `<div class="team-row is-me" title="你选的${esc(t.ally.name)}"><span class="team-label">${esc(t.name)}</span>`
        + `<span class="lane-hero">${face(t.ally, 'face-sm')}</span><em class="team-cur">你</em></div>`;
    }
    const cur = t.current && !picked;
    const flags = (cur ? '<em class="team-cur">当前</em>' : '') + (t.best && !cur ? '<em class="team-best">推荐</em>' : '');
    if (t.ally) {
      const tip = `队友${t.ally.name}${t.ally.sure ? '' : `（猜的，${Math.round(t.ally.conf * 100)}% 把握）`}占了${t.name}`;
      const warn = cur ? '<span class="team-warn">队友占了你的位置</span>' : '';
      return `<div class="team-row is-taken ${cur ? 'is-cur' : ''}" title="${esc(tip)}"><span class="team-label">${esc(t.name)}</span>`
        + `<span class="lane-hero ${t.ally.sure ? '' : 'is-unsure'}">${face(t.ally, 'face-sm')}${t.ally.sure ? '' : '<b>?</b>'}</span>${warn}${flags}</div>`;
    }
    const picks = t.picks.map((p) => `<span class="team-pick">${face(p, 'face-sm')}<b>${p.winrate.toFixed(1)}</b></span>`).join('')
      || '<span class="team-none">无候选</span>';
    if (picked) {
      return `<div class="team-row is-ally-pick" title="${esc(`队友打${t.name}可以选这些`)}">`
        + `<span class="team-label">${esc(t.name)}</span><div class="team-picks">${picks}</div></div>`;
    }
    const tip = cur ? `你现在按${t.name}推荐` : `点一下：改按${t.name}推荐`;
    return `<div class="team-row is-open ${cur ? 'is-cur' : ''} ${t.best ? 'is-best' : ''}" data-pos="${t.position}" title="${esc(tip)}">`
      + `<span class="team-label">${esc(t.name)}</span><div class="team-picks">${picks}</div>${flags}</div>`;
  }).join('');
  const [hint, small] = picked
    ? ['队友占了哪几个位置：选人界面认出来的优先，其余按常打位置猜；空位置后面是给队友的推荐（算上和你的配合）', '给队友的推荐']
    : ['队友占了哪几个位置：选人界面认出来的优先，其余按常打位置猜；空位置后面是你打这个位置的前两个推荐', '点空位置切换'];
  return `
    <section class="ov-team" title="${hint}">
      <h3>剩余位置<small>${small}</small></h3>${rows}
    </section>`;
}

function card(item, rank) {
  const tags = item.tags.map((t) => `<span class="tag tag-${t.kind}">${esc(t.text)}</span>`).join('');
  // 机制上为什么克 / 怕（知识库原句放在悬停提示里）
  const why = (item.why || []).map((w) => `<p class="why why-${esc(w.kind)}" title="${esc(w.quote)}">${w.kind === 'good' ? '✓' : '✗'} ${esc(w.text)}</p>`).join('');
  return `
    <div class="pick ${rank === 0 ? 'pick-top' : ''}" data-id="${item.id}" title="点一下：看选${esc(item.name)}怎么打这套阵容">
      <div class="pick-art"><img src="${portrait(item.id)}" alt="" onerror="this.remove()"></div>
      <div class="pick-body">
        <div class="pick-head">
          <span class="pick-name">${esc(item.name)}</span>
          <span class="pick-wr">${item.winrate.toFixed(1)}<small>%</small></span>
        </div>
        <div class="pick-bar"><i style="width:${barPct(item.winrate)}%"></i></div>
        <div class="pick-tags">${tags}</div>
        ${vsRow(item)}
        ${why ? `<div class="pick-why">${why}</div>` : ''}
      </div>
    </div>`;
}

// 内容分两栏：A = 阵容 + 分路，B = 我这路怎么打 / 推荐 / 威胁。窄的时候上下接着排（顺序不变），
// 叠加层拉宽到 640px 以上左右并排（base.css 的容器查询），整张卡片矮一半，不容易超出屏幕
function draftHTML(v) {
  const sideCls = v.side === 'radiant' ? 'side-radiant' : v.side === 'dire' ? 'side-dire' : 'side-unknown';
  const head = `
    <header class="ov-head">
      <span class="ov-pos">${esc(v.position_name)}</span>${v.position_auto ? '<span class="ov-auto" title="从选人界面名字下面的位置自动认出来的">自动</span>' : ''}
      <span class="ov-side ${sideCls}">${esc(v.side_name)}</span>
      <span class="ov-brand">选人助手</span>
    </header>`;
  let a = '';
  let html = '';
  const lines = lineup('敌', v.enemies, 'lineup-enemy') + lineup('友', v.allies, 'lineup-ally') + lineup('禁', v.bans || [], 'lineup-ban');
  if (lines) a += `<section class="ov-lineups">${lines}</section>`;
  a += teamHTML(v.team, !!v.my_hero);
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
    html += `<section class="ov-group"><h3>${esc(g.title)}</h3>${g.items.map((it) => card(it, rank++)).join('')}</section>`;
  }
  if (v.groups.length) html += `<div class="ov-tip">点推荐英雄看「选它怎么打」</div>`;
  if (!v.groups.length && !v.my_hero) {
    html += `<div class="ov-empty">暂无推荐（数据没拉或者位置下没有候选）</div>`;
  }
  if (v.threat) {
    html += `<div class="ov-threat">${face(v.threat, 'face-sm')}<span>注意 <b>${esc(v.threat.name)}</b> 对我方威胁最大</span><em>${v.threat.delta > 0 ? '+' : ''}${v.threat.delta.toFixed(1)}%</em></div>`;
  }
  if (!a || !html) return head + a + html;           // 只有一栏的内容，不用分
  return `${head}<div class="ov-cols"><div class="ov-col">${a}</div><div class="ov-col">${html}</div></div>`;
}

// closable：局内提醒，带关闭按钮（点它或按显隐热键关掉）
function noticeHTML(v) {
  const close = v.closable ? '<button class="ov-close" type="button" title="关闭这条提醒">×</button>' : '';
  return `
    <header class="ov-head"><span class="ov-pos">${esc(v.title)}</span><span class="ov-brand">选人助手</span>${close}</header>
    <section class="ov-notice">${v.lines.map((l) => `<p class="notice-${esc(l.kind)}">${esc(l.text)}</p>`).join('')}</section>`;
}

// 本局要点：两页（数据：分路 + 对线 / 起手 / 强势期 / 对面要防 / 关键装备；打法：AI 写的前中后期），热键翻页
function briefHTML(v) {
  const close = v.closable ? '<button class="ov-close" type="button" title="关闭">×</button>' : '';
  const tabs = (v.tabs || []).map((t) => `<span class="brief-tab ${t.on ? 'is-on' : ''}" data-page="${esc(t.page)}">${esc(t.label)}</span>`).join('');
  const sections = v.sections.map((s) => `
    <section class="brief-sec"><h3>${esc(s.title)}</h3>
      ${s.lines.map((l) => `<p class="brief-${esc(l.kind)}"${l.quote ? ` title="${esc(l.quote)}"` : ''}>${esc(l.text)}${l.note ? `<small>${esc(l.note)}</small>` : ''}</p>`).join('')}</section>`).join('');
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

export function renderOverlay(root, view) {
  const html = !view || !view.kind ? '<div class="ov-empty">等待助手…</div>'
    : view.kind === 'notice' ? noticeHTML(view) : view.kind === 'brief' ? briefHTML(view) : draftHTML(view);
  if (root._html === html) return false;
  root._html = html;
  root.innerHTML = html;
  return true;
}

// 叠加层和控制面板预览共用的点击：推荐英雄 → 「如果选它」，剩余位置 → 切位置，要点卡片的标签 → 翻页，× → 关卡片
export function bindOverlayClicks(root, act) {
  root.addEventListener('click', (e) => {
    const tab = e.target.closest('.brief-tab[data-page]');
    const pos = e.target.closest('.team-row[data-pos]');
    if (pos) return act('set', { key: 'position', value: Number(pos.dataset.pos) });
    if (tab) return act('brief_page', { page: tab.dataset.page });
    const pick = e.target.closest('.pick[data-id]');
    if (pick) return act('explain_pick', { hero_id: Number(pick.dataset.id) });
    if (e.target.closest('.ov-close')) act('dismiss_coach');
  });
}
