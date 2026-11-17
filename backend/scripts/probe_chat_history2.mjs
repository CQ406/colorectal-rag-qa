const BASE = 'http://127.0.0.1:8000/api';
async function ask(q, sid) {
  const body = { question: q, top_k: 5 }; if (sid) body.session_id = sid;
  const res = await fetch(`${BASE}/chat/stream`, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const rd = res.body.getReader(); const dec = new TextDecoder();
  let buf = '', s = null;
  while (true) { const { value, done } = await rd.read(); if (done) break;
    buf += dec.decode(value, { stream: true }); let i;
    while ((i = buf.indexOf('\n\n')) >= 0) { const raw = buf.slice(0, i); buf = buf.slice(i + 2);
      const ls = raw.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trimStart());
      if (!ls.length) continue;
      try { const p = JSON.parse(ls.join('\n')); if (p.type === 'meta') s = p.data.session_id; } catch {} } }
  return s;
}
(async () => {
  const sid = await ask('加速康复外科的核心措施有哪些？', null);
  await ask('它的证据等级高吗？', sid);
  const d = await (await fetch(`${BASE}/chat/${sid}`)).json();
  console.log(`turn_count=${d.turn_count}`);
  (d.turns || []).forEach((t, i) => {
    const c = t.citations || [];
    console.log(`  [${i+1}] ${String(t.question).slice(0,22)} | 引用 ${c.length} 条 | 首条: ${c[0] ? String(c[0].doc_title).slice(0,30) : '-'}`);
    if (c[0]) console.log(`        chunk_id=${String(c[0].chunk_id).slice(0,8)}… sections=${c[0].section || '-'}`);
  });
})();
