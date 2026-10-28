"""Focused follow-up: executive mobile layouts, simulated camera lifecycle, and recovery."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

BASE="http://127.0.0.1:5175"
OUT=Path(__file__).resolve().parents[2]/"test-artifacts"
result={"executive_responsive":[],"checks":[],"javascript_errors":[]}


def login(page,login_id,role):
    page.goto(BASE+"/login")
    page.get_by_label('Login ID',exact=True).fill(login_id)
    page.get_by_label('Password',exact=True).fill('TestOnly-Safe!42')
    page.get_by_role('button',name='Sign in',exact=True).click()
    page.wait_for_url(f'**/{role}/dashboard')
    expect(page.locator('.welcome-hero, .student-hero')).to_be_visible()


with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--no-sandbox','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'])
    context=browser.new_context(permissions=['camera'],reduced_motion='reduce',viewport={"width":1440,"height":950})
    page=context.new_page()
    page.on('pageerror',lambda e:result['javascript_errors'].append(str(e)))
    login(page,'QA-SUPER','super-admin')
    for width in (320,375,425):
        page.set_viewport_size({"width":width,"height":900})
        for route in ('dashboard','admins','hostels','sessions','scanner-management','qr-management','settings','profile-requests','audit-logs'):
            print(f'Executive mobile: {route} at {width}',flush=True)
            page.goto(BASE+'/super-admin/'+route)
            page.locator('h1').wait_for()
            page.wait_for_timeout(400)
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1'),f'Overflow {route} {width}'
            result['executive_responsive'].append({"page":route,"width":width})
    page.set_viewport_size({"width":1440,"height":950})
    page.goto(BASE+'/super-admin/dashboard')
    expect(page.locator('.welcome-hero')).to_be_visible()
    page.screenshot(path=str(OUT/'super-admin-dashboard-desktop.png'),full_page=True)
    context.close()

    context=browser.new_context(permissions=['camera'],reduced_motion='reduce',viewport={"width":1440,"height":950})
    page=context.new_page()
    page.on('pageerror',lambda e:result['javascript_errors'].append(str(e)))
    login(page,'QA-ADMIN','admin')
    page.goto(BASE+'/admin/scanner')
    page.get_by_role('button',name='Camera',exact=True).click()
    page.get_by_role('button',name='Start camera',exact=True).click()
    expect(page.locator('#smartmess-camera video')).to_be_visible(timeout=20000)
    page.evaluate("window.firstTrack=document.querySelector('#smartmess-camera video').srcObject.getVideoTracks()[0]")
    expect(page.get_by_role('button',name='Stop',exact=True)).to_be_enabled(timeout=20000)
    page.get_by_role('button',name='Restart camera',exact=True).click()
    expect(page.get_by_role('button',name='Stop',exact=True)).to_be_enabled(timeout=20000)
    page.wait_for_function('window.firstTrack.readyState === "ended"')
    page.evaluate("window.secondTrack=document.querySelector('#smartmess-camera video').srcObject.getVideoTracks()[0]")
    page.get_by_role('button',name='Stop',exact=True).click()
    page.wait_for_function('window.secondTrack.readyState === "ended"')
    result['checks'].append('Simulated camera initialization, restart, and stop release tracks')
    page.get_by_role('button',name='Start camera',exact=True).click()
    expect(page.locator('#smartmess-camera video')).to_be_visible(timeout=20000)
    page.evaluate("window.thirdTrack=document.querySelector('#smartmess-camera video').srcObject.getVideoTracks()[0]")
    page.get_by_role('link',name='Dashboard',exact=True).click()
    page.wait_for_function('window.thirdTrack.readyState === "ended"')
    result['checks'].append('Camera tracks stop on route unmount')
    page.goto(BASE+'/admin/scanner')
    page.route('**/api/scanner/consume',lambda route:route.abort('failed'))
    page.get_by_label('Hardware scanner input',exact=True).fill('invalid-token-for-network-check-123456789')
    page.get_by_label('Hardware scanner input',exact=True).press('Enter')
    expect(page.get_by_role('heading',name='Connection interrupted',exact=True)).to_be_visible()
    page.get_by_role('button',name='Ready for next scan',exact=True).click()
    page.unroute('**/api/scanner/consume')
    page.get_by_label('Hardware scanner input',exact=True).fill('invalid-token-for-error-check-123456789')
    page.get_by_label('Hardware scanner input',exact=True).press('Enter')
    expect(page.get_by_role('heading',name='Invalid QR code',exact=True)).to_be_visible()
    result['checks'].append('Network interruption, manual recovery, and invalid QR feedback')
    context.close()

    context=browser.new_context(reduced_motion='reduce',viewport={"width":375,"height":900})
    page=context.new_page()
    page.on('pageerror',lambda e:result['javascript_errors'].append(str(e)))
    login(page,'QA-STUDENT-1','student')
    expect(page.locator('.qr-frame svg')).to_be_visible()
    print('Waiting for scheduled QR rotation',flush=True)
    with page.expect_response(lambda r:r.url.endswith('/api/student/qr') and r.status==200,timeout=40000):
        pass
    expect(page.locator('.qr-frame svg')).to_be_visible()
    result['checks'].append('Scheduled automatic QR rotation')
    page.get_by_role('button',name='Open navigation',exact=True).click()
    page.keyboard.press('Escape')
    expect(page.locator('.sidebar.mobile-open')).to_have_count(0)
    result['checks'].append('Mobile navigation Escape dismissal')
    context.close()
    browser.close()

(OUT/'browser-followup-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
assert not result['javascript_errors'],result['javascript_errors']
print(json.dumps(result,indent=2))
