"""Compact enrollment dialogs; run after browser_registration.py on isolated QA."""
import json
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

out=Path(__file__).resolve().parents[2]/'test-artifacts'
checks=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':375,'height':900})
    page.goto('http://127.0.0.1:5175/login')
    page.get_by_label('Login ID',exact=True).fill('QA-ADMIN')
    page.get_by_label('Password',exact=True).fill('TestOnly-Safe!42')
    page.get_by_role('button',name='Sign in',exact=True).click()
    page.wait_for_url('**/admin/dashboard')
    for width in (320,375):
        page.set_viewport_size({'width':width,'height':900})
        page.goto('http://127.0.0.1:5175/admin/registration-requests')
        page.get_by_label('Status',exact=True).select_option('PENDING')
        page.get_by_role('button',name=re.compile('^Review ')).first.click()
        expect(page.locator('.application-detail')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        checks.append({'width':width,'view':'application drawer'})
        page.screenshot(path=str(out/f'registration-drawer-{width}.png'))
        page.get_by_role('button',name='Reject',exact=True).click()
        expect(page.get_by_label('Rejection reason',exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        checks.append({'width':width,'view':'decision confirmation'})
        page.keyboard.press('Escape')
        expect(page.get_by_role('dialog')).to_have_count(0)
    page.set_viewport_size({'width':1440,'height':1000})
    page.goto('http://127.0.0.1:5175/admin/students')
    page.get_by_placeholder('Search name, roll or registration…').fill('New Student')
    page.get_by_role('button',name=re.compile('^Edit New Student')).first.click()
    expect(page.get_by_label('Academic session',exact=True)).to_have_value('2026-2027')
    page.get_by_label('Room number',exact=True).fill('208')
    page.get_by_role('button',name='Save student',exact=True).click()
    expect(page.get_by_role('dialog')).to_have_count(0)
    page.get_by_role('button',name=re.compile('^Edit New Student')).first.click()
    expect(page.get_by_label('Academic session',exact=True)).to_have_value('2026-2027')
    checks.append({'view':'editing an approved applicant preserves academic session'})
    out.joinpath('registration-dialog-results.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
    print(json.dumps(checks,indent=2))
    browser.close()
