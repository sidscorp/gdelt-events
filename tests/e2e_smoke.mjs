// GDELT dashboard front-end smoke test (Playwright).
//
// Verifies the live dashboard renders + its core interactions work, and that
// none of OUR OWN assets/APIs fail. External article thumbnails (other news
// sites, often hotlink-blocked) and the by-design SSE briefing abort are
// ignored — they are not regressions.
//
// Setup (one-time):  npm i playwright && npx playwright install chromium
// Run:               URL=https://gdeltmonitor.com node tests/e2e_smoke.mjs
import { chromium } from 'playwright';

const TARGET = process.env.URL || 'https://gdeltmonitor.com';
const ORIGIN = new URL(TARGET).origin;
const ownFailures = [];

const browser = await chromium.launch();
const page = await browser.newPage();
page.on('response', (res) => {
  const u = res.url(), s = res.status();
  if (!u.startsWith(ORIGIN)) return;                 // ignore external resources
  if (u.includes('/static/') && s >= 400) ownFailures.push(`${s} ${u}`);
  if (u.includes('/api/') && s >= 500) ownFailures.push(`${s} ${u}`);
});
page.on('pageerror', (e) => ownFailures.push('pageerror: ' + e.message.slice(0, 160)));

const r = { url: TARGET };
try {
  await page.goto(TARGET, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('#articleList li.article', { timeout: 30000 });
  r.articles = await page.locator('#articleList li.article').count();

  await page.click('#themeBtn');
  r.darkOn = await page.evaluate(() => document.body.classList.contains('dark'));
  await page.click('#themeBtn');
  r.darkOff = await page.evaluate(() => document.body.classList.contains('dark'));

  await page.click('.time-pill[data-hours="24"]');
  await page.waitForTimeout(3000);
  r.articlesAfter = await page.locator('#articleList li.article').count();

  r.briefingVisible = await page.locator('#briefingPanel').isVisible().catch(() => false);
} catch (e) {
  r.fatal = e.message.slice(0, 160);
}
r.ownFailures = ownFailures;
r.pass = !r.fatal && r.articles > 0 && r.darkOn === true && r.darkOff === false
         && r.articlesAfter > 0 && ownFailures.length === 0;
await browser.close();
console.log(JSON.stringify(r, null, 2));
process.exit(r.pass ? 0 : 1);
