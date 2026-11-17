/**
 * 用 Node 复现前端对 SSE 的解析，验证「引用面板为空」到底出在哪一层。
 *
 * 复现方式与前端 dispatchSseEvent 一致：
 *   按行切分 → 取所有以 "data:" 开头的行 → JSON.parse → 按 type 分发
 *
 * 输出：
 *   - 各类型事件计数
 *   - retrieval 事件的 evidences 条数
 *   - done 事件的 citations 条数
 */
const BASE = 'http://127.0.0.1:8000/api';

async function main() {
  const res = await fetch(`${BASE}/qa/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question: '加速康复外科的核心措施有哪些？', top_k: 5 }),
  });

  if (!res.ok) {
    console.log(`请求失败: ${res.status} ${await res.text()}`);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const counts = {};
  let retrievalEvidences = null;
  let doneCitations = null;
  let doneAnswerLen = 0;
  let deltaChars = 0;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE 以空行分隔事件
    let idx;
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const rawEvent = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);

      const dataLines = rawEvent
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart());
      if (!dataLines.length) continue;

      let payload;
      try {
        payload = JSON.parse(dataLines.join('\n'));
      } catch {
        console.log('[无法解析]', rawEvent.slice(0, 120));
        continue;
      }

      counts[payload.type] = (counts[payload.type] || 0) + 1;
      if (payload.type === 'retrieval') {
        retrievalEvidences = (payload.data.evidences || []).length;
      } else if (payload.type === 'delta') {
        deltaChars += (payload.data.text || '').length;
      } else if (payload.type === 'done') {
        doneCitations = Array.isArray(payload.data.citations)
          ? payload.data.citations.length
          : `非数组(${typeof payload.data.citations})`;
        doneAnswerLen = (payload.data.answer || '').length;
      }
    }
  }

  console.log('事件计数:', counts);
  console.log(`retrieval.evidences 条数: ${retrievalEvidences}`);
  console.log(`delta 累计字符: ${deltaChars}`);
  console.log(`done.citations: ${doneCitations}`);
  console.log(`done.answer 长度: ${doneAnswerLen}`);
}

main().catch((e) => console.error('出错:', e.message));
