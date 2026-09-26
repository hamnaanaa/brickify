// Deterministic frame render of the assembly viewer -> PNG frames (with motion blur via sub-frame averaging)
// usage: node render_video.js <viewer-url> <outdir> [fps] [subframes] [shutter]
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
(async () => {
  const [url, outdir, fpsS = '30', subS = '5', shutterS = '0.6', maxS = '0', startS = '0'] = process.argv.slice(2);
  const fps = +fpsS, sub = +subS, shutter = +shutterS;
  fs.mkdirSync(outdir, { recursive: true });
  const browser = await chromium.launch({ args: ['--use-angle=metal', '--enable-webgl', '--ignore-gpu-blocklist'] });
  const page = await browser.newPage({ viewport: { width: 1080, height: 1080 }, deviceScaleFactor: 1 });
  page.on('console', m => { if (m.type() === 'error') console.log('console:', m.text()); });
  await page.goto(url + (url.includes('?') ? '&' : '?') + 'det=1&ui=0&play=0', { waitUntil: 'networkidle' });
  await page.waitForFunction(() => window.__ready === true && window.__total > 0, null, { timeout: 60000 });
  await page.waitForTimeout(800);
  const total = await page.evaluate(() => window.__total);
  const nFrames = +maxS > 0 ? Math.min(+maxS, Math.ceil(total * fps)) : Math.ceil(total * fps);
  console.log(`total ${total.toFixed(2)} s, ${nFrames} frames at ${fps} fps, ${sub} sub-samples, shutter ${shutter}`);
  // motion blur: average `sub` renders spread over `shutter` of the frame interval, done on a canvas in-page
  await page.evaluate(() => {
    const src = document.getElementById('c');
    const acc = document.createElement('canvas'); acc.width = src.width; acc.height = src.height; acc.id = 'acc';
    acc.style.cssText = 'position:fixed;left:0;top:0;width:100vw;height:100vh;z-index:99;display:none'; document.body.appendChild(acc);
    window.__blur = (times) => {
      const ctx = acc.getContext('2d'); ctx.globalCompositeOperation = 'source-over'; ctx.globalAlpha = 1; ctx.clearRect(0, 0, acc.width, acc.height);
      times.forEach((tt, i) => { window.__setTime(tt); ctx.globalAlpha = 1 / (i + 1); ctx.drawImage(src, 0, 0); });
      ctx.globalAlpha = 1;
    };
  });
  const t0 = Date.now();
  for (let f = 0; f < nFrames; f++) {
    const t = +startS + f / fps;
    const times = []; for (let k = 0; k < sub; k++) times.push(t + (k / Math.max(sub - 1, 1)) * shutter / fps);
    await page.evaluate((times) => window.__blur(times), times);
    const acc = await page.$('#acc');
    await page.evaluate(() => { document.getElementById('acc').style.display = 'block'; });
    await acc.screenshot({ path: path.join(outdir, `f${String(f).padStart(5, '0')}.png`), omitBackground: false });
    await page.evaluate(() => { document.getElementById('acc').style.display = 'none'; });
    if (f % 60 === 0) console.log(`frame ${f}/${nFrames} ${((Date.now() - t0) / 1000).toFixed(0)}s`);
  }
  await browser.close();
  console.log('done');
})().catch(e => { console.error(e); process.exit(1); });
