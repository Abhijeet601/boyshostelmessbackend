"""Session/scanner regression checks against the isolated QA server on :5175."""
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = "http://127.0.0.1:5175"
OUT = Path(__file__).resolve().parents[2] / "test-artifacts"
OUT.mkdir(exist_ok=True)
checks = []
errors = []


def login(page, login_id, role):
    page.goto(BASE + "/login")
    page.get_by_label("Login ID", exact=True).fill(login_id)
    page.get_by_label("Password", exact=True).fill("TestOnly-Safe!42")
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.wait_for_url(f"**/{role}/dashboard")
    expect(page.locator(".welcome-hero, .student-hero")).to_be_visible()


def status(page, path):
    return page.evaluate("""async path => {
      const api = await import('/src/lib/api.js');
      try { await api.apiRequest(path); return 200; }
      catch (error) { return error.status; }
    }""", path)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
    context = browser.new_context(reduced_motion="reduce", permissions=["camera"])
    context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(str(error))))
    try:
        admin = context.new_page()
        login(admin, "QA-ADMIN", "admin")
        admin.goto(BASE + "/admin/scanner")
        expect(admin.get_by_role("button", name="Camera", exact=True)).to_be_enabled()
        student = context.new_page()
        login(student, "QA-STUDENT-1", "student")
        expect(student.locator(".qr-frame svg")).to_be_visible()
        assert status(admin, "/scanner/status") == 200
        assert status(student, "/auth/me") == 200
        assert admin.evaluate("sessionStorage.getItem('mess_api_role')") == "admin"
        assert student.evaluate("sessionStorage.getItem('mess_api_role')") == "student"
        assert admin.evaluate("localStorage.getItem('mess_api_token')") is None
        checks.append("Admin scanner and Student QR stay authenticated in separate tabs")

        admin.get_by_role("button", name="Camera", exact=True).click()
        admin.get_by_role("button", name="Start camera", exact=True).click()
        expect(admin.get_by_role("button", name="Stop", exact=True)).to_be_enabled(timeout=20000)
        admin.evaluate("window.sessionTrack=document.querySelector('#smartmess-camera video').srcObject.getVideoTracks()[0]")
        other_admin = context.new_page()
        login(other_admin, "QA-ADMIN", "admin")
        other_admin.get_by_role("button", name="Account menu", exact=True).click()
        other_admin.locator(".dropdown").get_by_role("button", name="Sign out", exact=True).click()
        other_admin.wait_for_url("**/login")
        assert status(admin, "/scanner/status") == 200
        assert admin.evaluate("window.sessionTrack.readyState") == "live"
        checks.append("Logging out a second Admin session leaves the first session and camera active")

        student.reload()
        expect(student.locator(".qr-frame svg")).to_be_visible()
        assert status(student, "/scanner/status") == 403
        expect(student.locator(".qr-frame svg")).to_be_visible()
        checks.append("Tab login survives reload and student scanner access remains forbidden")

        admin.get_by_role("button", name="Account menu", exact=True).click()
        admin.locator(".dropdown").get_by_role("button", name="Sign out", exact=True).click()
        admin.wait_for_url("**/login")
        admin.wait_for_function("window.sessionTrack.readyState === 'ended'")
        assert status(student, "/auth/me") == 200
        checks.append("Own logout stops the camera without signing out the Student tab")

        # Hold an old request, replace this tab's session, then return an old 401.
        held = []
        race = context.new_page()
        race.goto(BASE + "/login")
        a = context.request.post(BASE + "/api/auth/login", data={"login_id": "QA-ADMIN", "password": "TestOnly-Safe!42"}).json()
        b = context.request.post(BASE + "/api/auth/login", data={"login_id": "QA-STUDENT-2", "password": "TestOnly-Safe!42"}).json()
        race.evaluate("async session => (await import('/src/lib/api.js')).saveSession(session)", a)
        race.route("**/api/session-regression-probe", lambda route: held.append(route))
        race.evaluate("""async () => {
          window.invalidations=0;
          window.addEventListener('smartmess:unauthorized',()=>window.invalidations++);
          const api=await import('/src/lib/api.js');
          window.oldResponse=api.apiRequest('/session-regression-probe').catch(error=>({name:error.name,status:error.status}));
        }""")
        race.wait_for_timeout(100)
        assert held
        race.evaluate("async session => (await import('/src/lib/api.js')).saveSession(session)", b)
        held[0].fulfill(status=401, json={"detail": "An older session expired"})
        result = race.evaluate("window.oldResponse")
        assert result["name"] == "AbortError"
        assert race.evaluate("window.invalidations") == 0
        assert race.evaluate("sessionStorage.getItem('mess_api_role')") == "student"
        assert status(race, "/auth/me") == 200
        checks.append("A delayed 401 from an old token cannot clear a newer login")

        legacy = context.new_page()
        legacy.goto(BASE + "/login")
        legacy.evaluate("""session => {
          localStorage.setItem('mess_api_token',session.token);
          localStorage.setItem('mess_api_role',session.role);
          localStorage.setItem('mess_api_user',JSON.stringify(session.user));
        }""", a)
        legacy.reload()
        legacy.wait_for_url("**/admin/dashboard")
        assert legacy.evaluate("sessionStorage.getItem('mess_api_role')") == "admin"
        assert legacy.evaluate("localStorage.getItem('mess_api_token')") is None
        checks.append("A valid legacy shared login migrates into tab storage")

        denied = context.new_page()
        login(denied, "QA-ADMIN", "admin")
        scanner_requests = []
        denied.on("request", lambda request: scanner_requests.append(request.url) if "/api/scanner/" in request.url else None)
        denied.route("**/api/scanner/status", lambda route: route.fulfill(status=403, json={"detail": {"code": "UNAUTHORIZED_SCANNER", "message": "Scanner access is disabled"}}))
        denied.goto(BASE + "/admin/scanner")
        expect(denied.get_by_text("Scanner access is disabled", exact=True)).to_be_visible()
        assert denied.get_by_role("button", name="Start camera", exact=True).count() == 0
        assert denied.get_by_label("Hardware scanner input", exact=True).count() == 0
        count = len(scanner_requests)
        denied.wait_for_timeout(16500)
        assert len(scanner_requests) == count
        assert all("/status" in path for path in scanner_requests)
        denied.unroute("**/api/scanner/status")
        denied.get_by_role("button", name="Try again", exact=True).click()
        expect(denied.get_by_label("Hardware scanner input", exact=True)).to_be_visible()
        checks.append("403 stops polling and scanning; explicit retry restores verified access")

        expired = context.new_page()
        expired.goto(BASE + "/login")
        expired.evaluate("""async () => {
          const api=await import('/src/lib/api.js');
          api.saveSession({token:'expired.invalid',role:'admin',user:{role:'admin',name:'Expired test'}});
        }""")
        expired.goto(BASE + "/admin/scanner")
        expired.wait_for_url("**/login")
        assert expired.evaluate("sessionStorage.getItem('mess_api_token')") is None
        checks.append("Invalid session clears only this tab and redirects to login")

        rejected = context.new_page()
        rejected.goto(BASE + '/login')
        rejected.get_by_label('Login ID', exact=True).fill('QA-ADMIN')
        rejected.get_by_label('Password', exact=True).fill('Incorrect-password!42')
        rejected.get_by_role('button', name='Sign in', exact=True).click()
        expect(rejected.get_by_text('Invalid login ID or password', exact=True)).to_be_visible()
        rejected.get_by_label('Password', exact=True).fill('TestOnly-Safe!42')
        rejected.get_by_role('button', name='Sign in', exact=True).click()
        rejected.wait_for_url('**/admin/dashboard')
        checks.append('A failed sign-in remains retryable without invalidating other tab sessions')
    finally:
        context.close()
        browser.close()

result = {"checks": checks, "javascript_errors": errors}
(OUT / "browser-session-results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
assert not errors, errors
print(json.dumps(result, indent=2))
