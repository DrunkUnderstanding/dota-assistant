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

function card(item, rank) {
  const tags = item.tags.map((t) => `<span class="tag tag-${t.kind}">${esc(t.text)}</span>`).join('');
  return `
    <div class="pick ${rank === 0 ? 'pick-top' : ''}" data-id="${item.id}">
      <div class="pick-art"><img src="${portrait(item.id)}" alt="" onerror="this.remove()"></div>
      <div class="pick-body">
        <div class="pick-head">
          <span class="pick-name">${esc(item.name)}</span>
          <span class="pick-wr">${item.winrate.toFixed(1)}<small>%</small></span>
        </div>
        <div class="pick-bar"><i style="width:${barPct(item.winrate)}%"></i></div>
        <div class="pick-tags">${tags}</div>
      </div>
    </div>`;
}

function draftHTML(v) {
  const sideCls = v.side === 'radiant' ? 'side-radiant' : v.side === 'dire' ? 'side-dire' : 'side-unknown';
  let html = `
    <header class="ov-head">
      <span class="ov-pos">${esc(v.position_name)}</span>
      <span class="ov-side ${sideCls}">${esc(v.side_name)}</span>
      <span class="ov-brand">选人助手</span>
    </header>`;
  const lines = lineup('敌', v.enemies, 'lineup-enemy') + lineup('友', v.allies, 'lineup-ally');
  if (lines) html += `<section class="ov-lineups">${lines}</section>`;
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
  if (!v.groups.length && !v.my_hero) {
    html += `<div class="ov-empty">暂无推荐（数据没拉或者位置下没有候选）</div>`;
  }
  if (v.threat) {
    html += `<div class="ov-threat">${face(v.threat, 'face-sm')}<span>注意 <b>${esc(v.threat.name)}</b> 对我方威胁最大</span><em>${v.threat.delta > 0 ? '+' : ''}${v.threat.delta.toFixed(1)}%</em></div>`;
  }
  return html;
}

// closable：局内提醒，带关闭按钮（点它或按显隐热键关掉）
function noticeHTML(v) {
  const close = v.closable ? '<button class="ov-close" type="button" title="关闭这条提醒">×</button>' : '';
  return `
    <header class="ov-head"><span class="ov-pos">${esc(v.title)}</span><span class="ov-brand">选人助手</span>${close}</header>
    <section class="ov-notice">${v.lines.map((l) => `<p class="notice-${esc(l.kind)}">${esc(l.text)}</p>`).join('')}</section>`;
}

// 本局要点：两页（数据：对线 / 起手 / 强势期 / 对面要防 / 关键装备；打法：AI 写的前中后期），热键翻页
function briefHTML(v) {
  const close = v.closable ? '<button class="ov-close" type="button" title="关闭">×</button>' : '';
  const tabs = (v.tabs || []).map((t) => `<span class="brief-tab ${t.on ? 'is-on' : ''}">${esc(t.label)}</span>`).join('');
  const sections = v.sections.map((s) => `
    <section class="brief-sec"><h3>${esc(s.title)}</h3>
      ${s.lines.map((l) => `<p class="brief-${esc(l.kind)}">${esc(l.text)}${l.note ? `<small>${esc(l.note)}</small>` : ''}</p>`).join('')}</section>`).join('');
  return `
    <header class="ov-head"><span class="ov-pos">${esc(v.title)}</span><span class="ov-brand">选人助手</span>${close}</header>
    ${tabs ? `<div class="brief-tabs">${tabs}<em>${esc(v.hint || '')}</em></div>` : ''}
    ${sections}`;
}

// 只在内容真的变了时重画，避免动画每秒重放
export function renderOverlay(root, view) {
  const html = !view || !view.kind ? '<div class="ov-empty">等待助手…</div>'
    : view.kind === 'notice' ? noticeHTML(view) : view.kind === 'brief' ? briefHTML(view) : draftHTML(view);
  if (root._html === html) return false;
  root._html = html;
  root.innerHTML = html;
  return true;
}
