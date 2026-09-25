"""Real Chromium UI against a disposable local database; no external traffic."""
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

from aiohttp.test_utils import TestServer
import pytest
from playwright.async_api import async_playwright, expect

from services.memory import upsert_memory_updates
from services.tasks import save_task
from webapp import server

pytestmark = pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS') != '1', reason='Dedicated browser CI job enables Chromium')


async def test_mobile_and_desktop_editing(db, tmp_path):
    await save_task(42, {'title': 'Browser assignment', 'course': 'WEB', 'estimated_minutes': 90})
    await upsert_memory_updates(42, [{'key': 'browser_fact', 'value': 'original'}])
    values = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': 42, 'first_name': 'Browser Test'})}
    key = hmac.new(b'WebAppData', server.settings.bot_token.encode(), hashlib.sha256).digest()
    values['hash'] = hmac.new(key, '\n'.join(f'{k}={v}' for k, v in sorted(values.items())).encode(), hashlib.sha256).hexdigest()
    script = 'window.Telegram={WebApp:{initData:' + json.dumps(urlencode(values)) + ',ready(){},expand(){}}};'
    async with TestServer(server.create_web_app()) as app, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=os.getenv('TEST_BROWSER_EXECUTABLE') or None)
        try:
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            async def traffic(route):
                if route.request.url.startswith(str(app.make_url('/'))):
                    await route.continue_()
                elif route.request.url.startswith('https://telegram.org/'):
                    await route.fulfill(content_type='application/javascript', body=script)
                else:
                    await route.abort()
            await page.route('**/*', traffic)
            await page.goto(str(app.make_url('/app')))
            await expect(page.locator('#statusDot')).to_have_class('status-dot online')
            await page.get_by_role('button', name='Tasks & Risk', exact=True).click()
            await expect(page.locator('#v4Tasks')).to_contain_text('Browser assignment')
            await page.locator('#v4Tasks').get_by_role('button', name='Edit', exact=True).click()
            await page.locator('#taskForm [name=progress]').fill('35')
            await page.locator('#taskForm').get_by_role('button', name='Save task').click()
            await expect(page.locator('#v4Tasks')).to_contain_text('35%')
            await page.get_by_role('button', name='Memory', exact=True).click()
            await page.locator('#v4Memory textarea').fill('corrected in browser')
            await page.locator('#v4Memory').get_by_role('button', name='Save correction').click()
            await expect(page.locator('#v4Memory textarea')).to_have_value('corrected in browser')
            await page.get_by_role('button', name='Settings', exact=True).click()
            await page.locator('#preferencesForm [name=dnd_start]').fill('22:15')
            async with page.expect_response(lambda response: response.url.endswith('/api/v4/preferences') and response.request.method == 'POST') as saved:
                await page.locator('#preferencesForm').get_by_role('button', name='Save preferences').click()
            assert (await saved.value).status == 200
            await page.reload()
            await page.get_by_role('button', name='Settings', exact=True).click()
            await expect(page.locator('#preferencesForm [name=dnd_start]')).to_have_value('22:15')
            await page.get_by_role('button', name='Refresh diagnostics').click()
            await expect(page.locator('#v4Diagnostics')).to_contain_text('SQLite')
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            await page.evaluate("window.scrollTo(0, 0)")
            await page.screenshot(path=str(tmp_path / 'mobile.png'), full_page=True)
            await page.set_viewport_size({'width': 1280, 'height': 900})
            await page.get_by_role('button', name='Week', exact=True).click()
            await expect(page.locator('#overrideForm')).to_be_visible()
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            await page.evaluate("window.scrollTo(0, 0)")
            await page.screenshot(path=str(tmp_path / 'desktop.png'), full_page=True)
            assert errors == []
        finally:
            await browser.close()
