// 界面外壳：由 python -m dota_assistant run 启动，参数 --server=http://127.0.0.1:端口。
// 只开两个窗口加载 Python 服务的页面，逻辑全在 Python 那边。
//   叠加层：透明、置顶、鼠标穿透、不抢焦点。它是一个普通的桌面窗口，盖在「无边框窗口」模式的 Dota 上面，
//           不注入游戏、不读游戏内存（独占全屏下看不到，这是 Windows 的限制）。
//           卡片右上角有把锁：点开（或按 F7）后鼠标移到卡片上就能按住拖动、按住右边缘的拉手改宽度，再点一下锁固定、恢复穿透；
//           拖完把位置和宽度报给 Python 存起来。窗口始终收在屏幕里面。
//           固定状态下只有按钮（锁、×）接点击，其余地方照样穿透。
//           「哪里接鼠标」由页面报可点区域，主进程每 30ms 看系统光标在不在区域里来切换穿透。
//           不靠页面的 mouseover / mouseout：窗口一从穿透切到接鼠标，Chromium 就会补发一次 mouseout，
//           页面以为鼠标离开了按钮又切回穿透，按钮永远点不到（2026-10-22 实测出的问题）。
//   控制面板：普通窗口，关掉它 = 退出助手。
// 日志：往 stdout 写「级别 内容」一行，Python（webui._pipe_electron）收进 logs/ 下的日志文件；页面脚本报错也转过去。
const path = require('path');
// 页面是本机服务的固定文件，不需要 Electron 开发模式的 CSP 安全提示（打包后本来也不显示），免得每次启动刷两条警告
process.env.ELECTRON_DISABLE_SECURITY_WARNINGS = 'true';
const { app, BrowserWindow, ipcMain, screen } = require('electron');

const arg = process.argv.find((a) => a.startsWith('--server='));
const SERVER = arg ? arg.slice('--server='.length) : 'http://127.0.0.1:8765';

let overlay = null;
let panel = null;
let display = null;      // 叠加层所在的屏（config 里的 monitor），位置按它的比例存
let locked = true;       // 固定：只有锁和 × 接鼠标；解锁：整张卡片都接（好拖动）
let drag = null;         // 拖动中：{ dx, dy, timer }
let resize = null;       // 改宽度中：{ dx, timer }
const MIN_WIDTH = 340;   // 再窄推荐卡片就挤不下了
let hitRects = [];       // 页面报来的可点区域（窗口内 CSS 像素 = 逻辑像素）：固定时是锁和 ×，解锁时是整张卡片
let grabbing = false;    // 窗口现在是否接鼠标

function log(level, ...parts) {
  const text = parts.map((p) => (typeof p === 'string' ? p : JSON.stringify(p))).join(' ');
  try {
    process.stdout.write(`${level} ${text.replace(/\r?\n/g, ' | ')}\n`);
  } catch {
    // Python 那边已经退出了，没处写
  }
}

process.on('uncaughtException', (err) => log('ERROR', '主进程出错', err && err.stack ? err.stack : String(err)));

// 页面里的报错 / 警告、页面崩溃、加载失败都记下来（开着界面时看不到控制台）
function watch(win, name) {
  const wc = win.webContents;
  // 只收一个参数：多写参数 Electron 会报「console-message arguments are deprecated」
  wc.on('console-message', (e) => {
    if (e.level === 'warning' || e.level === 'error') {
      log(e.level === 'error' ? 'ERROR' : 'WARN', `[${name}] ${e.message} (${e.sourceId}:${e.lineNumber})`);
    }
  });
  wc.on('render-process-gone', (_e, d) => log('ERROR', `[${name}] 页面进程没了：${d.reason}（${d.exitCode}）`));
  wc.on('did-fail-load', (_e, code, desc, url) => log('ERROR', `[${name}] 页面加载失败 ${code} ${desc} ${url}`));
  win.on('unresponsive', () => log('WARN', `[${name}] 窗口没响应`));
  win.on('responsive', () => log('INFO', `[${name}] 窗口恢复响应`));
}

async function loadConfig() {
  try {
    const res = await fetch(`${SERVER}/api/config`);
    return await res.json();
  } catch (err) {
    log('WARN', '读不到叠加层配置，用默认值', String(err));
    return { monitor: 1, x: 0.8, y: 0.22, width: 420, opacity: 0.85 };
  }
}

function pickDisplay(monitor) {
  // 和 mss 的编号对齐：1 = 主屏，2、3… 按 Electron 列出的顺序（多屏时以实测为准）
  const all = screen.getAllDisplays();
  // 多屏 + 不同缩放没实测过，把每块屏的位置和缩放记下来，对不上时看日志
  all.forEach((d, i) => log('INFO', `屏幕 ${i + 1}：${d.bounds.width}x${d.bounds.height} @ (${d.bounds.x},${d.bounds.y})，缩放 ${d.scaleFactor}${d.id === screen.getPrimaryDisplay().id ? '（主屏）' : ''}`));
  if (monitor > all.length) log('WARN', `config 里 monitor = ${monitor}，但只有 ${all.length} 块屏，改用主屏`);
  if (monitor <= 1 || monitor > all.length) return screen.getPrimaryDisplay();
  return all[monitor - 1];
}

// 高度给足到屏幕底部，内容多高就画多高；空白部分完全透明且鼠标穿透，不影响操作
function fitHeight(y) {
  const { bounds } = display;
  return Math.max(200, bounds.y + bounds.height - y - 24);
}

const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));

// 窗口收回屏幕里：宽度不超过屏幕，左右不出界，高度到屏幕底
function fitBounds(x, y, width) {
  const { bounds } = display;
  const w = clamp(Math.round(width), MIN_WIDTH, bounds.width - 16);
  const nx = clamp(Math.round(x), bounds.x, bounds.x + bounds.width - w);
  const ny = clamp(Math.round(y), bounds.y, bounds.y + bounds.height - 160);
  return { x: nx, y: ny, width: w, height: fitHeight(ny) };
}

function createOverlay(cfg) {
  display = pickDisplay(cfg.monitor);
  const { bounds } = display;
  // 按比例算出的位置可能让窗口伸出屏幕（高 DPI 缩放下逻辑宽度变小，比如 4K 200% 只有 1920），往回收到屏幕内
  const { x, y, width, height } = fitBounds(bounds.x + bounds.width * cfg.x, bounds.y + bounds.height * cfg.y, cfg.width);
  overlay = new BrowserWindow({
    x, y, width, height,
    transparent: true, frame: false, resizable: false, movable: false, focusable: false,
    skipTaskbar: true, hasShadow: false, show: false, alwaysOnTop: true,
    backgroundColor: '#00000000',
    webPreferences: { backgroundThrottling: false, preload: path.join(__dirname, 'preload.js') },
  });
  log('INFO', `叠加层：(${x},${y}) ${width}x${height}，配置 ${JSON.stringify(cfg)}`);
  watch(overlay, '叠加层');
  overlay.setAlwaysOnTop(true, 'screen-saver');
  // 叠加层不进任何截图（Windows 10 2004+ 走 WDA_EXCLUDEFROMCAPTURE）：
  // 找不到 Dota 窗口、退回整屏截图时也不会把叠加层截进去挡住英雄格；代价是 OBS / Win+Shift+S 也截不到它
  overlay.setContentProtection(true);
  overlay.setIgnoreMouseEvents(true);
  grabbing = false;
  const hitTimer = setInterval(applyMouse, 30);
  overlay.loadURL(`${SERVER}/overlay.html`);
  overlay.once('ready-to-show', () => overlay.showInactive());
  // 游戏切回前台时可能把叠加层压下去，定期提到最上层（不抢焦点）
  const timer = setInterval(() => {
    if (overlay && !overlay.isDestroyed()) overlay.moveTop();
  }, 3000);
  overlay.on('closed', () => { clearInterval(timer); clearInterval(hitTimer); stopDrag(false); stopResize(false); overlay = null; });
}

// ---------- 解锁 / 拖动 ----------

function cursorOnHitRects() {
  const c = screen.getCursorScreenPoint();
  const b = overlay.getBounds();
  const x = c.x - b.x;
  const y = c.y - b.y;
  return hitRects.some((r) => x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h);
}

function applyMouse() {
  if (!overlay || overlay.isDestroyed()) return;
  const want = !!drag || !!resize || cursorOnHitRects();
  if (want === grabbing) return;
  grabbing = want;
  overlay.setIgnoreMouseEvents(!want);
}

function startDrag() {
  if (locked || drag || resize || !overlay) return;
  const c = screen.getCursorScreenPoint();
  const [wx, wy] = overlay.getPosition();
  drag = { dx: c.x - wx, dy: c.y - wy, timer: null };
  // 窗口跟着系统光标走（不用页面里的坐标，免得高 DPI 下换算出错）
  drag.timer = setInterval(() => {
    if (!overlay || overlay.isDestroyed()) return;
    const p = screen.getCursorScreenPoint();
    overlay.setPosition(p.x - drag.dx, p.y - drag.dy);
  }, 16);
  applyMouse();
}

function stopDrag(save = true) {
  if (!drag) return;
  clearInterval(drag.timer);
  drag = null;
  if (save) saveBounds('拖到');
}

// 拖动 / 改宽度结束：收回屏幕里，把位置（占屏幕的比例）和宽度报给 Python 存起来
function saveBounds(what) {
  if (!overlay || overlay.isDestroyed()) return;
  const cur = overlay.getBounds();
  const b = fitBounds(cur.x, cur.y, cur.width);
  overlay.setBounds(b);
  applyMouse();
  const { bounds } = display;
  log('INFO', `叠加层${what} (${b.x},${b.y}) 宽 ${b.width}`);
  fetch(`${SERVER}/api/action`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'overlay_moved', x: (b.x - bounds.x) / bounds.width, y: (b.y - bounds.y) / bounds.height,
                           width: b.width }),
  }).catch((err) => log('WARN', '叠加层位置没存上', String(err)));
}

// 改宽度：窗口右边缘跟着光标走，左边不动；不超出屏幕右边
function startResize() {
  if (locked || drag || resize || !overlay) return;
  const c = screen.getCursorScreenPoint();
  const b = overlay.getBounds();
  resize = { dx: b.x + b.width - c.x, timer: null };
  resize.timer = setInterval(() => {
    if (!overlay || overlay.isDestroyed()) return;
    const p = screen.getCursorScreenPoint();
    const cur = overlay.getBounds();
    const { bounds } = display;
    const w = clamp(Math.round(p.x + resize.dx - cur.x), MIN_WIDTH, bounds.x + bounds.width - cur.x);
    if (w !== cur.width) overlay.setBounds({ ...cur, width: w });
  }, 16);
  applyMouse();
}

function stopResize(save = true) {
  if (!resize) return;
  clearInterval(resize.timer);
  resize = null;
  if (save) saveBounds('改宽度');
}

const fromOverlay = (e) => overlay && !overlay.isDestroyed() && e.sender === overlay.webContents;

ipcMain.on('overlay:locked', (e, v) => {
  if (!fromOverlay(e) || v === locked) return;
  locked = v;
  log('INFO', locked ? '叠加层固定' : '叠加层解锁');
  if (locked) { stopDrag(); stopResize(); }
});
ipcMain.on('overlay:hit-rects', (e, rects) => {
  if (!fromOverlay(e) || !Array.isArray(rects)) return;
  hitRects = rects.filter((r) => [r.x, r.y, r.w, r.h].every(Number.isFinite));
  applyMouse();
});
ipcMain.on('overlay:drag', (e, on) => {
  if (!fromOverlay(e)) return;
  if (on) startDrag(); else stopDrag();
});
ipcMain.on('overlay:resize', (e, on) => {
  if (!fromOverlay(e)) return;
  if (on) startResize(); else stopResize();
});

function createPanel() {
  panel = new BrowserWindow({
    width: 1140, height: 780, minWidth: 900, minHeight: 600,
    title: 'Dota 选人助手 · 控制面板',
    backgroundColor: '#0b0e13',
    autoHideMenuBar: true,
    show: false,
  });
  watch(panel, '控制面板');
  panel.loadURL(`${SERVER}/panel.html`);
  panel.once('ready-to-show', () => panel.show());
  panel.on('closed', () => { panel = null; app.quit(); });
}

app.whenReady().then(async () => {
  log('INFO', `Electron ${process.versions.electron} 启动，服务 ${SERVER}`);
  const cfg = await loadConfig();
  createOverlay(cfg);
  createPanel();
});

app.on('window-all-closed', () => app.quit());
