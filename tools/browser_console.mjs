// 无头浏览器里打开几个页面，收控制台报错（重构门禁 G6，由 tools/browser_check.py 调用）。
// 用法：node tools/browser_console.mjs <调试端口> <秒数> <url> [url ...]
// 输出一行 JSON：[{url, errors: [...], warnings: [...], text: 页面文字长度, sample: 开头一段文字}]
const [port, seconds, ...urls] = process.argv.slice(2);
const base = `http://127.0.0.1:${port}`;

async function openPage(url) {
  const t = await (await fetch(`${base}/json/new?about:blank`, { method: 'PUT' })).json();
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener('open', r));
  const page = { url, errors: [], warnings: [], text: 0, ws, id: 0, waits: new Map() };
  page.send = (method, params = {}) => new Promise((resolve) => {
    const id = ++page.id;
    page.waits.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
  ws.addEventListener('message', (e) => {
    const m = JSON.parse(e.data);
    if (m.id && page.waits.has(m.id)) { page.waits.get(m.id)(m.result ?? m.error); page.waits.delete(m.id); return; }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails;
      page.errors.push(`未捕获异常：${d.exception?.description ?? d.text}（${d.url ?? ''}:${d.lineNumber}）`);
    } else if (m.method === 'Runtime.consoleAPICalled') {
      const text = m.params.args.map((a) => a.value ?? a.description ?? a.type).join(' ');
      if (m.params.type === 'error' || m.params.type === 'assert') page.errors.push(`console.${m.params.type}：${text}`);
      else if (m.params.type === 'warning') page.warnings.push(`console.warn：${text}`);
    } else if (m.method === 'Log.entryAdded') {
      const en = m.params.entry;
      const line = `${en.source}：${en.text}${en.url ? `（${en.url}）` : ''}`;
      if (en.level === 'error') page.errors.push(line);
      else if (en.level === 'warning') page.warnings.push(line);
    }
  });
  await page.send('Runtime.enable');
  await page.send('Log.enable');
  await page.send('Page.enable');
  await page.send('Page.navigate', { url });
  return page;
}

const pages = [];
for (const url of urls) pages.push(await openPage(url));
await new Promise((r) => setTimeout(r, Number(seconds) * 1000));
for (const p of pages) {
  const r = await p.send('Runtime.evaluate', { expression: 'document.body ? document.body.innerText : null', returnByValue: true });
  const body = r?.result?.value;
  p.text = typeof body === 'string' ? body.length : -1;
  p.sample = typeof body === 'string' ? body.replace(/\s+/g, ' ').slice(0, 160) : '';
  p.ws.close();
}
console.log(JSON.stringify(pages.map(({ url, errors, warnings, text, sample }) => ({ url, errors, warnings, text, sample }))));
process.exit(0);
