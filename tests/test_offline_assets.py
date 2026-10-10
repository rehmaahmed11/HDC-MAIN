"""Critical UI libraries must not depend on public CDN availability."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class OfflineAssetTest(unittest.TestCase):
    def test_shared_and_login_dependencies_are_local(self):
        for name in ('shared/base.html', 'auth/login.html'):
            source = (ROOT / 'templates/hdc' / name).read_text()
            self.assertNotRegex(source, r'(?:src|href)=[\"\']https?://')
            for path in re.findall(r'(?:src|href)=[\"\'](/hdc_static/[^\"\']+)', source):
                # The ?v=<stamp> cache-buster is not part of the path.
                clean = path.split('?')[0]
                clean_path = clean.removeprefix('/hdc_static/')
                self.assertTrue((ROOT / 'static/hdc' / clean_path).is_file(), clean_path)
        self.assertNotIn('fonts.googleapis.com', (ROOT / 'static/hdc/css/hdc.css').read_text())

    def test_vendor_css_resources_and_licenses_exist(self):
        vendor = ROOT / 'static/hdc/vendor'
        for name in ('bootstrap', 'chartjs', 'fontawesome', 'jakarta'):
            self.assertTrue((vendor / name / 'LICENSE').is_file(), name)
        for css in vendor.rglob('*.css'):
            for url in re.findall(r'url\([\"\']?([^\)\"\']+)', css.read_text()):
                if not url.startswith('data:'):
                    self.assertTrue((css.parent / url.split('?')[0].split('#')[0]).is_file(), url)
