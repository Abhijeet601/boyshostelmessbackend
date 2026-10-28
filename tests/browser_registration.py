"""End-to-end enrollment checks. Run only against tests/serve_preview.py."""
import io
import json
import uuid
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect, sync_playwright

BASE='http://127.0.0.1:5175'
OUT=Path(__file__).resolve().parents[2]/'test-artifacts'
checks=[];errors=[];responsive=[]


def login(page,username,password='TestOnly-Safe!42',role='admin'):
    page.goto(BASE+'/login')
    page.get_by_label('Login ID',exact=True).fill(username)
    page.get_by_label('Password',exact=True).fill(password)
    page.get_by_role('button',name='Sign in',exact=True).click()
    page.wait_for_url('**/'+role+'/dashboard')
    expect(page.locator('.welcome-hero, .student-hero')).to_be_visible()


def request(page,path,method='GET',payload=None):
    return page.evaluate('''async ({path,method,payload})=>{
      const token=sessionStorage.getItem('mess_api_token');
      const response=await fetch('/api'+path,{method,headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},...(payload?{body:JSON.stringify(payload)}:{})});
      return {status:response.status,body:await response.json()};
    }''',{'path':path,'method':method,'payload':payload})


with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'])
    context=browser.new_context(viewport={'width':1440,'height':1000})
    def observe(page):
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('console',lambda message:errors.append(message.text) if message.type=='error' and 'Failed to load resource' not in message.text else None)
    context.on('page',observe)
    context.on('response',lambda response:errors.append(f'HTTP {response.status} {response.url}') if response.status>=500 else None)
    applicant=context.new_page();admin=context.new_page()
    suffix=uuid.uuid4().hex[:8];name='New Student '+suffix;username='REGISTER-'+suffix;password='StudentSecure!42'
    applicant.goto(BASE+'/login');applicant.get_by_role('link',name='Create your account').click()
    expect(applicant.get_by_label('Full name',exact=True)).to_be_visible()
    applicant.get_by_role('button',name='Continue',exact=True).click()
    expect(applicant.get_by_label('Full name',exact=True)).to_be_focused()
    applicant.get_by_label('Full name',exact=True).fill(name)
    applicant.get_by_label('Email address',exact=True).fill(suffix+'@example.test')
    applicant.get_by_label('Mobile number',exact=True).fill('9876543210')
    photo=io.BytesIO();Image.new('RGB',(32,32),'#6366f1').save(photo,'PNG')
    applicant.locator('input[type=file]').set_input_files({'name':'portrait.png','mimeType':'image/png','buffer':photo.getvalue()})
    applicant.get_by_role('button',name='Continue',exact=True).click()
    for label,value in [('Roll number','ROLL-'+suffix),('Registration number','REG-'+suffix),('Course','BSc'),('Department','Computer Science'),('Academic year','First year'),('Semester','1'),('Academic session','2026-2027')]:
        applicant.get_by_label(label,exact=True).fill(value)
    applicant.get_by_role('button',name='Continue',exact=True).click()
    applicant.get_by_label('Hostel',exact=True).select_option(label='North Residence')
    applicant.get_by_label('Room number',exact=True).fill('207')
    applicant.get_by_role('button',name='Continue',exact=True).click()
    applicant.get_by_label('Login ID or email',exact=True).fill(username)
    applicant.get_by_label('Password',exact=True).fill(password)
    applicant.get_by_label('Confirm password',exact=True).fill(password)
    applicant.get_by_role('button',name='Continue',exact=True).click()
    expect(applicant.get_by_role('heading',name='Looking good?')).to_be_visible()
    applicant.get_by_role('button',name='Submit Registration for Approval').click()
    applicant.wait_for_url('**/registration/status')
    expect(applicant.get_by_role('heading',name='Registration Submitted Successfully')).to_be_visible()
    assert not applicant.evaluate("sessionStorage.getItem('mess_api_token')")
    checks.append('Five-step registration, photograph upload, review and restricted pending status')
    applicant.screenshot(path=str(OUT/'registration-pending-desktop.png'),full_page=True)
    applicant.goto(BASE+'/login')
    applicant.get_by_label('Login ID',exact=True).fill(username);applicant.get_by_label('Password',exact=True).fill(password)
    applicant.get_by_role('button',name='Sign in',exact=True).click();applicant.wait_for_url('**/registration/status')
    expect(applicant.get_by_text('PENDING',exact=True)).to_be_visible()
    checks.append('Pending login returns to restricted application status without dashboard token')
    login(admin,'QA-ADMIN')
    admin.goto(BASE+'/admin/registration-requests')
    admin.get_by_placeholder('Search name, roll or registration').fill(suffix)
    admin.get_by_role('button',name='Review '+name).click()
    expect(admin.get_by_role('heading',name='Application details',exact=True)).to_be_visible()
    expect(admin.locator('.application-detail img')).to_be_visible()
    admin.screenshot(path=str(OUT/'registration-review-desktop.png'),full_page=True)
    admin.get_by_role('button',name='Approve student',exact=True).click()
    expect(admin.get_by_role('heading',name='Approve this student?',exact=True)).to_be_visible()
    admin.get_by_role('button',name='Confirm approval',exact=True).click()
    expect(admin.get_by_role('dialog')).to_have_count(0)
    applicant.get_by_role('button',name='Check Status',exact=True).click()
    expect(applicant.get_by_role('heading',name='Your Account Has Been Approved!')).to_be_visible()
    checks.append('Scoped admin photo/detail review, approval confirmation and approved applicant status')
    login(applicant,username,password,'student')
    expect(applicant.locator('.attendance-ring')).to_be_visible()
    expect(applicant.locator('.qr-frame svg')).to_be_visible()
    applicant.screenshot(path=str(OUT/'registration-approved-student-desktop.png'),full_page=True)
    token=request(applicant,'/student/qr','POST')['body']['token']
    admin.goto(BASE+'/admin/scanner');admin.get_by_role('button',name='USB / Bluetooth',exact=True).click()
    admin.get_by_label('Hardware scanner input',exact=True).fill(token);admin.get_by_label('Hardware scanner input',exact=True).press('Enter')
    expect(admin.get_by_text('Attendance marked successfully',exact=True)).to_be_visible()
    admin.wait_for_timeout(4200)
    token=request(applicant,'/student/qr','POST')['body']['token']
    admin.get_by_label('Hardware scanner input',exact=True).fill(token);admin.get_by_label('Hardware scanner input',exact=True).press('Enter')
    expect(admin.get_by_role('heading',name='Attendance Already Marked',exact=True)).to_be_visible()
    checks.append('Approved student QR, authorized scanner first attendance and duplicate warning')
    # Rejection and resubmission are separate from the now-approved student.
    other=context.new_page();other.goto(BASE+'/login')
    rejected='REJECT-'+suffix
    payload={'name':'Correction '+suffix,'email':'correct-'+suffix+'@example.test','mobile':'9876543210','admission_number':'CORR-'+suffix,'registration_number':'CORRREG-'+suffix,'hostel':'North Residence','room_number':'301','course':'BSc','department':'Computing','academic_year':'1','semester':'1','academic_session':'2026-2027','login_id':rejected,'password':password,'confirm_password':password}
    result=other.request.post(BASE+'/api/public/registration',data=payload).json()
    other.evaluate('(credential)=>sessionStorage.setItem("smartmess_application",credential)',result['credential'])
    app=other.request.get(BASE+'/api/registration/status',headers={'Authorization':'Bearer '+result['credential']}).json()
    admin.goto(BASE+'/admin/registration-requests');admin.get_by_placeholder('Search name, roll or registration').fill(rejected)
    # Search uses name / roll / registration, so use the common suffix.
    admin.get_by_placeholder('Search name, roll or registration').fill(suffix)
    admin.get_by_role('button',name='Review Correction '+suffix).click();admin.get_by_role('button',name='Reject',exact=True).click()
    admin.get_by_label('Rejection reason',exact=True).fill('Please correct your assigned room.')
    admin.get_by_role('button',name='Confirm rejection',exact=True).click()
    expect(admin.get_by_role('dialog')).to_have_count(0)
    other.goto(BASE+'/registration/status');expect(other.get_by_role('heading',name='Registration Not Approved')).to_be_visible()
    expect(other.get_by_text('Please correct your assigned room.',exact=True)).to_be_visible()
    other.get_by_role('link',name='Correct application').click()
    expect(other.get_by_label('Full name',exact=True)).to_have_value('Correction '+suffix)
    for _ in range(2):other.get_by_role('button',name='Continue',exact=True).click()
    other.get_by_label('Room number',exact=True).fill('302');other.get_by_role('button',name='Continue',exact=True).click()
    other.get_by_label('Password',exact=True).fill(password);other.get_by_label('Confirm password',exact=True).fill(password)
    other.get_by_role('button',name='Continue',exact=True).click();other.get_by_role('button',name='Submit Registration for Approval').click()
    expect(other.get_by_role('heading',name='Registration Submitted Successfully')).to_be_visible()
    checks.append('Required rejection reason, private feedback and corrected application resubmission')
    # Suspension UI revokes already-issued student sessions.
    admin.goto(BASE+'/admin/students');admin.get_by_placeholder('Search name, roll or registration…').fill(suffix)
    admin.get_by_role('button',name='Disable '+name,exact=True).click();admin.get_by_label('Reason',exact=True).fill('Account review')
    admin.get_by_role('button',name='Confirm suspension').click();expect(admin.get_by_role('dialog')).to_have_count(0)
    applicant.reload();applicant.wait_for_url('**/login')
    admin.get_by_role('button',name='Enable '+name,exact=True).click();admin.get_by_label('Reason',exact=True).fill('Review completed')
    admin.get_by_role('button',name='Confirm reactivation').click();expect(admin.get_by_role('dialog')).to_have_count(0)
    checks.append('Suspend/reactivate controls persist and invalidate active student login')
    login(applicant,username,password,'student')
    # Exercise new public and staff layouts at common compact widths.
    for width in (320,375,425,768,1440):
        for page,path in ((other,'/register'),(other,'/registration/status'),(admin,'/admin/registration-requests'),(applicant,'/student/dashboard')):
            page.set_viewport_size({'width':width,'height':900});page.goto(BASE+path)
            page.locator('h1').first.wait_for()
            page.wait_for_timeout(400)
            overflow=page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
            responsive.append({'width':width,'path':path,'overflow':overflow})
            assert not overflow,(width,path)
            if width==375:page.screenshot(path=str(OUT/(path.replace('/','-').strip('-')+'-mobile.png')),full_page=True)
    checks.append('20 responsive checks on registration, status, approval inbox and approved student dashboard')
    other.emulate_media(reduced_motion='reduce');other.goto(BASE+'/register');expect(other.get_by_label('Full name',exact=True)).to_be_visible()
    assert other.evaluate("[...document.querySelectorAll('*')].every(el=>getComputedStyle(el).animationName==='none')")
    checks.append('Reduced motion disables CSS animation and preserves wizard access')
    assert not errors,errors
    OUT.joinpath('browser-registration-results.json').write_text(json.dumps({'checks':checks,'responsive':responsive,'page_errors':errors},indent=2),encoding='utf-8')
    print(json.dumps({'checks':checks,'responsive':len(responsive),'page_errors':errors},indent=2))
    browser.close()
