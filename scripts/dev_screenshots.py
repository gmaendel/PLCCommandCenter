# Dev-only: regenerate docs/screenshots/ from a dashboard running on 127.0.0.1:18085 (dry-run, DEMO data only -
# never publish screenshots of a live site).
#   python3 -m venv /tmp/shotvenv && /tmp/shotvenv/bin/pip install playwright && /tmp/shotvenv/bin/playwright install chromium
#   /tmp/shotvenv/bin/python scripts/dev_screenshots.py      (not needed in production)
import os, sys, json
from playwright.sync_api import sync_playwright
BASE = 'http://127.0.0.1:18085'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'docs', 'screenshots')
pw_ = os.environ.get('PLCCC_SHOT_PASSWORD')     # dashboard password of the preview server (optional)
report = []
with sync_playwright() as p:
    b = p.chromium.launch()
    for label, vp, mobile, scheme in [('mobile', (390, 844), True, 'light'), ('mobile-dark', (390, 844), True, 'dark'), ('desktop', (1280, 800), False, 'light')]:
        ctx = b.new_context(viewport={'width': vp[0], 'height': vp[1]}, device_scale_factor=2 if mobile else 1,
                            is_mobile=mobile, has_touch=mobile, color_scheme=scheme)
        page = ctx.new_page()
        errors = []
        page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
        page.on('pageerror', lambda e: errors.append(str(e)))
        views = ['overview', 'trends', 'devices', 'settings'] if label != 'mobile-dark' else ['overview', 'trends']
        for v in views:
            page.goto(BASE + '/?p=' + v + '#' + v, wait_until='networkidle')
            page.wait_for_timeout(1800)
            if v == 'devices':
                page.click('[data-toggle="1"]'); page.wait_for_timeout(300)
            if v == 'trends':
                page.wait_for_selector('#trendStats .stat', timeout=8000); page.wait_for_timeout(600)
            ov = page.evaluate('[document.documentElement.scrollWidth, window.innerWidth]')
            fn = f'{label}-{v}.png'
            page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path=os.path.join(OUT, fn), full_page=False)
            if mobile and label == 'mobile' and v in ('overview', 'devices', 'settings'):
                page.screenshot(path=os.path.join(OUT, f'{label}-{v}-fullpage.png'), full_page=True)
            report.append({'shot': fn, 'scrollWidth': ov[0], 'innerWidth': ov[1], 'h_overflow': ov[0] > ov[1]})
        if label == 'mobile':
            # 7-day trend of a Siemens tag
            page.goto(BASE + '/?p=t2#trends', wait_until='networkidle'); page.wait_for_timeout(1200)
            page.select_option('#trendTag', '9'); page.wait_for_timeout(800)
            page.click('#rangeSeg button[data-h="168"]'); page.wait_for_timeout(1500)
            page.screenshot(path=os.path.join(OUT, 'mobile-trends-7d-psi.png')); report.append({'shot': 'mobile-trends-7d-psi.png'})
            # unlock sheet, then tag editor (nothing is saved)
            page.goto(BASE + '/?p=d2#devices', wait_until='networkidle'); page.wait_for_timeout(1200)
            page.click('[data-toggle="3"]'); page.wait_for_timeout(300)
            page.click('[data-edit-tag="4"]'); page.wait_for_timeout(500)
            page.screenshot(path=os.path.join(OUT, 'mobile-unlock-sheet.png')); report.append({'shot': 'mobile-unlock-sheet.png'})
            if pw_:
                page.fill('#pwInput', pw_); page.click('#pwGo'); page.wait_for_timeout(800)
                page.wait_for_timeout(600)
                page.screenshot(path=os.path.join(OUT, 'mobile-tag-editor.png')); report.append({'shot': 'mobile-tag-editor.png'})
        report.append({'context': label, 'console_errors': errors})
        ctx.close()
    b.close()
print(json.dumps(report, indent=1))
