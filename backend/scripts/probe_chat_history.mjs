const BASE = 'http://127.0.0.1:8000/api';
async function ask(question, sessionId) {
  const body = { question, top_k: 5 };
  if (sessionId) body.session_id = sessionId;
  const res = await fetch(`${BASE}/chat/stream`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const reader = res.body.getReader(); const dec = new TextDecoder();
  let buf = '', sid = null;
  while (true) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, i); buf = buf.slice(i + 2);
      const ls = raw.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trimStart());
      if (!ls.length) continue;
      try { const p = JSON.parse(ls.join('\n')); if (p.type === 'meta') sid = p.data.session_id; } catch {}
    }
  }
  return sid;
}
(async () => {
  const sid = await ask('加速康复外科的核心措施有哪些？', null);
  await ask('它的证据等级高吗？', sid);
  const r = await fetch(`${BASE}/chat/${sid}`);
  const d = await r.json();
  console.log(`HTTP ${r.status} | session ${String(sid).slice(0,8)} | turn_count=${d.turn_count}`);
  (d.turns || []).forEach((t, i) => {
    console.log(`  [${i+1}] Q: ${String(t.question).slice(0,26)}`);
    console.log(`      A: ${String(t.answer||'').slice(0,26)}… (${(t.answer||'').length} 字) | phase=${t.phase} intent=${t.intent} citations=${t.citations_count}`);
  });
})();
