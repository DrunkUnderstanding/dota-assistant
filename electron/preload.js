// 叠加层页面的预加载脚本：只开放「固定 / 解锁、可点区域、开始 / 结束拖动」几个口子给页面。
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('overlayShell', {
  setLocked: (locked) => ipcRenderer.send('overlay:locked', !!locked),
  setHitRects: (rects) => ipcRenderer.send('overlay:hit-rects', rects),
  dragStart: () => ipcRenderer.send('overlay:drag', true),
  dragEnd: () => ipcRenderer.send('overlay:drag', false),
});
