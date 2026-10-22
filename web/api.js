// 和 Python 服务通信：SSE 收状态快照，POST 发操作。
export function connect(onState, onLink) {
  let es;
  const open = () => {
    es = new EventSource('/api/events');
    es.onopen = () => onLink?.(true);
    es.onmessage = (ev) => {
      try { onState(JSON.parse(ev.data)); } catch (e) { console.error(e); }
    };
    es.onerror = () => onLink?.(false);   // EventSource 会自己重连
  };
  open();
  return () => es.close();
}

export async function act(type, payload = {}) {
  try {
    const res = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type, ...payload }),
    });
    return await res.json();
  } catch (e) {
    return { ok: false, message: `连不上助手：${e.message}` };
  }
}

export async function getJSON(path) {
  const res = await fetch(path);
  return res.json();
}
