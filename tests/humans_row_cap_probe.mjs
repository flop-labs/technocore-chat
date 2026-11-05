// Run against a local service: node tests/humans_row_cap_probe.mjs <port>.
// Real layout and animations, with bounded message batches instead of 250 writes.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';

const base = `http://127.0.0.1:${process.argv[2] || 8099}`;
const browser = await chromium.launch();
try {
  for (const reducedMotion of ['no-preference', 'reduce']) {
    const context = await browser.newContext({reducedMotion});
    const page = await context.newPage(), pending = [], errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/r/row-cap-probe?*', route => { pending.push(route); });
    await page.goto(`${base}/humans#r/row-cap-probe`, {waitUntil: 'domcontentloaded'});
    const target = page.locator('#log .seq').filter({hasText: /^#125$/});
    const position = () => target.evaluate(el => el.getBoundingClientRect().top);
    let before;
    for (let first = 1; first <= 201; first += 50) {
      for (let tries = 0; !pending.length && tries < 1000; tries++)
        await page.waitForTimeout(10);
      assert.ok(pending.length, 'the live pump must request its next batch');
      const messages = Array.from({length: 50}, (_, i) => ({
        seq: first + i, ts: new Date().toISOString(), from: 'reader',
        text: `row ${first + i} ` + 'readable message '.repeat(20),
      }));
      await pending.shift().fulfill({json: {
        room: 'row-cap-probe', first_seq: first, last_seq: first + 49, messages,
      }});
      await page.waitForFunction(seq =>
        document.querySelector('#log .msg:last-child .seq')?.textContent === `#${seq}`,
        first + 49);
      if (first === 151) {
        await target.evaluate(el => el.scrollIntoView({block: 'center'}));
        await page.waitForTimeout(300); // Outlast the 180ms arrival animation.
        before = await position();
      }
    }
    await page.waitForTimeout(300);
    const after = await position();
    assert.equal(await page.locator('#log .msg').count(), 200);
    assert.equal(await page.locator('#log .seq').first().textContent(), '#51');
    assert.equal(await page.locator('#log .seq').last().textContent(), '#250');
    assert.ok(await page.locator('#jump').isVisible());
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({reducedMotion, before, after, delta: after - before}));
    assert.ok(Math.abs(after - before) <= 1, 'retained history moved out from under the reader');
    await context.close();
  }
} finally {
  await browser.close();
}
