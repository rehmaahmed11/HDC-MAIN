"""Templates must not leak stray markup after the document end.

Text sitting after ``</html>`` is still rendered by browsers at the very
bottom of the page, so a duplicated closing fragment in the shared layout
shows up as garbled characters on every screen (reported on User Management).
"""
from pathlib import Path
import unittest

from qa_support import IsolatedAppTest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / 'templates'


class TemplateDocumentEndTest(unittest.TestCase):
    def test_no_template_carries_content_after_the_closing_html_tag(self):
        offenders = {}
        for template in sorted(TEMPLATES.rglob('*.html')):
            source = template.read_text(encoding='utf-8')
            marker = '</html>'
            end = source.find(marker)
            if end == -1:
                continue
            remainder = source[end + len(marker):]
            if remainder.strip():
                offenders[str(template.relative_to(ROOT))] = remainder.strip()[:120]
            self.assertEqual(
                source.count(marker), 1,
                f'{template.relative_to(ROOT)} closes the document more than once')
        self.assertEqual(offenders, {})


class RenderedDocumentEndTest(IsolatedAppTest):
    def test_user_role_management_page_renders_nothing_after_document_end(self):
        response = self.client.get('/hdc/users')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        end = body.find('</html>')
        self.assertNotEqual(end, -1)
        self.assertEqual(body.count('</html>'), 1)
        self.assertEqual(body[end + len('</html>'):].strip(), '')


if __name__ == '__main__':
    unittest.main()
