const BASE = 'http://127.0.0.1:8000/api';
async function ask(question, sessionId) {
  const body = { question, top_k: 5 };
  if (sessionId) body.session_id = sessionId;
  const res = await fetch(`${BASE}/chat/stream`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) { console.log('HTTP ' + res.status, (await res.text()).slice(0,200)); return null; }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '', counts = {}, meta = null, done = null, chars = 0;
  while (true) {
    const { value, done: fin } = await reader.read();
    if (fin) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, i); buf = buf.slice(i + 2);
      const lines = raw.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trimStart());
      if (!lines.length) continue;
      let p; try { p = JSON.parse(lines.join('\n')); } catch { continue; }
      counts[p.type] = (counts[p.type]||0)+1;
      if (p.type === 'meta') meta = p.data;
      if (p.type === 'delta') chars += (p.data.text||'').length;
      if (p.type === 'done') done = p.data;
      if (p.type === 'error') console.log('  error:', p.data.message);
    }
  }
  console.log(`  事件: ${JSON.stringify(counts)}`);
  console.log(`  meta: session=${meta?.session_id?.slice(0,8)} 分期=${meta?.phase} 意图=${meta?.intent} 改写=${meta?.rewritten_query ? '是' : '否'}`);
  console.log(`  delta 字符=${chars} | done.answer=${(done?.answer||'').length} | 引用=${(done?.citations||[]).length}`);
  return meta?.session_id;
}
(async () => {
  console.log('=== 首轮 ===');
  const sid = await ask('加速康复外科的核心措施有哪些？', null);
  console.log('');
  console.log('=== 追问（带 session_id）===');
  await ask('它的证据等级高吗？', sid);
})();
