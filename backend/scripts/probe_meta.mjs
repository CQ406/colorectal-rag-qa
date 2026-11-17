const BASE = 'http://127.0.0.1:8000/api';
async function stream(path, body) {
  const res = await fetch(BASE + path, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const rd = res.body.getReader(); const dec = new TextDecoder();
  let buf = '', meta = null, done = null, chars = 0, ev = {};
  while (true) { const { value, done: f } = await rd.read(); if (f) break;
    buf += dec.decode(value, { stream: true }); let i;
    while ((i = buf.indexOf('\n\n')) >= 0) { const raw = buf.slice(0, i); buf = buf.slice(i + 2);
      const ls = raw.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trimStart());
      if (!ls.length) continue;
      try { const p = JSON.parse(ls.join('\n')); ev[p.type] = (ev[p.type]||0)+1;
        if (p.type === 'meta') meta = p.data;
        if (p.type === 'delta') chars += (p.data.text||'').length;
        if (p.type === 'done') done = p.data; } catch {} } }
  return { ev, meta, done, chars };
}
(async () => {
  console.log('=== 多轮会诊：「你是谁」===');
  const a = await stream('/chat/stream', { question: '你是谁', top_k: 5 });
  console.log(`  事件 ${JSON.stringify(a.ev)}`);
  console.log(`  intent=${a.meta?.intent} phase=${a.meta?.phase} 置信度=${a.meta?.intent_confidence}`);
  console.log(`  引用 ${(a.done?.citations||[]).length} 条 | 答案 ${(a.done?.answer||'').length} 字`);
  console.log(`  答案开头: ${String(a.done?.answer||'').slice(0,60).replace(/\n/g,' ')}`);

  console.log('');
  console.log('=== 智能问答：「你能做什么」===');
  const b = await stream('/qa/stream', { question: '你能做什么？', top_k: 5 });
  console.log(`  事件 ${JSON.stringify(b.ev)}`);
  console.log(`  引用 ${(b.done?.citations||[]).length} 条 | 答案 ${(b.done?.answer||'').length} 字`);
  console.log(`  答案开头: ${String(b.done?.answer||'').slice(0,60).replace(/\n/g,' ')}`);

  console.log('');
  console.log('=== 对照：正常知识提问仍走检索 ===');
  const c = await stream('/qa/stream', { question: '吻合口漏怎么预防？', top_k: 5 });
  console.log(`  事件 ${JSON.stringify(c.ev)}`);
  console.log(`  引用 ${(c.done?.citations||[]).length} 条 | 答案 ${(c.done?.answer||'').length} 字`);
})();
