import { test, expect, type Page } from '@playwright/test'

async function mockAnswers(page: Page) {
  let round = 0
  // All generated answers in these UI tests are local fixtures.
  await page.route('**/api/qa', route => route.fulfill({ status: 503, json: { detail: 'Unexpected fallback in UI test' } }))
  await page.route('**/api/qa/stream', route => {
    const n = ++round
    const citation = {
      index: 1, chunk_id: `ux-chunk-${n}`, document_id: `ux-doc-${n}`,
      doc_title: `UI fixture source ${n}`, doc_year: 2025, doc_journal: 'Synthetic fixture',
      evidence_level: 'guideline', evidence_level_zh: '临床指南',
      phase: 'preop', page_start: 3, page_end: 5, section: '测试章节',
      content_snippet: `第${n}轮合成摘要。`, positions: [],
    }
    const answer = `第${n}轮模拟回答[1]。`
    const body = [
      { type: 'delta', data: { text: answer } },
      { type: 'done', data: { answer, citations: [citation], elapsed_ms: 12, invalid_citations: [] } },
    ].map(event => `data: ${JSON.stringify(event)}\n\n`).join('')
    return route.fulfill({ contentType: 'text/event-stream', body })
  })
}

test('home statistics recover after failure and scene cards prepare an editable question', async ({ page }) => {
  let count = 0
  await page.route('**/api/stats', route => ++count === 1
    ? route.fulfill({ status: 503, json: { detail: 'Synthetic outage' } })
    : route.fulfill({ json: { documents: 123, total_chunks: 456, embedded_chunks: 456, phase_distribution: {}, evidence_distribution: {} } }))
  await page.goto('/qa')
  await expect(page.getByText('知识库统计暂不可用', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: '重试统计' }).click()
  await expect(page.locator('.library-count')).toHaveText('知识库已收录 123 篇文献')
  await page.getByRole('button', { name: /术前评估/ }).click()
  await expect(page.locator('textarea')).toHaveValue(/请按术前评估场景/)
  await expect(page.locator('.chat-toolbar .el-select')).toContainText('术前')
  await page.getByRole('button', { name: /查看证据/ }).click()
  await expect(page.getByText('提问后，在这里核对文献原文')).toBeVisible()
  await expect(page.getByText('本次回答暂无可核验的文献引用')).toHaveCount(0)
})

test('citation opens the correct answer source, retries original text and resizes on desktop', async ({ page }) => {
  await mockAnswers(page)
  let attempts = 0
  await page.route('**/api/chunks/ux-chunk-1', route => ++attempts === 1
    ? route.fulfill({ status: 503, json: { detail: 'Synthetic original-text outage' } })
    : route.fulfill({ json: { content: '第1轮原文完整片段，仅为合成界面测试。', archived: true } }))
  await page.goto('/qa')
  const input = page.locator('.chat-input textarea')
  await input.fill('模拟问题一')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.locator('.answer-body .cite')).toHaveCount(1)
  await page.locator('.answer-body .cite').first().click()
  await expect(page.locator('.citation-item.is-active')).toContainText('UI fixture source 1')
  await expect(page.locator('.citation-item')).toContainText('p.3-5')
  await page.getByRole('button', { name: '展开原文' }).click()
  await expect(page.getByText('原文加载失败，当前仅显示摘要。')).toBeVisible()
  await page.getByRole('button', { name: '重试原文' }).click()
  await expect(page.getByText('第1轮原文完整片段，仅为合成界面测试。')).toBeVisible()
  await expect(page.getByText('历史版本原文')).toBeVisible()
  const before = (await page.locator('.evidence-side').boundingBox())!.width
  await page.getByRole('separator', { name: '调整引用面板宽度' }).focus()
  await page.keyboard.press('ArrowLeft')
  expect((await page.locator('.evidence-side').boundingBox())!.width).toBeGreaterThan(before)
  await page.getByRole('button', { name: '关闭证据' }).click()
  await input.fill('模拟问题二')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.locator('.answer-body .cite')).toHaveCount(2)
  await page.locator('.answer-body .cite').last().click()
  await expect(page.locator('.citation-item')).toContainText('UI fixture source 2')
  await page.getByRole('button', { name: '关闭证据' }).click()
  await page.locator('.answer-body .cite').first().click()
  await expect(page.locator('.citation-item')).toContainText('UI fixture source 1')
  await expect(page.locator('.citation-item')).not.toContainText('UI fixture source 2')
  await expect(page.getByRole('button', { name: '展开原文' })).toBeVisible()
})

test('mobile question input, navigation and evidence sheet remain usable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockAnswers(page)
  await page.goto('/qa')
  const input = page.locator('.chat-input textarea')
  const box = (await input.boundingBox())!
  expect(box.width).toBeGreaterThan(300)
  expect(box.y + box.height).toBeLessThan(844)
  await input.fill('移动端合成问题')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.locator('.answer-body .cite').click()
  await expect(page.locator('.citation-item')).toContainText('UI fixture source 1')
  const side = (await page.locator('.evidence-side').boundingBox())!
  expect(side.x).toBe(0)
  expect(side.width).toBe(390)
  expect(side.y + side.height).toBeCloseTo(844, 0)
  await page.keyboard.press('Escape')
  await expect(page.locator('.evidence-side')).toHaveCount(0)
  await page.getByRole('button', { name: '打开导航', exact: true }).click()
  await page.getByRole('menuitem', { name: '典型病例' }).click()
  await expect(page).toHaveURL(/\/cases$/)
  await expect(page.locator('.app-aside')).not.toHaveClass(/is-open/)
  await page.goto('/chat')
  await expect(page.getByRole('link', { name: '连续对话', exact: true })).toHaveClass(/selected/)
  await page.locator('.chat-input textarea').fill('可编辑的连续追问')
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeEnabled()
})

test('images default to read-only detail, support zoom and recover from empty filters', async ({ page }) => {
  let analysisRequests = 0
  page.on('request', req => { if (/\/api\/images\/.*(?:analy|interpret)/.test(req.url()) && req.method() === 'POST') analysisRequests++ })
  await page.goto('/images')
  await expect(page.locator('.detail-title')).toHaveText('E2E image 24')
  await expect(page.locator('.detail-img')).toHaveJSProperty('naturalWidth', 16)
  await page.getByRole('button', { name: '放大查看原图' }).click()
  await expect(page.locator('.el-image-viewer__wrapper')).toBeVisible()
  await page.keyboard.press('Escape')
  await page.getByPlaceholder('搜索标题 / 部位 / 说明').fill('no-matching-ui-fixture')
  await page.getByPlaceholder('搜索标题 / 部位 / 说明').press('Enter')
  await expect(page.getByText('没有符合筛选条件的影像')).toBeVisible()
  await page.getByRole('button', { name: '清除筛选', exact: true }).click()
  await expect(page.locator('.card')).toHaveCount(24)
  expect(analysisRequests).toBe(0)
})

test('mobile images switch between gallery and details with a loaded original', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/images')
  await page.locator('.card').first().click()
  await expect(page.locator('.detail-title')).toBeVisible()
  await expect(page.locator('.detail-img')).toHaveJSProperty('naturalWidth', 16)
  await expect(page.locator('.gallery-pane')).not.toBeVisible()
  expect((await page.locator('.detail-pane').boundingBox())!.width).toBeLessThanOrEqual(390)
  await page.getByRole('button', { name: '影像列表', exact: true }).click()
  await expect(page.locator('.card').first()).toBeVisible()
})

test('case loading errors provide retry and public empty state discloses no drafts', async ({ page }) => {
  let attempts = 0
  await page.route(/\/api\/cases(?:\?|$)/, route => ++attempts === 1
    ? route.fulfill({ status: 503, json: { detail: 'Synthetic case-list outage' } })
    : route.fulfill({ json: { items: [], total: 0, limit: 20, offset: 0 } }))
  await page.goto('/cases')
  await expect(page.getByText('病例加载失败')).toBeVisible()
  await page.getByRole('button', { name: '重试加载' }).click()
  await expect(page.getByText('暂无已审核公开的病例，资料经审核发布后会显示在这里')).toBeVisible()
  await expect(page.getByText('草稿待补全来源与人工审核，尚未向访客公开')).toHaveCount(0)
  await expect(page.getByText('管理范围病例', { exact: false })).toHaveCount(0)
})

test('public knowledge overview avoids protected calls and charts fit narrow screens', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const protectedCalls: string[] = []
  page.on('request', request => { if (request.url().includes('/api/admin/kb/')) protectedCalls.push(request.url()) })
  await page.goto('/knowledge')
  await expect(page.getByText('当前展示公开语料统计；登录授权账户后可查阅文献详情与管理记录')).toBeVisible()
  await expect(page.getByRole('tab', { name: '文献管理' })).toHaveCount(0)
  await expect(page.locator('.stat-cards')).toBeVisible()
  for (const chart of await page.locator('.chart-block').all()) {
    const box = (await chart.boundingBox())!
    expect(box.x).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width).toBeLessThanOrEqual(390)
  }
  expect(protectedCalls).toEqual([])
})
