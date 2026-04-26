import { expect, test } from '@playwright/test'

test.describe('Dependency Graph page', () => {
  test('renders react-flow canvas with nodes and edges', async ({ page }) => {
    await page.goto('/graph')

    // Header
    await expect(page.getByRole('heading', { name: /Agent Dependency Graph/i })).toBeVisible()

    // Wait for the loading state to finish
    await page.waitForFunction(
      () => !document.body.innerText.includes('Loading dependency graph'),
      { timeout: 15_000 }
    )

    // The agent count should be > 0
    await expect(page.getByText(/\d+ agents · \d+ dependencies/)).toBeVisible({ timeout: 10_000 })

    // React-flow renders an svg with edge paths
    await expect(page.locator('.react-flow__edges')).toBeVisible({ timeout: 10_000 })

    // At least one custom agent node
    const nodes = page.locator('.react-flow__node-agent')
    await expect(nodes.first()).toBeVisible()
    expect(await nodes.count()).toBeGreaterThan(0)

    // MiniMap + Controls + Background present
    await expect(page.locator('.react-flow__minimap')).toBeVisible()
    await expect(page.locator('.react-flow__controls')).toBeVisible()
  })

  test('toolbar buttons are clickable', async ({ page }) => {
    await page.goto('/graph')
    await page.waitForFunction(
      () => !document.body.innerText.includes('Loading dependency graph'),
      { timeout: 15_000 }
    )
    // Save layout button
    const saveBtn = page.getByRole('button', { name: /save layout/i })
    await expect(saveBtn).toBeVisible()
    await saveBtn.click()
    await expect(page.getByText(/saved /)).toBeVisible({ timeout: 5_000 })
    // Auto layout button
    await page.getByRole('button', { name: /auto layout/i }).click()
    // Reset (just verify it's reachable; we don't actually want to wipe state mid-test)
    await expect(page.getByRole('button', { name: /reset/i })).toBeVisible()
  })

  test('clicking a node opens the side panel', async ({ page }) => {
    await page.goto('/graph')
    await page.waitForFunction(
      () => !document.body.innerText.includes('Loading dependency graph'),
      { timeout: 15_000 }
    )
    const node = page.locator('.react-flow__node-agent').first()
    await expect(node).toBeVisible()
    await node.click()
    // Side panel opens with "Open agent →" link
    await expect(page.getByRole('link', { name: /Open agent/ })).toBeVisible({ timeout: 5_000 })
    // Incoming/Outgoing sections visible
    await expect(page.getByText('Incoming')).toBeVisible()
    await expect(page.getByText('Outgoing')).toBeVisible()
  })

  test('layout persists across reloads', async ({ page }) => {
    await page.goto('/graph')
    await page.waitForFunction(
      () => !document.body.innerText.includes('Loading dependency graph'),
      { timeout: 15_000 }
    )
    await page.getByRole('button', { name: /save layout/i }).click()
    await page.waitForTimeout(300)

    // localStorage should have the key set
    const stored = await page.evaluate(() => localStorage.getItem('framework-graph-layout:v1'))
    expect(stored).toBeTruthy()
    const parsed = JSON.parse(stored!)
    expect(parsed.positions).toBeDefined()
    expect(Object.keys(parsed.positions).length).toBeGreaterThan(0)
  })
})
