// 顶栏帮认整条：真鼠标点「?」→ 力量 → 最后一个英雄（菜单最下面一行，看 410 高的窗口够不够）
import { execFileSync } from 'node:child_process';
const targets = await (await fetch('http://127.0.0.1:9334/json')).json();
const page = targets.find((t) => t.type === 'page' && t.url.includes('topbar'));
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r));
let id = 0;
const ev = (expr) => new Promise((res) => {
  const my = ++id;
  const h = (e) => { const m = JSON.parse(e.data); if (m.id === my) { ws.removeEventListener('message', h); res(m.result?.result?.value); } };
  ws.addEventListener('message', h);
  ws.send(JSON.stringify({ id: my, method: 'Runtime.evaluate', params: { expression: expr, returnByValue: true } }));
});
const click = (x, y) => console.log(execFileSync('python', ['mouse.py', 'click', String(x), String(y)], { encoding: 'utf-8' }).trim());
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const pos = (sel) => `[...document.querySelectorAll('${sel}')].filter(e=>e.getClientRects().length).map(e=>{const r=e.getBoundingClientRect();return [e.innerText.slice(0,8)||e.title,Math.round(screenX+r.x+r.width/2),Math.round(screenY+r.y+r.height/2),Math.round(r.bottom)]})`;
const ask = (await ev(pos('.bb.ask')))[0];
console.log('ask', ask); click(ask[1], ask[2]); await wait(400);
const tabs = await ev(pos('.menu .tabs button'));
console.log('tabs', tabs.map((t) => t[0]).join(' ')); click(tabs[0][1], tabs[0][2]); await wait(400);
const heroes = await ev(pos('.menu .picks button'));
const menuBottom = await ev(`Math.round(document.querySelector('.menu').getBoundingClientRect().bottom)`);
console.log('heroes', heroes.length, 'menuBottom', menuBottom, 'innerHeight', await ev('innerHeight'));
const last = heroes[heroes.length - 1];
console.log('pick', last); click(last[1], last[2]); await wait(1200);
console.log('after', await ev(`({menu: !document.querySelector('.menu').hidden, ask: ${pos('.bb.ask')}.length})`));
ws.close();
