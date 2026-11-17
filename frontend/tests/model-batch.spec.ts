import { expect, test } from '@playwright/test'
import { readFile } from 'node:fs/promises'

test('模型批量训练：多选、成功清单、复核评估、导出', async ({ page }) => {
  test.setTimeout(120_000)
  const errors: string[] = []
  page.on('pageerror', e => errors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()} ${r.url()}`) })
  await page.goto('/model')
  await expect(page.locator('svg.dag-svg')).toBeVisible()
  await expect(page.getByTestId('batch-panel')).toBeVisible()

  // 多选两个模型
  await page.getByTestId('select-1').check()
  await page.getByTestId('select-2').check()
  await page.getByTestId('version-input').fill('FV-001')
  await page.getByTestId('batch-train').click()
  await expect(page.getByTestId('batch-message')).toBeVisible()
  await expect(page.getByTestId('run-detail')).toBeVisible()

  // 成功率按成功样本算，不再按全部样本
  const row = page.locator('tr[data-model="MODE-0001"][data-path="排除失败样本"]').first()
  await expect(row).toBeVisible()
  await expect(row.getByTestId('success-rate')).toHaveText('75%')
  await expect(row).toContainText('age、income、score')
  await expect(page.getByTestId('success-list').locator('tbody tr')).toHaveCount(3)
  await expect(page.getByTestId('success-SAMPLE-0003')).toHaveCount(0)
  await expect(page.getByTestId('pending-SAMPLE-0003')).toBeVisible()
  await expect(page.getByTestId('used-features')).toHaveText('age、income、score')

  // 重复提交不会多出第二份
  const before = await page.locator('tbody tr[data-testid^="run-"]').count()
  await page.getByTestId('select-1').uncheck()
  await page.getByTestId('batch-train').click()
  await page.waitForTimeout(800)
  await expect(page.locator('tbody tr[data-testid^="run-"]')).toHaveCount(before)

  // 复核评估
  const runRow = page.locator('tr[data-model="MODE-0001"][data-path="排除失败样本"]').first()
  await runRow.getByTestId(/run-evaluate/).click()
  await expect(page.getByTestId('run-recheck')).toContainText('有效 3 条')
  await expect(page.getByTestId('batch-message')).toContainText('实际参与评估 3 条')

  // 导出区分两种口径
  const downloadPromise = page.waitForEvent('download')
  await runRow.getByTestId(/run-export/).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('model-training.csv')
  const csv = await readFile((await download.path())!, 'utf-8')
  expect(csv).toContain('口径')
  expect(csv).toContain('成功样本,MODE-0001,SAMPLE-0001')
  expect(csv).toContain('待补齐样本,MODE-0001,SAMPLE-0003')
  expect(csv).toContain('失败,已排除')
  expect(csv).not.toContain('待补齐样本,MODE-0001,SAMPLE-0001')

  // 补齐路径给出不同结果
  await page.getByTestId('select-1').check()
  await page.getByTestId('path-input').selectOption('补齐失败分片')
  await page.getByTestId('batch-train').click()
  const backfill = page.locator('tr[data-model="MODE-0001"][data-path="补齐失败分片"]').first()
  await expect(backfill).toBeVisible()
  await expect(backfill.getByTestId('success-rate')).toHaveText('100%')
  await expect(page.getByTestId('success-SAMPLE-0003')).toBeVisible()
  await expect(page.getByTestId('success-SAMPLE-0003')).toContainText('已补齐')

  // 再点回「排除失败样本」的训练结果，详情只展示这份清单，不残留补齐结果
  await runRow.getByTestId(/run-detail/).click()
  await expect(page.getByTestId('run-detail')).toContainText('排除失败样本')
  await expect(page.getByTestId('success-list').locator('tbody tr')).toHaveCount(3)
  await expect(page.getByTestId('success-SAMPLE-0003')).toHaveCount(0)
  await expect(page.getByTestId('pending-SAMPLE-0003')).toBeVisible()

  expect(errors).toEqual([])
})
