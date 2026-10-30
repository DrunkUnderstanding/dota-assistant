// 界面外壳：由 python -m dota_assistant run 启动，参数 --server=http://127.0.0.1:端口。
// 只开三个窗口加载 Python 服务的页面，逻辑全在 Python 那边。
//   叠加层：透明、置顶、鼠标穿透、不抢焦点。它是一个普通的桌面窗口，盖在「无边框窗口」模式的 Dota 上面，
//           不注入游戏、不读游戏内存（独占全屏下看不到，这是 Windows 的限制）。
//           卡片右上角有把锁：点开（或按 F7）后整个窗口显示成一个金色虚线框，按住框里任意处拖动（能拖到别的屏），
//           按住右边 / 下边 / 右下角的拉手改宽 / 高 / 两样一起，再点一下锁固定、恢复穿透；
//           拖完把位置、大小和所在的屏报给 Python 存起来。窗口始终收在它所在的那块屏里面（松手时光标在哪块屏就算哪块）。
//           固定状态下只有按钮（锁、×）接点击，其余地方照样穿透。
//           「哪里接鼠标」由页面报可点区域，主进程每 30ms 看系统光标在不在区域里来切换穿透。
//           不靠页面的 mouseover / mouseout：窗口一从穿透切到接鼠标，Chromium 就会补发一次 mouseout，
//           页面以为鼠标离开了按钮又切回穿透，按钮永远点不到（2026-10-22 实测出的问题）。
//   买活 / 大招计时条：和叠加层一样透明、置顶、不抢焦点，盖在屏幕最上面一条（游戏顶栏），每个英雄头像下面一个买活小条、对面的再加一个大招小条；
//           只有小条接鼠标（页面报区域，同上），不能拖，位置按屏幕高度换算（web/topbar.html）。
//   控制面板：普通窗口，关掉它 = 退出助手。
//   队友战绩：控制面板点「队友战绩」window.open 开的普通窗口（web/mates.html），可以单独关。
// 日志：往 stdout 写「级别 内容」一行，Python（webui._pipe_electron）收进 logs/ 下的日志文件；页面脚本报错也转过去。
const path = require('path');
// 页面是本机服务的固定文件，不需要 Electron 开发模式的 CSP 安全提示（打包后本来也不显示），免得每次启动刷两条警告
process.env.ELECTRON_DISABLE_SECURITY_WARNINGS = 'true';
const { app, BrowserWindow, ipcMain, screen } = require('electron');

const arg = process.argv.find((a) => a.startsWith('--server='));
const SERVER = arg ? arg.slice('--server='.length) : 'http://127.0.0.1:8765';

let overlay = null;
let panel = null;
let display = null;      // 叠加层所在的屏：默认 config 里的 monitor，拖到别的屏就换成那块（存那块屏左上角，下次启动找回来）；位置按它的比例存
let gameDisplay = null;  // Dota 所在的屏（config 里的 monitor），买活 / 大招计时条放这块，不跟着叠加层走
let userHeight = null;   // 拉过的高度；null = 没拉过，窗口到屏幕底
let locked = true;       // 固定：只有锁和 × 接鼠标；解锁：整张卡片都接（好拖动）
let drag = null;         // 拖动中：{ dx, dy, timer }
let resize = null;       // 改大小中：{ mode: 'w' 宽 | 'h' 高 | 'wh' 都改, dx, dy, timer }
const MIN_WIDTH = 300;   // 再窄推荐卡片就挤不下了（Python 那边 consts.OVERLAY_WIDTH 同一个下限）
const MIN_HEIGHT = 160;  // 再矮卡片缩到最小也放不下几行
let hitRects = [];       // 页面报来的可点区域（窗口内 CSS 像素 = 逻辑像素）：固定时是锁和 ×，解锁时是整个窗口
let grabbing = false;    // 窗口现在是否接鼠标
let topbar = null;       // 买活计时条
let topbarRects = [];    // 它的可点区域（小条）
let topbarGrabbing = false;
const TOPBAR_HEIGHT = 410; // 买活 / 大招计时条窗口高（1080 高的屏幕上；Dota 顶栏头像高 40、下面 40~66 是复活倒计时，买活小条在 69~93、
                          // 对面大招小条在 96~120，右键大招小条弹的菜单从 124 往下到约 305，挑英雄菜单最长的力量列 6 行到约 390）；
                          // 只有小条和打开的菜单接鼠标，其余透明点穿

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

// 叠加层放哪块屏：上次拖到的那块（按左上角坐标找）还在就用它，否则放 Dota 那块
function overlayDisplay(cfg) {
  const s = cfg.screen;
  if (!s) return gameDisplay;
  const found = screen.getAllDisplays().find((d) => d.bounds.x === s.x && d.bounds.y === s.y);
  if (!found) log('WARN', `上次叠加层在左上角 (${s.x},${s.y}) 的那块屏，现在没有了，放回 monitor = ${cfg.monitor}`);
  return found || gameDisplay;
}

// 高度给足到屏幕底部，内容多高就画多高；空白部分完全透明且鼠标穿透，不影响操作
function fitHeight(y) {
  const { bounds } = display;
  return Math.max(200, bounds.y + bounds.height - y - 24);
}

const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));

// 窗口收回所在的屏里：宽高不超过屏幕，上下左右不出界；没拉过高度就到屏幕底
function fitBounds(x, y, width, height = userHeight) {
  const { bounds } = display;
  const w = clamp(Math.round(width), MIN_WIDTH, bounds.width - 16);
  const nx = clamp(Math.round(x), bounds.x, bounds.x + bounds.width - w);
  if (height == null) {
    const ny = clamp(Math.round(y), bounds.y, bounds.y + bounds.height - 160);
    return { x: nx, y: ny, width: w, height: fitHeight(ny) };
  }
  const h = clamp(Math.round(height), MIN_HEIGHT, bounds.height);
  const ny = clamp(Math.round(y), bounds.y, bounds.y + bounds.height - h);
  return { x: nx, y: ny, width: w, height: h };
}

function createOverlay(cfg) {
  display = overlayDisplay(cfg);
  userHeight = Number.isFinite(cfg.height) ? cfg.height : null;
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

// 买活计时条：整屏宽、顶上一条。Dota 界面按屏幕高度缩放，scale 传给页面换算头像位置
function createTopbar() {
  const { bounds } = gameDisplay;
  const scale = bounds.height / 1080;
  const height = Math.round(TOPBAR_HEIGHT * scale);
  topbar = new BrowserWindow({
    x: bounds.x, y: bounds.y, width: bounds.width, height,
    transparent: true, frame: false, resizable: false, movable: false, focusable: false,
    skipTaskbar: true, hasShadow: false, show: false, alwaysOnTop: true,
    backgroundColor: '#00000000',
    webPreferences: { backgroundThrottling: false, preload: path.join(__dirname, 'preload.js') },
  });
  log('INFO', `买活计时条：(${bounds.x},${bounds.y}) ${bounds.width}x${height}，缩放 ${scale.toFixed(3)}`);
  watch(topbar, '买活计时');
  topbar.setAlwaysOnTop(true, 'screen-saver');
  topbar.setContentProtection(true);       // 同叠加层：不进截图
  topbar.setIgnoreMouseEvents(true);
  topbarGrabbing = false;
  const hitTimer = setInterval(applyTopbarMouse, 30);
  topbar.loadURL(`${SERVER}/topbar.html?scale=${scale}`);
  topbar.once('ready-to-show', () => topbar.showInactive());
  const timer = setInterval(() => {
    if (topbar && !topbar.isDestroyed()) topbar.moveTop();
  }, 3000);
  topbar.on('closed', () => { clearInterval(timer); clearInterval(hitTimer); topbar = null; });
}

function applyTopbarMouse() {
  if (!topbar || topbar.isDestroyed()) return;
  const c = screen.getCursorScreenPoint();
  const b = topbar.getBounds();
  const want = topbarRects.some((r) => c.x - b.x >= r.x && c.x - b.x < r.x + r.w && c.y - b.y >= r.y && c.y - b.y < r.y + r.h);
  if (want === topbarGrabbing) return;
  topbarGrabbing = want;
  topbar.setIgnoreMouseEvents(!want);
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
  const b = overlay.getBounds();
  drag = { dx: c.x - b.x, dy: c.y - b.y, width: b.width, height: b.height, timer: null };
  // 窗口跟着系统光标走（不用页面里的坐标，免得高 DPI 下换算出错）；拖动中不收进屏幕，能跨到别的屏。
  // 宽高跟位置一起设：拖过缩放不同的两块屏时 Windows 会按新屏的 DPI 改窗口大小，固定住免得越拖越大 / 越小
  drag.timer = setInterval(() => {
    if (!overlay || overlay.isDestroyed()) return;
    const p = screen.getCursorScreenPoint();
    overlay.setBounds({ x: p.x - drag.dx, y: p.y - drag.dy, width: drag.width, height: drag.height });
  }, 16);
  applyMouse();
}

function stopDrag(save = true) {
  if (!drag) return;
  clearInterval(drag.timer);
  const { width } = drag;
  drag = null;
  if (!save || !overlay || overlay.isDestroyed()) return;
  // 松手时光标在哪块屏，叠加层就放哪块屏（以前固定收回 monitor 那块，拖到副屏一松手就弹回来）
  const now = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  if (now.id !== display.id) {
    const b = now.bounds;
    log('INFO', `叠加层拖到另一块屏：${b.width}x${b.height} @ (${b.x},${b.y})，缩放 ${now.scaleFactor}`);
    display = now;
  }
  saveBounds('拖到', width);
}

// 拖动 / 改大小结束：收回所在的屏里，把位置（占那块屏的比例）、宽高和那块屏报给 Python 存起来
function saveBounds(what, width) {
  if (!overlay || overlay.isDestroyed()) return;
  const cur = overlay.getBounds();
  const b = fitBounds(cur.x, cur.y, width ?? cur.width);
  overlay.setBounds(b);
  applyMouse();
  const { bounds } = display;
  log('INFO', `叠加层${what} (${b.x},${b.y}) ${b.width}x${b.height}${userHeight == null ? '（高度到屏幕底）' : ''}`);
  fetch(`${SERVER}/api/action`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'overlay_moved', x: (b.x - bounds.x) / bounds.width, y: (b.y - bounds.y) / bounds.height,
                           width: b.width, height: userHeight ?? undefined, screen: { x: bounds.x, y: bounds.y } }),
  }).catch((err) => log('WARN', '叠加层位置没存上', String(err)));
}

// 改大小：右边 / 下边跟着光标走，左上角不动；不超出所在屏的右边 / 下边
function startResize(mode) {
  if (locked || drag || resize || !overlay) return;
  const c = screen.getCursorScreenPoint();
  const b = overlay.getBounds();
  resize = { mode, dx: b.x + b.width - c.x, dy: b.y + b.height - c.y, timer: null };
  resize.timer = setInterval(() => {
    if (!overlay || overlay.isDestroyed()) return;
    const p = screen.getCursorScreenPoint();
    const cur = overlay.getBounds();
    const { bounds } = display;
    const next = { ...cur };
    if (resize.mode.includes('w')) next.width = clamp(Math.round(p.x + resize.dx - cur.x), MIN_WIDTH, bounds.x + bounds.width - cur.x);
    if (resize.mode.includes('h')) next.height = clamp(Math.round(p.y + resize.dy - cur.y), MIN_HEIGHT, bounds.y + bounds.height - cur.y);
    if (next.width !== cur.width || next.height !== cur.height) overlay.setBounds(next);
  }, 16);
  applyMouse();
}

function stopResize(save = true) {
  if (!resize) return;
  clearInterval(resize.timer);
  const { mode } = resize;
  resize = null;
  if (!save || !overlay || overlay.isDestroyed()) return;
  if (mode.includes('h')) userHeight = overlay.getBounds().height;
  saveBounds({ w: '改宽度', h: '改高度', wh: '改大小' }[mode]);
}

const fromOverlay = (e) => overlay && !overlay.isDestroyed() && e.sender === overlay.webContents;

ipcMain.on('overlay:locked', (e, v) => {
  if (!fromOverlay(e) || v === locked) return;
  locked = v;
  log('INFO', locked ? '叠加层固定' : '叠加层解锁');
  if (locked) { stopDrag(); stopResize(); }
});
ipcMain.on('overlay:hit-rects', (e, rects) => {
  if (!Array.isArray(rects)) return;
  if (topbar && !topbar.isDestroyed() && e.sender === topbar.webContents) {
    topbarRects = rects.filter((r) => [r.x, r.y, r.w, r.h].every(Number.isFinite));
    applyTopbarMouse();
    return;
  }
  if (!fromOverlay(e)) return;
  hitRects = rects.filter((r) => [r.x, r.y, r.w, r.h].every(Number.isFinite));
  applyMouse();
});
ipcMain.on('overlay:drag', (e, on) => {
  if (!fromOverlay(e)) return;
  if (on) startDrag(); else stopDrag();
});
ipcMain.on('overlay:resize', (e, mode) => {
  if (!fromOverlay(e)) return;
  if (['w', 'h', 'wh'].includes(mode)) startResize(mode); else stopResize();
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
  // 控制面板 window.open 的本机页面（队友战绩 mates.html）开成普通窗口；别的网址一律不开
  panel.webContents.setWindowOpenHandler(({ url }) => (url.startsWith(`${SERVER}/`)
    ? { action: 'allow', overrideBrowserWindowOptions: {
      width: 1240, height: 860, minWidth: 900, minHeight: 560,
      backgroundColor: '#0b0e13', autoHideMenuBar: true } }
    : { action: 'deny' }));
  panel.webContents.on('did-create-window', (win) => watch(win, '队友战绩'));
  panel.loadURL(`${SERVER}/panel.html`);
  panel.once('ready-to-show', () => panel.show());
  panel.on('closed', () => { panel = null; app.quit(); });
}

app.whenReady().then(async () => {
  log('INFO', `Electron ${process.versions.electron} 启动，服务 ${SERVER}`);
  const cfg = await loadConfig();
  gameDisplay = pickDisplay(cfg.monitor);
  createOverlay(cfg);
  createTopbar();   // 买活 / 大招都关着也开：控制面板能随时打开，关着时页面什么都不画、整条点穿
  createPanel();
});

app.on('window-all-closed', () => app.quit());
