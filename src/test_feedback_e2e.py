"""End-to-end check of the feedback feature in a real browser.

Posts an actual report through the form, then verifies it came back from the
server — the badge count must go up, which proves the whole round trip rather
than just that a form submitted.
"""
import sys, asyncio, json, urllib.request
from playwright.async_api import async_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else 'http://akl-train-map:8731/'
OUT = sys.argv[2] if len(sys.argv) > 2 else 'output/feedback.png'


def api(path):
    with urllib.request.urlopen(URL.rstrip('/') + path) as r:
        return json.load(r)


async def main():
    before = api('/api/feedback')['stats']
    print('before:', before['reports'], 'reports across', before['units'], 'units')

    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await (await b.new_context(viewport={'width': 1500, 'height': 1000})).new_page()
        errs = []
        pg.on('console', lambda m: errs.append(m.text) if m.type == 'error' else None)
        pg.on('pageerror', lambda e: errs.append('pageerror: ' + str(e)))
        await pg.goto(URL, wait_until='networkidle', timeout=60000)
        await pg.wait_for_timeout(2800)

        print('badges on map/list:', await pg.locator('.flag').count())
        worst = await pg.locator('#worst .w').count()
        print('"most reported" rows:', worst)
        if worst:
            print('  top row:', (await pg.locator('#worst .w').first.inner_text()).replace('\n', ' '))
        await pg.screenshot(path=OUT)

        # Open the form from the "most reported" list — that path also covers
        # units that aren't currently out on the network.
        await pg.locator('#worst .w').first.click()
        await pg.wait_for_timeout(900)
        if not await pg.locator('#fbwrap.on').count():
            # It flew to a live marker instead; use the popup button.
            btn = pg.locator('.fbopen')
            if await btn.count():
                await btn.first.click()
                await pg.wait_for_timeout(700)
        opened = await pg.locator('#fbwrap.on').count()
        print('form opened:', bool(opened))
        print('  unit shown:', await pg.locator('#fbunit').inner_text())
        print('  categories:', await pg.locator('#fbcat option').count())
        await pg.screenshot(path=OUT.replace('.png', '_form.png'))

        # Validation must reject a bad case reference rather than silently drop it.
        await pg.fill('#fbref', 'not a valid ref!!')
        await pg.fill('#fbnote', 'automated test — invalid ref')
        await pg.click('#fbsend')
        await pg.wait_for_timeout(1200)
        print('  invalid-ref message:', await pg.locator('#fbmsg').inner_text())
        print('  still open (rejected):', bool(await pg.locator('#fbwrap.on').count()))

        # Now a valid submission.
        await pg.fill('#fbref', '')
        await pg.select_option('#fbcat', 'cleanliness')
        await pg.select_option('#fbsev', '2')
        await pg.fill('#fbnote', 'automated end-to-end test report')
        await pg.click('#fbsend')
        await pg.wait_for_timeout(1500)
        print('  success message:', await pg.locator('#fbmsg').inner_text())
        await pg.screenshot(path=OUT.replace('.png', '_sent.png'))
        await pg.wait_for_timeout(1200)
        print('form closed after send:', not await pg.locator('#fbwrap.on').count())

        print('console errors:', errs or 'none')
        await b.close()

    after = api('/api/feedback')['stats']
    print('after:', after['reports'], 'reports across', after['units'], 'units')
    print('DELTA:', after['reports'] - before['reports'], '(expect 1)')
    newest = api('/api/feedback')['reports'][0]
    print('newest stored:', newest['unit'], newest['category'],
          'sev' + str(newest['severity']), '|', newest['note'])
    print('repeat offenders:', after['repeat_offenders'])


asyncio.run(main())
