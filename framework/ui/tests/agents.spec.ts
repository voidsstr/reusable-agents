import { expect, test } from '@playwright/test'

// Smoke + functional tests for the framework UI. Assume the framework
// stack is running at TEST_UI_URL (default http://localhost:8091/).

test.describe('Agent List page', () => {
  test('renders header and at least one agent card', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByText('reusable-agents').first()).toBeVisible()
    // The nav has Agents / Graph / Confirmations / AI Providers / Events
    // Scope to the header so the "Agents" nav link doesn't collide with
    // the agent-card list below.
    const header = page.locator('header')
    await expect(header.getByRole('link', { name: 'Agents', exact: true })).toBeVisible()
    await expect(header.getByRole('link', { name: 'Graph', exact: true })).toBeVisible()
    await expect(header.getByRole('link', { name: 'AI Providers', exact: true })).toBeVisible()
    // At least one agent visible (we registered ~28)
    const cards = page.locator('a[href^="/agents/"]')
    await expect(cards.first()).toBeVisible({ timeout: 10_000 })
    expect(await cards.count()).toBeGreaterThan(0)
  })

  test('does not show duplicate base blueprint agents', async ({ page }) => {
    await page.goto('/')
    await page.waitForLoadState('networkidle')
    // The base "progressive-improvement-agent" (without per-site prefix)
    // is marked is_blueprint=true — register-all-from-dir.sh skips them,
    // so they should NOT appear in the list.
    const baseLink = page.locator('a[href="/agents/progressive-improvement-agent"]')
    await expect(baseLink).toHaveCount(0)
    // Per-site instances should be present
    await expect(page.locator('a[href="/agents/aisleprompt-progressive-improvement-agent"]')).toHaveCount(1)
  })
})

test.describe('Agent Detail page', () => {
  test('shows tabs and switches between them', async ({ page }) => {
    await page.goto('/agents/seo-opportunity-agent')
    await expect(page.getByText('seo-opportunity-agent').first()).toBeVisible()
    // Tabs
    for (const tab of ['Overview', 'Directives', 'Runs', 'Messages', 'Storage', 'Confirmations', 'Changelog']) {
      await expect(page.getByRole('button', { name: tab, exact: true })).toBeVisible()
    }
    // Click Runs — verify content area shows runs UI by looking for refresh
    // button or the empty-state message
    await page.getByRole('button', { name: 'Runs', exact: true }).click()
    await expect(
      page.getByText(/refresh|No runs yet/i).first()
    ).toBeVisible({ timeout: 8_000 })
    // Click Storage — verify content area shows storage UI
    await page.getByRole('button', { name: 'Storage', exact: true }).click()
    await expect(
      page.getByText(/prefix|No blobs/i).first()
    ).toBeVisible({ timeout: 8_000 })
  })

  test('Runs tab drills into per-run artifacts', async ({ page, request }) => {
    // Find an agent that has runs in storage. The smoke test populated
    // `progressive-improvement-agent` (now a blueprint, hidden from the list
    // but still has run data in storage) — fall back to any agent with runs.
    const candidates = ['progressive-improvement-agent',
                         'aisleprompt-progressive-improvement-agent',
                         'seo-opportunity-agent']
    let target: string | null = null
    for (const c of candidates) {
      const r = await request.get(`http://localhost:8093/api/agents/${c}/runs?limit=5`)
      if (!r.ok()) continue
      const runs = await r.json()
      if (Array.isArray(runs) && runs.length > 0) { target = c; break }
    }
    if (!target) test.skip(true, 'no agent has runs in storage yet')

    await page.goto(`/agents/${target}`)
    await page.getByRole('button', { name: 'Runs', exact: true }).click()
    const runRows = page.locator('button').filter({ hasText: /\d{8}T\d{6}Z/ })
    await expect(runRows.first()).toBeVisible({ timeout: 8_000 })
    await runRows.first().click()
    await expect(page.getByText('Run artifacts')).toBeVisible({ timeout: 5_000 })
    // Try to click recommendations.json if present
    const recArtifact = page.getByText('recommendations.json').first()
    if (await recArtifact.count() > 0) {
      await recArtifact.click()
      await expect(page.getByText(/recommendations|schema_version/).first())
        .toBeVisible({ timeout: 5_000 })
    }
  })
})

test.describe('AI Providers page', () => {
  test('lists providers and shows default badge', async ({ page }) => {
    await page.goto('/providers')
    await expect(page.getByText(/AI Provider|Provider/i).first()).toBeVisible()
    // Default-provider seeded as anthropic
    await expect(page.getByText('anthropic').first()).toBeVisible({ timeout: 5_000 })
  })
})

test.describe('Confirmations page', () => {
  test('renders without errors', async ({ page }) => {
    await page.goto('/confirmations')
    // Either list of confirmations OR empty state — both are valid
    await expect(page.locator('body')).toBeVisible()
    // Should not show a crash
    const errorBoundary = page.getByText(/Error|Something went wrong/i)
    await expect(errorBoundary).toHaveCount(0)
  })
})

test.describe('Events page', () => {
  test('renders the events feed', async ({ page }) => {
    await page.goto('/events')
    await expect(page.locator('body')).toBeVisible()
    const errorBoundary = page.getByText(/Error|Something went wrong/i)
    await expect(errorBoundary).toHaveCount(0)
  })
})
