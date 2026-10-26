// 叠加层页面的预加载脚本：只开放「固定 / 解锁、可点区域、开始 / 结束拖动、改宽度」几个口子给页面。
// 买活计时条（topbar.html）也用这个，只用「可点区域」。
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('overlayShell', {
  setLocked: (locked) => ipcRenderer.send('overlay:locked', !!locked),
  setHitRects: (rects) => ipcRenderer.send('overlay:hit-rects', rects),
  dragStart: () => ipcRenderer.send('overlay:drag', true),
  dragEnd: () => ipcRenderer.send('overlay:drag', false),
  resizeStart: () => ipcRenderer.send('overlay:resize', true),
  resizeEnd: () => ipcRenderer.send('overlay:resize', false),
});
