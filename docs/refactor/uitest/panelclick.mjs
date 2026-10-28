// node panelclick.mjs "<选择器>"：现读位置马上真鼠标点（控制面板客户区原点 398,157）
import { execFileSync } from 'node:child_process';
const targets = await (await fetch('http://127.0.0.1:9334/json')).json();
const page = targets.find((t) => t.type === 'page' && t.url.includes('panel'));
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r));
let id = 0;
const ev = (expr) => new Promise((res) => {
  const my = ++id;
  const h = (e) => { const m = JSON.parse(e.data); if (m.id === my) { ws.removeEventListener('message', h); res(m.result?.result?.value); } };
  ws.addEventListener('message', h);
  ws.send(JSON.stringify({ id: my, method: 'Runtime.evaluate', params: { expression: expr, returnByValue: true } }));
});
await ev('window.__clicks=[];document.addEventListener("click",e=>window.__clicks.push([e.target.tagName,(e.target.innerText||"").slice(0,6)]),true);1');
const [x, y] = await ev(`(()=>{const r=document.querySelector(${JSON.stringify(process.argv[2])}).getBoundingClientRect();return [Math.round(398+r.x+r.width/2),Math.round(157+r.y+r.height/2)]})()`);
console.log(execFileSync('python', ['mouse.py', 'click', String(x), String(y)], { encoding: 'utf-8' }).trim());
await new Promise((r) => setTimeout(r, 1500));
console.log(await ev('JSON.stringify(window.__clicks)'), await ev('document.querySelector("#mates").innerText.slice(0,24)'));
ws.close();
