import { test, expect } from '@playwright/test'

// Captures screenshots for README. Run with:
//   TEST_UI_URL=http://localhost:8091 npx playwright test capture-screenshots
// Output goes to ../docs/screenshots/ relative to the framework/ui dir.
const OUT = '../../docs/screenshots'

test.use({
  viewport: { width: 1600, height: 1000 },
  deviceScaleFactor: 2,           // higher-DPI for crisper images
})

test.describe('readme screenshots', () => {
  test.beforeEach(async ({ page }) => {
    // Set a stable filter so cards are the same across runs
    await page.addInitScript(() => {
      localStorage.setItem('agent-list-filter', 'all')
      localStorage.setItem('agent-list-app-filter', 'all')
    })
  })

  test('agent grid', async ({ page }) => {
    await page.goto('/')
    await page.waitForLoadState('networkidle')
    // Wait for agent cards to render
    await expect(page.locator('a[href^="/agents/"]').first()).toBeVisible()
    await page.waitForTimeout(500)
    await page.screenshot({ path: `${OUT}/agent-grid.png`, fullPage: false })
  })

  test('agent grid filtered to aisleprompt', async ({ page }) => {
    await page.goto('/')
    await page.waitForLoadState('networkidle')
    await page.getByTestId('app-filter-aisleprompt').click()
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/agent-grid-filtered.png`, fullPage: false })
  })

  test('agent detail with goals tab', async ({ page }) => {
    await page.goto('/agents/aisleprompt-progressive-improvement-agent')
    await page.waitForLoadState('networkidle')
    await page.getByRole('button', { name: 'Goals', exact: true }).click()
    await page.waitForTimeout(700)
    await page.screenshot({ path: `${OUT}/agent-detail-goals.png`, fullPage: false })
  })

  test('agent detail overview with confirmation banner', async ({ page }) => {
    await page.goto('/agents/aisleprompt-progressive-improvement-agent')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(500)
    await page.screenshot({ path: `${OUT}/agent-detail-overview.png`, fullPage: false })
  })

  test('dependency graph', async ({ page }) => {
    await page.goto('/graph')
    await page.waitForFunction(
      () => !document.body.innerText.includes('Loading dependency graph'),
      { timeout: 15_000 }
    )
    await page.waitForTimeout(1500)  // let auto-layout settle + animations finish
    await page.screenshot({ path: `${OUT}/dependency-graph.png`, fullPage: false })
  })

  test('dependency graph with side panel', async ({ page }) => {
    await page.goto('/graph')
    await page.waitForFunction(
      () => !document.body.innerText.includes('Loading dependency graph'),
      { timeout: 15_000 }
    )
    await page.waitForTimeout(1500)
    // Click a recognizable node (responder-agent typically has many edges)
    const node = page.locator('.react-flow__node-agent').filter({ hasText: 'responder-agent' }).first()
    if (await node.count() > 0) {
      await node.click()
      await page.waitForTimeout(400)
    }
    await page.screenshot({ path: `${OUT}/dependency-graph-side-panel.png`, fullPage: false })
  })
})
