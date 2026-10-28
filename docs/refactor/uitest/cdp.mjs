// node cdp.mjs <页面关键字> "<表达式>"：在 Electron 某个页面里求值（只读，点击用真鼠标）
const targets = await (await fetch('http://127.0.0.1:9334/json')).json();
const page = targets.find((t) => t.type === 'page' && t.url.includes(process.argv[2]));
if (!page) { console.log(JSON.stringify({ err: 'no page', urls: targets.map((t) => t.url) })); process.exit(0); }
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r));
ws.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { expression: process.argv[3], returnByValue: true } }));
const m = await new Promise((r) => ws.addEventListener('message', (e) => r(JSON.parse(e.data))));
console.log(JSON.stringify(m.result?.result?.value ?? m.result));
ws.close();
