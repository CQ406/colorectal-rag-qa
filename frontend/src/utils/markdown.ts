/**
 * 答案渲染：Markdown → 安全的 HTML，并把引用标记变成可点击元素。
 *
 * 两个必须处理的问题
 * ------------------
 * 1. **XSS 防护**：答案由 LLM 生成，属不可信内容。虽然走了 markdown-it，
 *    但原始 HTML 若被放行则可能注入脚本。故：
 *      - markdown-it 关闭 html 选项（不解析原始 HTML）
 *      - 再用 DOMPurify 二次过滤
 *
 * 2. **引用标记可点击**：答案中的 `[1]` 需渲染为可点击上标。
 *
 * 实现方式：哨兵标记法（重要）
 * ----------------------------
 * 最初实现把引用标签包在反引号里交给 markdown-it，事后再从 <code> 中还原。
 * 该方案**失败**：markdown-it 会把内容转义为
 *   `<code>&lt;sup class=&quot;cite&quot;...&gt;</code>`
 * 而反向还原的正则需同时匹配 `&lt;` `&quot;` `&gt;` 的转义形式，
 * 极其脆弱，实测完全无法还原，引用标记以字面文本出现在页面上。
 *
 * 现改为哨兵标记法，分三步：
 *   ① 把 `[1]` 换成不含特殊字符的哨兵，如 `@@CITE1@@`
 *   ② 交给 markdown-it 渲染（哨兵是普通文本，不会被转义或改写）
 *   ③ 在结果 HTML 中把哨兵替换为真正的 `<sup class="cite">` 标签
 *
 * 哨兵只含字母数字与 @，不含 `<` `>` `&` `"`，
 * 因此能安全穿过 markdown-it 的转义流程，还原时也不存在歧义。
 *
 * 已知取舍：若答案中出现 `` `[1]` ``（行内代码包裹的引用标记），
 * 也会被替换为可点击引用。医学问答场景下代码块中出现引用标记的情况
 * 极罕见，且替换后仍可点击、不会造成误解，故接受该取舍。
 */

import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import json from 'highlight.js/lib/languages/json'
import plaintext from 'highlight.js/lib/languages/plaintext'
import python from 'highlight.js/lib/languages/python'
import sql from 'highlight.js/lib/languages/sql'
import MarkdownIt from 'markdown-it'

// 按需注册语言，避免打包全部语言（体积从 ~1MB 降到 ~100KB）
hljs.registerLanguage('python', python)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('shell', bash)
hljs.registerLanguage('json', json)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('text', plaintext)
hljs.registerLanguage('plaintext', plaintext)

const md: MarkdownIt = new MarkdownIt({
  html: false, // 禁止原始 HTML，防 XSS
  linkify: true,
  breaks: true, // 单个换行即换段，符合中文阅读习惯
  highlight(code: string, lang: string): string {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return hljs.highlight(code, { language: lang, ignoreIllegals: true }).value
      } catch {
        /* 降级到纯文本 */
      }
    }
    return hljs.highlight(code, { language: 'plaintext' }).value
  },
})

/** 引用标记：支持 [1]、[1][3]、[1,2]、[1，2]（全角）、[1-3] */
const CITATION_PATTERN = /\[(\d+(?:\s*[-,，]\s*\d+)*)\]/g

/** 哨兵格式：不含 < > & " 等会被转义的字符 */
const SENTINEL_PREFIX = '@@CITE'
const SENTINEL_SUFFIX = '@@'
const SENTINEL_PATTERN = /@@CITE(\d+)@@/g

/** 单个引用区间的最大展开数量，防止 `[1-9999]` 这类输入导致内存膨胀 */
const MAX_RANGE = 20

/**
 * 把引用标记替换为哨兵。
 *
 * @returns [替换后的文本, 哨兵编号 -> 引用编号列表]
 */
function replaceCitationsWithSentinels(text: string): [string, Map<number, number[]>] {
  const registry = new Map<number, number[]>()
  let sentinelSeq = 0

  const replaced = text.replace(CITATION_PATTERN, (match, group: string) => {
    const indices: number[] = []

    for (const part of String(group).split(/[，,]/)) {
      const trimmed = part.trim()
      if (trimmed.includes('-')) {
        const bounds = trimmed.split('-', 2)
        const lo = Number(bounds[0])
        const hi = Number(bounds[1])
        if (
          Number.isInteger(lo) &&
          Number.isInteger(hi) &&
          lo > 0 &&
          hi >= lo &&
          hi - lo <= MAX_RANGE
        ) {
          for (let i = lo; i <= hi; i += 1) indices.push(i)
        }
      } else if (/^\d+$/.test(trimmed)) {
        indices.push(Number(trimmed))
      }
    }

    if (indices.length === 0) return match

    sentinelSeq += 1
    registry.set(sentinelSeq, indices)
    return `${SENTINEL_PREFIX}${sentinelSeq}${SENTINEL_SUFFIX}`
  })

  return [replaced, registry]
}

/**
 * 把 HTML 中的哨兵还原为可点击的引用上标。
 */
function restoreSentinels(html: string, registry: Map<number, number[]>): string {
  return html.replace(SENTINEL_PATTERN, (_match, seqStr: string) => {
    const indices = registry.get(Number(seqStr))
    if (!indices) return _match
    return indices
      .map(
        (i) =>
          `<sup class="cite" data-cite="${i}">[${i}]</sup>`,
      )
      .join('')
  })
}

/**
 * 渲染答案为安全 HTML。
 *
 * @param text 答案原文（含 [n] 引用标记）
 */
export function renderAnswer(text: string): string {
  if (!text) return ''

  // ① 引用标记 → 哨兵
  const [withSentinels, registry] = replaceCitationsWithSentinels(text)

  // ② markdown 渲染
  const html = md.render(withSentinels)

  // ③ 哨兵 → 真实引用标签
  const withCitations = restoreSentinels(html, registry)

  // ④ 白名单过滤（XSS 防护）
  return DOMPurify.sanitize(withCitations, {
    ALLOWED_TAGS: [
      'p', 'br', 'strong', 'em', 'del', 'sup', 'sub', 'code', 'pre', 'span',
      'ul', 'ol', 'li', 'blockquote', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr', 'a',
    ],
    ALLOWED_ATTR: ['class', 'data-cite', 'title', 'href', 'target', 'rel'],
    ALLOWED_URI_REGEXP: /^(?:https?|mailto):/i,
  })
}

/** 移除引用标记，得到纯文本（用于复制、导出） */
export function stripCitationMarkers(text: string): string {
  return text.replace(CITATION_PATTERN, '')
}
