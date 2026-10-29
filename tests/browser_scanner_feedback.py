"""Self-contained scanner UI regression suite. API and camera decoder are mocked.

Run: python tests/browser_scanner_feedback.py
Starts its own Vite server, never connects to a database, and closes its server.
Uses installed Playwright Chromium or an existing local Chrome installation.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

from playwright.sync_api import Error, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "test-artifacts"
OUT.mkdir(exist_ok=True)
USER = {"id": 101, "name": "Scanner Test Operator", "role": "admin", "hostel_scope": "North Residence", "scanner_enabled": True}
SETTINGS = {"app_name": "SmartMess", "institution_name": "Scanner QA", "timezone": "Asia/Kolkata", "scanner_sound": False, "schedules": []}
SESSION = {"id": 7, "meal": "Breakfast", "session_date": "2026-10-29", "start_time": "07:00", "end_time": "10:00", "hostel": "North Residence"}
STUDENT = {"id": 42, "name": "Scanner Fixture Student", "admission_number": "ROLL-042", "registration_number": "REG-2026-042", "hostel": "North Residence", "room_number": "102", "photo_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/aEAAAAASUVORK5CYII="}
MARKED_AT = "2026-10-29T02:45:00+00:00"
CAMERA_MODULE = """
export class Html5Qrcode {
  constructor() { this.isScanning = false; window.__testCamera = this; }
  async start(device, settings, decoded) { this.isScanning = true; window.__decodeQr = decoded; }
  pause() { window.__testCameraPaused = true; }
  resume() { window.__testCameraPaused = false; }
  async stop() { this.isScanning = false; }
  clear() {}
  getRunningTrackSettings() { return {}; }
}
"""


def run():
    report = {"checks": [], "errors": [], "viewports": [], "mocked": ["API responses", "camera QR decoder"]}
    scans = []
    held = []

    def api_route(route):
        path = urlsplit(route.request.url).path
        if path.endswith("/scanner/consume"):
            token = route.request.post_data_json["token"]
            scans.append(token)
            if token == "held-request":
                held.append(route)
                return
            if token == "network-error":
                route.abort("failed")
                return
            if token == "malformed-request":
                route.fulfill(status=422, json={"detail": [{"loc": ["body", "token"], "msg": "String should have at most 256 characters", "type": "string_too_long"}]})
                return
            if token.startswith("invalid:"):
                code = token.split(":", 1)[1]
                route.fulfill(status=403 if code in {"STUDENT_NOT_APPROVED", "STUDENT_SUSPENDED", "UNAUTHORIZED_SCANNER"} else 422, json={"detail": {"success": False, "code": code, "message": "Test rejection: " + code}})
                return
            if token.startswith("duplicate"):
                route.fulfill(status=409, json={"detail": {"success": False, "code": "ALREADY_MARKED", "message": "Attendance already marked for this meal", "student": STUDENT, "meal": "Breakfast", "session": SESSION, "original_time": MARKED_AT, "alreadyMarkedAt": MARKED_AT, "qr_replay": True}})
                return
            route.fulfill(json={"success": True, "status": "MARKED", "code": "MARKED", "student": STUDENT, "meal": "Breakfast", "scanned_at": MARKED_AT})
        elif path.endswith("/auth/me"):
            route.fulfill(json=USER)
        elif path.endswith("/settings"):
            route.fulfill(json=SETTINGS)
        elif path.endswith("/scanner/status") or path.endswith("/current-session"):
            route.fulfill(json={"session": SESSION, "meal": "Breakfast", "sessions": [SESSION], "timezone": "Asia/Kolkata"})
        elif path.endswith("/scanner/heartbeat"):
            route.fulfill(json={"status": "online"})
        elif path.endswith("/notifications"):
            route.fulfill(json={"items": [], "unread_count": 0})
        else:
            raise AssertionError("Unexpected API request: " + path)

    def scan(page, token):
        field = page.get_by_label("Hardware scanner input", exact=True)
        expect(field).to_be_enabled()
        field.fill(token)
        field.press("Enter")

    def dismiss(page):
        page.locator('[data-testid="scan-feedback"] button').click()
        page.wait_for_timeout(200)
        expect(page.get_by_test_id("scan-feedback")).to_have_count(0)

    def counter(page, label, value):
        tile = page.locator(".scanner-stat").filter(has=page.get_by_text(label, exact=True))
        expect(tile.locator("strong span")).to_have_attribute("aria-label", str(value))

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "VITE_API_BASE_URL": "/api"}
    with (OUT / "scanner-vite.log").open("w", encoding="utf-8") as log:
        server = subprocess.Popen([shutil.which("node"), str(ROOT / "frontend/node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", str(port), "--strictPort"], cwd=ROOT / "frontend", env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            for _ in range(100):
                try:
                    with urlopen(base, timeout=1):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError("Vite did not start; see scanner-vite.log")
            with sync_playwright() as playwright:
                launch = {"headless": True, "args": ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]}
                chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
                if chrome.exists():
                    launch["executable_path"] = str(chrome)
                browser = playwright.chromium.launch(**launch)
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce", permissions=["camera"])
                context.route("**/api/**", api_route)
                context.route("**/node_modules/.vite/deps/html5-qrcode.js*", lambda route: route.fulfill(content_type="application/javascript", body=CAMERA_MODULE))
                context.add_init_script("sessionStorage.setItem('mess_api_token', 'scanner-test-token'); sessionStorage.setItem('mess_api_role', 'admin'); sessionStorage.setItem('mess_api_user', " + json.dumps(json.dumps(USER)) + ");")
                page = context.new_page()
                page.on("pageerror", lambda error: report["errors"].append(str(error)))
                page.goto(base + "/admin/scanner")
                expect(page.get_by_label("Hardware scanner input", exact=True)).to_be_enabled()

                scan(page, "success-first")
                expect(page.get_by_role("heading", name="ATTENDANCE MARKED SUCCESSFULLY!", exact=True)).to_be_visible()
                expect(page.get_by_test_id("scan-feedback").get_by_text(STUDENT["name"], exact=True)).to_be_visible()
                counter(page, "Total Scans", 1)
                counter(page, "Successful Scans", 1)
                page.wait_for_timeout(500)
                assert page.get_by_test_id("scan-feedback").evaluate("el => Number(getComputedStyle(el).opacity)") > 0.98
                page.screenshot(path=str(OUT / "scanner-feedback-success-desktop.png"), full_page=True)
                expect(page.get_by_label("Hardware scanner input", exact=True)).to_be_disabled()
                page.wait_for_timeout(4100)
                expect(page.get_by_test_id("scan-feedback")).to_have_count(0)
                expect(page.get_by_label("Hardware scanner input", exact=True)).to_be_enabled()
                report["checks"].append("USB Enter success, exact student details, locked input, automatic resume after four seconds")

                scan(page, "duplicate-desktop")
                feedback = page.get_by_test_id("scan-feedback")
                expect(feedback.get_by_role("heading", name="ATTENDANCE ALREADY MARKED!", exact=True)).to_be_visible()
                for text in (STUDENT["name"], STUDENT["admission_number"], STUDENT["registration_number"], STUDENT["hostel"], STUDENT["room_number"], "DUPLICATE SCAN", "Original attendance date", "Original attendance time"):
                    expect(feedback.get_by_text(text, exact=True)).to_be_visible()
                expect(feedback.locator("img")).to_have_attribute("src", STUDENT["photo_url"])
                expect(feedback).to_contain_text("08:15:00")
                expect(feedback).to_contain_text("29 Oct 2026")
                page.wait_for_timeout(500)
                assert feedback.evaluate("el => Number(getComputedStyle(el).opacity)") > 0.98
                page.screenshot(path=str(OUT / "scanner-feedback-duplicate-desktop.png"), full_page=True)
                counter(page, "Duplicate Scans", 1)
                expect(page.get_by_role("list", name="Recent scan events").locator("li").first).to_contain_text("Duplicate")
                dismiss(page)
                report["checks"].append("Large duplicate card includes photo, roll, registration, hostel, room, meal and original India-local timestamp; manual dismissal and immediate activity/counters")

                # An old dismissal timer must not reset the next student's request.
                scan(page, "duplicate-old-timer")
                expect(page.get_by_role("heading", name="ATTENDANCE ALREADY MARKED!", exact=True)).to_be_visible()
                page.wait_for_timeout(3000)
                dismiss(page)
                scan(page, "success-after-dismissal")
                expect(page.get_by_role("heading", name="ATTENDANCE MARKED SUCCESSFULLY!", exact=True)).to_be_visible()
                page.wait_for_timeout(1500)
                expect(page.get_by_role("heading", name="ATTENDANCE MARKED SUCCESSFULLY!", exact=True)).to_be_visible()
                dismiss(page)
                report["checks"].append("Manual dismissal clears the old timer without prematurely dismissing subsequent feedback")

                errors = {"QR_INVALID": "Invalid QR Code", "QR_EXPIRED": "Expired QR Code", "QR_USED": "QR Already Used", "STUDENT_NOT_APPROVED": "Student Account Not Approved", "STUDENT_SUSPENDED": "Student Account Suspended", "NO_ACTIVE_SESSION": "No Active Meal Session"}
                for code, title in errors.items():
                    scan(page, "invalid:" + code)
                    expect(page.get_by_test_id("scan-feedback").get_by_role("heading", name=title, exact=True)).to_be_visible()
                    expect(page.locator(".scan-feedback-invalid")).to_be_visible()
                    dismiss(page)
                scan(page, "network-error")
                expect(page.get_by_role("heading", name="Network Error", exact=True)).to_be_visible()
                dismiss(page)
                scan(page, "malformed-request")
                expect(page.get_by_role("heading", name="Invalid QR Code", exact=True)).to_be_visible()
                dismiss(page)
                scan(page, "held-request")
                expect(page.get_by_label("Hardware scanner input", exact=True)).to_be_disabled()
                page.wait_for_timeout(15100)
                expect(page.get_by_role("heading", name="Network Error", exact=True)).to_be_visible()
                try:
                    held.pop().abort()
                except Error:
                    pass
                dismiss(page)
                report["checks"].append("Invalid, expired, used, unapproved, suspended, no-session, network failure and request timeout produce distinct retryable error cards")

                for width in (320, 360, 390, 768, 1440):
                    page.set_viewport_size({"width": width, "height": 950})
                    scan(page, "duplicate-viewport")
                    expect(page.get_by_test_id("scan-feedback")).to_be_visible()
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 1"), f"Horizontal overflow at {width}px"
                    bounds = page.get_by_test_id("scan-feedback").bounding_box()
                    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1
                    expect(page.get_by_role("button", name="Scan Next Student", exact=True)).to_be_visible()
                    if width == 390:
                        page.wait_for_timeout(500)
                        assert feedback.evaluate("el => Number(getComputedStyle(el).opacity)") > 0.98
                        page.screenshot(path=str(OUT / "scanner-feedback-duplicate-mobile.png"), full_page=True)
                    report["viewports"].append(width)
                    dismiss(page)
                report["checks"].append("Duplicate alert and essential dismiss control fit 320/360/390/768/1440px without horizontal overflow and honor reduced motion")

                page.emulate_media(reduced_motion="no-preference")
                scan(page, "duplicate-motion")
                expect(page.get_by_role("heading", name="ATTENDANCE ALREADY MARKED!", exact=True)).to_be_visible()
                page.wait_for_timeout(700)
                assert feedback.evaluate("el => Number(getComputedStyle(el).opacity)") > 0.98
                assert feedback.evaluate("el => getComputedStyle(el).animationName") == "scan-feedback-glow"
                assert feedback.locator(".scan-feedback-icon").evaluate("el => getComputedStyle(el,'::after').animationName") == "scan-feedback-pulse"
                page.screenshot(path=str(OUT / "scanner-feedback-duplicate-animated-desktop.png"), full_page=True)
                page.emulate_media(reduced_motion="reduce")
                assert feedback.evaluate("el => getComputedStyle(el).animationName") == "none"
                assert feedback.locator(".scan-feedback-icon").evaluate("el => getComputedStyle(el,'::after').animationName") == "none"
                dismiss(page)
                report["checks"].append("Normal-motion warning entrance settles visibly with amber glow/pulse; reduced-motion preference disables CSS animations")

                page.get_by_role("button", name="Camera", exact=True).click()
                page.get_by_role("button", name="Start camera", exact=True).click()
                page.wait_for_function("typeof window.__decodeQr === 'function'")
                before = len(scans)
                page.evaluate("() => { for (let i=0;i<10;i++) window.__decodeQr('duplicate-camera'); }")
                expect(page.get_by_role("heading", name="ATTENDANCE ALREADY MARKED!", exact=True)).to_be_visible()
                page.evaluate("() => { for (let i=0;i<10;i++) window.__decodeQr('duplicate-camera'); }")
                page.wait_for_timeout(100)
                assert len(scans) == before + 1, "Camera callbacks bypassed scan feedback lock"
                assert page.evaluate("window.__testCameraPaused") is True
                page.wait_for_timeout(4100)
                expect(page.get_by_test_id("scan-feedback")).to_have_count(0)
                assert page.evaluate("window.__testCameraPaused") is False
                page.evaluate("window.__decodeQr('success-camera')")
                expect(page.get_by_role("heading", name="ATTENDANCE MARKED SUCCESSFULLY!", exact=True)).to_be_visible()
                dismiss(page)
                assert page.evaluate("window.__testCameraPaused") is False
                report["checks"].append("Camera callback adapter pauses/resumes for success and duplicate, ignores repeated callbacks during processing/feedback, and supports immediate dismissal")

                page.get_by_role("button", name="USB / Bluetooth", exact=True).click()
                scan(page, "invalid:UNAUTHORIZED_SCANNER")
                expect(page.get_by_role("heading", name="Scanner Not Authorized", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Review Scanner Access", exact=True)).to_be_visible()
                assert page.get_by_label("Hardware scanner input", exact=True).count() == 0
                report["checks"].append("Scanner authorization rejection shows prominent red card and blocks further input")
                assert not report["errors"], report["errors"]
                report["submitted_events"] = len(scans)
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=10)
            (OUT / "scanner-feedback-browser-results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run()
