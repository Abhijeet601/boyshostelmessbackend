"""Run against serve_preview.py and Vite :5175. Never touches production data."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/"test-artifacts"
OUT.mkdir(exist_ok=True)
BASE="http://127.0.0.1:5175"
PASSWORD="TestOnly-Safe!42"
report={"routes":[],"viewports":[],"checks":[],"errors":[]}


def login(page,login_id,role):
    page.goto(BASE+"/login")
    page.get_by_label("Login ID",exact=True).fill(login_id)
    page.get_by_label("Password",exact=True).fill(PASSWORD)
    page.get_by_role("button",name="Sign in",exact=True).click()
    page.wait_for_url(f"**/{role}/dashboard")
    expect(page.locator(".welcome-hero, .student-hero")).to_be_visible()


def load(page,path):
    print("Checking "+path+f" ({page.viewport_size['width']}px)",flush=True)
    page.goto(BASE+path)
    page.locator("h1").wait_for()
    page.wait_for_timeout(500)
    errors=page.locator(".error-state")
    assert errors.count()==0,errors.all_text_contents()


with sync_playwright() as p:
    browser=p.chromium.launch(executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",headless=True,args=["--no-sandbox"])
    context=browser.new_context(viewport={"width":1440,"height":1000},reduced_motion="reduce")
    page=context.new_page()
    page.on("pageerror",lambda error:report["errors"].append(str(error)))
    page.goto(BASE+"/login")
    expect(page.get_by_role("heading",name="Welcome back",exact=True)).to_be_visible()
    page.screenshot(path=str(OUT/"login-desktop.png"),full_page=True)
    login(page,"QA-SUPER","super-admin")
    for route in ["dashboard","admins","hostels","students","students/1","qr-management","scanner-management","sessions","scanner","attendance","meals","profile-requests","analytics","reports","notifications","audit-logs","settings","profile"]:
        load(page,f"/super-admin/{route}")
        report["routes"].append(f"super-admin/{route}")
    page.goto(BASE+"/super-admin/students")
    page.get_by_role("button",name="Add student",exact=True).first.click()
    modal=page.get_by_role("dialog")
    import uuid
    suffix=uuid.uuid4().hex[:8]
    browser_name="Browser Check "+suffix
    modal.get_by_label("Full name",exact=True).fill(browser_name)
    modal.get_by_label("Roll / admission number",exact=True).fill("BROWSER-ADM-"+suffix)
    modal.get_by_label("Registration number",exact=True).fill("BROWSER-REG-"+suffix)
    modal.get_by_label("Room number",exact=True).fill("305")
    modal.get_by_label("Hostel",exact=True).select_option(label="North Residence")
    modal.get_by_role("button",name="Save student",exact=True).click()
    expect(modal).not_to_be_visible()
    page.get_by_placeholder("Search name, roll or registration…").fill(browser_name)
    expect(page.get_by_role("link",name=browser_name)).to_be_visible()
    page.get_by_role("button",name=f"Edit {browser_name}",exact=True).click()
    page.get_by_role("dialog").get_by_label("Room number",exact=True).fill("306")
    page.get_by_role("dialog").get_by_role("button",name="Save student").click()
    expect(page.get_by_role("dialog")).not_to_be_visible()
    page.get_by_role("button",name=f"Preview {browser_name}",exact=True).click()
    expect(page.get_by_role("dialog").get_by_text("306",exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).not_to_be_visible()
    report["checks"].append("Student create/edit/preview drawer/Escape")
    page.goto(BASE+"/super-admin/attendance")
    with page.expect_download() as download_info:
        page.get_by_role("button",name="CSV",exact=True).click()
    assert download_info.value.suggested_filename.endswith(".csv")
    report["checks"].append("Browser CSV download")
    page.goto(BASE+"/super-admin/settings")
    page.get_by_label("Application name",exact=True).fill("SmartMess")
    page.get_by_role("button",name="Save changes",exact=True).click()
    expect(page.get_by_text("Settings saved",exact=True)).to_be_visible()
    report["checks"].append("Settings save persistence")
    context.close()

    context=browser.new_context(viewport={"width":1440,"height":1000},reduced_motion="reduce")
    page=context.new_page()
    page.on("pageerror",lambda error:report["errors"].append(str(error)))
    login(page,"QA-ADMIN","admin")
    page.screenshot(path=str(OUT/"admin-dashboard-desktop.png"),full_page=True)
    for route in ["dashboard","students","qr-management","scanner","attendance","meals","profile-requests","reports","notifications","profile"]:
        load(page,f"/admin/{route}")
        report["routes"].append(f"admin/{route}")
    load(page,"/admin/admins")
    expect(page.get_by_text("This page needs a different permission",exact=True)).to_be_visible()
    report["checks"].append("Unauthorized executive route denied")
    load(page,"/admin/scanner")
    auth=context.request.post(BASE+"/api/auth/login",data={"login_id":"QA-STUDENT-3","password":PASSWORD}).json()
    headers={"Authorization":"Bearer "+auth["token"]}
    token=context.request.post(BASE+"/api/student/qr",headers=headers).json()["token"]
    page.get_by_label("Hardware scanner input",exact=True).fill(token)
    page.get_by_label("Hardware scanner input",exact=True).press("Enter")
    expect(page.get_by_text("Attendance marked successfully",exact=True)).to_be_visible()
    page.screenshot(path=str(OUT/"scanner-success-desktop.png"),full_page=True)
    page.wait_for_timeout(4200)
    token=context.request.post(BASE+"/api/student/qr",headers=headers).json()["token"]
    page.get_by_label("Hardware scanner input",exact=True).fill(token)
    page.get_by_label("Hardware scanner input",exact=True).press("Enter")
    expect(page.get_by_role("heading",name="Attendance Already Marked",exact=True)).to_be_visible()
    report["checks"].append("USB keyboard Enter check-in and duplicate warning")
    page.get_by_role("button",name="Ready for next scan",exact=True).click()
    page.get_by_role("button",name="Camera",exact=True).click()
    page.get_by_role("button",name="Start camera",exact=True).click()
    expect(page.locator(".auth-error")).to_be_visible(timeout=15000)
    report["checks"].append("Camera unavailable / denied recovery state")
    for width in [320,375,425,768,1440]:
        page.set_viewport_size({"width":width,"height":900})
        for route in ["dashboard","scanner","students","attendance","reports","meals","profile"]:
            load(page,"/admin/"+route)
            overflow=page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
            assert not overflow,f"Horizontal overflow: admin/{route} at {width}px"
            report["viewports"].append({"role":"admin","page":route,"width":width})
        if width==375:
            load(page,"/admin/scanner")
            page.screenshot(path=str(OUT/"scanner-mobile.png"),full_page=True)
            page.get_by_role("button",name="Open navigation",exact=True).click()
            expect(page.locator(".sidebar.mobile-open")).to_be_visible()
            page.get_by_role("button",name="Close navigation",exact=True).click(position={"x":350,"y":100})
            expect(page.locator(".sidebar.mobile-open")).to_have_count(0)
            report["checks"].append("Mobile navigation open and backdrop close")
    context.close()

    context=browser.new_context(viewport={"width":1440,"height":1000},reduced_motion="reduce")
    page=context.new_page()
    page.on("pageerror",lambda error:report["errors"].append(str(error)))
    login(page,"QA-STUDENT-1","student")
    expect(page.locator(".qr-frame svg")).to_be_visible(timeout=15000)
    page.screenshot(path=str(OUT/"student-dashboard-desktop.png"),full_page=True)
    with page.expect_response(lambda response:response.url.endswith("/api/student/qr") and response.status==200):
        page.get_by_role("button",name="Refresh",exact=True).click()
    expect(page.locator(".qr-frame svg")).to_be_visible()
    report["checks"].append("Student QR renders under StrictMode and refreshes")
    for width in [320,375,425,768,1440]:
        page.set_viewport_size({"width":width,"height":900})
        for route in ["dashboard","qr-management","attendance","meals","notifications","profile"]:
            load(page,"/student/"+route)
            assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"),f"Student overflow {route} {width}"
            report["viewports"].append({"role":"student","page":route,"width":width})
        if width==375:
            load(page,"/student/dashboard")
            expect(page.locator(".qr-frame svg")).to_be_visible()
            page.screenshot(path=str(OUT/"student-dashboard-mobile.png"),full_page=True)
    page.goto(BASE+"/student/profile")
    page.get_by_label("Mobile number",exact=True).fill("9876543210")
    page.get_by_role("button",name="Request profile update",exact=True).click()
    expect(page.get_by_text("Pending Approval",exact=True)).to_be_visible()
    report["checks"].append("Student profile change request")
    page.get_by_role("button",name="Account menu",exact=True).click()
    page.locator(".dropdown").get_by_role("button",name="Sign out",exact=True).click()
    page.wait_for_url("**/login")
    page.goto(BASE+"/student/attendance")
    page.wait_for_url("**/login")
    report["checks"].append("Logout and protected route redirect")
    context.close()
    browser.close()

(OUT/"browser-results.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
assert not report["errors"],report["errors"]
print(json.dumps({"routes":len(report["routes"]),"responsive_checks":len(report["viewports"]),"functional_checks":report["checks"],"javascript_errors":report["errors"]},indent=2))
