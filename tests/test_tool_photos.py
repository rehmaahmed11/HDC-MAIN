#!/usr/bin/env python3
"""Tool photos: a saved picture must be *seeable*, and addable later.

The add-tool form accepted a photo, and then nothing on the site showed it in a
way an operator could use: one 48 px thumbnail inside a crowded inventory cell,
no way to enlarge it, and no way at all to give a photo to a tool that already
existed (the edit dialog had no file field).  These tests pin the behaviour the
tools screens now have:

  P-01  uploading with the add-tool form stores a resized WebP next to the tool
  P-02  the inventory row renders a "Show photo" control pointing at that file
  P-03  a tool created without a photo can be given one later, from any tools
        page, through POST /hdc/tool-rental/inventory/<id>/photo
  P-04  replacing a photo changes the cache-busting stamp, so the new picture
        is what the browser shows
  P-05  removing a photo clears the field and deletes the file
  P-06  a file that is not an image, or is too large, is refused with a message
        and leaves the tool as it was
  P-07  the page behind the eye button (a tool's position) shows the photo and
        offers to change it
  P-08  the tools dashboard row shows the same thumbnail
  P-09  the stored file is served, as an image, at the URL the page renders
  P-10  a reader sees photos but is offered no way to change them, and the
        write route refuses them
  P-11  the posted `next` page can only ever be another tools page
  P-12  the shared tool dialogs carry a real CSRF token (an imported macro
        renders the context processor's token empty, which used to make the
        Purchase / Scrap dialogs answer 400)

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' python -m unittest tests.test_tool_photos -v
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from PIL import Image                                       # noqa: E402
from werkzeug.security import generate_password_hash        # noqa: E402

from hdc.app import create_app                              # noqa: E402
from hdc.extensions import db                               # noqa: E402
from hdc.models.auth import HDCUser                         # noqa: E402
from hdc.models.tool_rental import Tool, ToolCategory       # noqa: E402
from hdc.services import tool_photos                        # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']

_PHOTO_URL = re.compile(r'/hdc_static/tool-images/(\d+)\.webp\?v=(\d+)')
_TOKEN_IN_FORM = re.compile(
    r'<form[^>]*id="purchaseModalForm".*?name="_csrf_token"\s+value="([^"]*)"',
    re.DOTALL)


def _jpeg(width=1200, height=900, colour=(198, 40, 40)):
    """A real encoded photo, big enough to prove the resize happened."""
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), colour).save(buffer, format='JPEG')
    buffer.seek(0)
    return buffer


class ToolPhotoTestCase(unittest.TestCase):
    """One isolated app, database and static folder per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-photo-test-')
        self.db_path = os.path.join(self.tmp, 'test.db')
        self.app = create_app({'HDC_DB_PATH': self.db_path,
                               'HDC_INSTANCE_DIR': self.tmp})
        # Uploaded photos must never land in the repository checkout.
        self.static_dir = os.path.join(self.tmp, 'static')
        os.makedirs(self.static_dir, exist_ok=True)
        self.app.static_folder = self.static_dir
        self.photo_dir = os.path.join(self.static_dir, 'tool-images')

        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login('admin', ADMIN_PASSWORD)

        self.category = ToolCategory(name='Concrete')
        db.session.add(self.category)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _csrf(html):
        match = re.search(r'name="_csrf_token" value="([^"]+)"', html or '')
        return match.group(1) if match else ''

    def _login(self, username, password):
        page = self.client.get('/hdc/login')
        self.client.post('/hdc/login', data={
            'username': username, 'password': password,
            '_csrf_token': self._csrf(page.get_data(as_text=True)),
        }, follow_redirects=True)

    def _switch_user(self, username, password):
        """Sign this test in as somebody else.

        Flask-Login parks the signed-in user on ``g``, and the app context this
        test pushed is shared by every request its clients make — so a second
        login inside the same context silently keeps the first user (the trap
        the CRUD-gap tests note).  A fresh context *and* a fresh client is the
        only way to really be that second user.
        """
        self.ctx.pop()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self._login(username, password)

    def _token(self):
        with self.client.session_transaction() as session:
            return session.get('_csrf_token', '')

    def _make_tool(self, code='T-0001', name='Jack Hammer', qty=4.0):
        tool = Tool(tool_code=code, name=name, unit='pcs',
                    category_id=self.category.id, total_quantity=qty,
                    purchase_cost=1500.0, rental_rate_per_day=400.0,
                    condition='good', status='active', is_void=False)
        db.session.add(tool)
        db.session.commit()
        return tool

    def _tool(self, tool_id):
        db.session.expire_all()
        return db.session.get(Tool, int(tool_id))

    def _photo_file(self, tool_id):
        return os.path.join(self.photo_dir, f'{int(tool_id)}.webp')

    def _add_tool_with_photo(self, name='Vibrator', filename='photo.jpg'):
        """Create a tool through the real inventory form, with a photo."""
        page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        response = self.client.post(
            '/hdc/tool-rental/inventory',
            data={
                '_csrf_token': self._csrf(page),
                'action': 'add_tool',
                'name': name,
                'tool_code': '',
                'category_id': str(self.category.id),
                'unit': 'pcs',
                'condition': 'good',
                'total_quantity': '3',
                'purchase_cost': '900',
                'rental_rate_per_day': '250',
                'tool_image': (_jpeg(), filename),
            },
            content_type='multipart/form-data')
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:400])
        tool = Tool.query.filter_by(name=name).first()
        self.assertIsNotNone(tool, 'the add-tool form did not create the tool')
        return tool

    def _post_photo(self, tool, payload=None, image=None, filename='photo.jpg'):
        data = {'_csrf_token': self._token(), 'action': 'upload'}
        data.update(payload or {})
        if image is not None:
            data['tool_image'] = (image, filename)
        return self.client.post(f'/hdc/tool-rental/inventory/{tool.id}/photo',
                                data=data, content_type='multipart/form-data')

    def _photo_urls(self, html):
        return _PHOTO_URL.findall(html or '')

    # ==================================================================
    # P-01 — the create form stores a compact WebP
    # ==================================================================
    def test_add_tool_form_stores_resized_webp(self):
        tool = self._add_tool_with_photo()
        self.assertEqual(tool.image_path, f'tool-images/{tool.id}.webp')

        path = self._photo_file(tool.id)
        self.assertTrue(os.path.isfile(path), 'the photo file was not written')
        with Image.open(path) as stored:
            self.assertEqual(stored.format, 'WEBP')
            self.assertLessEqual(max(stored.size), 640,
                                 'the stored photo was not resized to 640 px')
        self.assertLess(os.path.getsize(path), 200 * 1024,
                        'the stored photo is not compact')

    def test_add_tool_form_refuses_a_file_that_is_not_an_image(self):
        page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        response = self.client.post(
            '/hdc/tool-rental/inventory',
            data={
                '_csrf_token': self._csrf(page),
                'action': 'add_tool',
                'name': 'Not A Photo',
                'category_id': str(self.category.id),
                'unit': 'pcs',
                'total_quantity': '1',
                'tool_image': (io.BytesIO(b'this is a receipt, not a picture'),
                               'receipt.txt'),
            },
            content_type='multipart/form-data', follow_redirects=True)
        body = response.get_data(as_text=True)
        self.assertIsNone(Tool.query.filter_by(name='Not A Photo').first(),
                          'a tool was created although its photo was unreadable')
        self.assertIn('not a readable image', body)

    def test_a_refused_opening_purchase_leaves_no_orphan_photo(self):
        """The photo is written before the opening stock behind it is checked.

        A refused purchase rolls the tool back — and SQLite can hand that id to
        the next tool, which would otherwise inherit somebody else's picture.
        """
        from hdc.routes import tool_rental as tool_routes
        original = tool_routes.record_tool_purchase
        tool_routes.record_tool_purchase = lambda **kwargs: (
            False, 'That supplier is blocked.', None)
        try:
            page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
            response = self.client.post(
                '/hdc/tool-rental/inventory',
                data={
                    '_csrf_token': self._csrf(page),
                    'action': 'add_tool',
                    'name': 'Ghost Tool',
                    'category_id': str(self.category.id),
                    'unit': 'pcs',
                    'total_quantity': '5',
                    'tool_image': (_jpeg(), 'ghost.jpg'),
                },
                content_type='multipart/form-data', follow_redirects=True)
        finally:
            tool_routes.record_tool_purchase = original

        self.assertIn('That supplier is blocked.', response.get_data(as_text=True))
        self.assertIsNone(Tool.query.filter_by(name='Ghost Tool').first())
        stored = os.listdir(self.photo_dir) if os.path.isdir(self.photo_dir) else []
        self.assertEqual(stored, [],
                         'the rolled-back tool left a photo file behind')

    # ==================================================================
    # P-02 / P-09 — the row shows the photo, and the file is reachable
    # ==================================================================
    def test_inventory_row_offers_show_photo(self):
        tool = self._add_tool_with_photo()
        page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)

        self.assertIn('data-tool-photo="view"', page,
                      'no Show-photo control on the inventory row')
        self.assertIn('tool_photos.js', page, 'the photo dialog script is missing')
        self.assertIn('toolPhotoViewerModal', page, 'the photo viewer is missing')
        self.assertIn('toolPhotoUploadModal', page, 'the photo uploader is missing')

        urls = self._photo_urls(page)
        self.assertIn((str(tool.id), ), [(tool_id,) for tool_id, _stamp in urls],
                      'the row does not point at this tool\'s photo')

    def test_stored_photo_is_served_as_an_image(self):
        tool = self._add_tool_with_photo()
        page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        url = next(path for path, _stamp in self._photo_urls(page)
                   if path == str(tool.id))
        response = self.client.get(f'/hdc_static/tool-images/{url}.webp')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/webp')
        self.assertGreater(len(response.get_data()), 100)

    def test_a_tool_without_a_photo_offers_to_add_one(self):
        tool = self._make_tool(code='T-NO-PHOTO', name='Shovel')
        page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        self.assertIn(f'data-tool-id="{tool.id}"', page)
        self.assertIn('data-tool-photo="edit"', page,
                      'a tool with no photo must offer a way to add one')
        self.assertNotIn(f'tool-images/{tool.id}.webp', page)

    # ==================================================================
    # P-03 / P-04 / P-05 — add later, replace, remove
    # ==================================================================
    def test_photo_can_be_added_to_an_existing_tool(self):
        tool = self._make_tool(code='T-LATER', name='Concrete Mixer')
        self.assertIsNone(tool.image_path)

        response = self._post_photo(tool, image=_jpeg(),
                                    payload={'next': '/hdc/tool-rental/inventory'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'].split('?')[0],
                         '/hdc/tool-rental/inventory')

        saved = self._tool(tool.id)
        self.assertEqual(saved.image_path, f'tool-images/{saved.id}.webp')
        self.assertTrue(os.path.isfile(self._photo_file(saved.id)))

        page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        self.assertIn(f'/hdc_static/tool-images/{saved.id}.webp', page)

    def test_replacing_a_photo_changes_the_cache_stamp(self):
        tool = self._make_tool(code='T-REPLACE', name='Drill')
        self._post_photo(tool, image=_jpeg(colour=(20, 90, 200)))
        first_page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        first = dict((tool_id, stamp) for tool_id, stamp in self._photo_urls(first_page))

        # A second upload keeps the file name, so only the stamp can tell the
        # browser that the picture changed.
        self._post_photo(tool, image=_jpeg(colour=(240, 200, 20)))
        second_page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        second = dict((tool_id, stamp) for tool_id, stamp in self._photo_urls(second_page))

        self.assertIn(str(tool.id), first)
        self.assertIn(str(tool.id), second)
        self.assertNotEqual(first[str(tool.id)], second[str(tool.id)],
                            'a replaced photo kept its old cache stamp')
        self.assertEqual(len(os.listdir(self.photo_dir)), 1,
                         'replacing a photo left another file behind')

    def test_removing_a_photo_clears_the_field_and_the_file(self):
        tool = self._make_tool(code='T-REMOVE', name='Scaffolding Clamp')
        self._post_photo(tool, image=_jpeg())
        self.assertTrue(os.path.isfile(self._photo_file(tool.id)))

        response = self._post_photo(tool, payload={'action': 'remove'})
        self.assertEqual(response.status_code, 302)

        saved = self._tool(tool.id)
        self.assertIsNone(saved.image_path)
        self.assertFalse(os.path.exists(self._photo_file(tool.id)),
                         'the photo file survived its removal')

        page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        self.assertNotIn(f'tool-images/{tool.id}.webp', page)

    def test_removing_a_photo_that_was_never_there_says_so(self):
        tool = self._make_tool(code='T-NONE', name='Wheelbarrow')
        response = self.client.post(
            f'/hdc/tool-rental/inventory/{tool.id}/photo',
            data={'_csrf_token': self._token(), 'action': 'remove'},
            follow_redirects=True)
        self.assertIn('no photo to remove', response.get_data(as_text=True))

    # ==================================================================
    # P-06 — refusals leave the tool untouched
    # ==================================================================
    def test_non_image_upload_is_refused_and_keeps_the_old_photo(self):
        tool = self._make_tool(code='T-BAD', name='Needle Vibrator')
        self._post_photo(tool, image=_jpeg())
        before = self._tool(tool.id).image_path

        response = self._post_photo(tool, image=io.BytesIO(b'plain text'),
                                    filename='notes.txt')
        self.assertEqual(response.status_code, 302)
        body = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        self.assertEqual(self._tool(tool.id).image_path, before,
                         'a refused upload changed the stored photo')
        self.assertTrue(os.path.isfile(self._photo_file(tool.id)))
        self.assertIn(f'tool-images/{tool.id}.webp', body)

    def test_oversize_upload_is_refused(self):
        """A 12 MB ceiling: phone photos pass, the absurd is refused.

        The size check runs before the image is decoded, so an oversized blob
        is turned away without PIL ever touching it.
        """
        tool = self._make_tool(code='T-BIG', name='Tower Crane')
        oversized = io.BytesIO(b'0' * (tool_photos.MAX_TOOL_PHOTO_BYTES + 1024))
        response = self._post_photo(tool, image=oversized, filename='huge.jpg')
        self.assertEqual(response.status_code, 302)

        body = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        self.assertIn('the limit is 12 MB', body)
        self.assertIsNone(self._tool(tool.id).image_path)
        self.assertFalse(os.path.exists(self._photo_file(tool.id)))

    def test_upload_without_a_file_is_refused(self):
        tool = self._make_tool(code='T-EMPTY', name='Spirit Level')
        response = self._post_photo(tool)          # no tool_image at all
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self._tool(tool.id).image_path)

    # ==================================================================
    # P-07 / P-08 — the eye-button page and the dashboard show it too
    # ==================================================================
    def test_position_page_behind_the_eye_button_shows_the_photo(self):
        tool = self._make_tool(code='T-EYE', name='Power Trowel')
        self._post_photo(tool, image=_jpeg())

        page = self.client.get(f'/hdc/tool-rental/tool/{tool.id}').get_data(as_text=True)
        self.assertIn(f'/hdc_static/tool-images/{tool.id}.webp', page)
        self.assertIn('data-tool-photo="view"', page)
        self.assertIn('toolPhotoViewerModal', page)
        self.assertIn('Change / remove photo', page,
                      'a writer on the position page cannot replace the photo')

        # The eye button in every tools list points here.
        listing = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        self.assertIn(f'/hdc/tool-rental/tool/{tool.id}"', listing)

    def test_dashboard_row_shows_the_photo(self):
        tool = self._make_tool(code='T-DASH', name='Plate Compactor')
        self._post_photo(tool, image=_jpeg())

        page = self.client.get('/hdc/tool-rental/dashboard').get_data(as_text=True)
        self.assertIn(f'/hdc_static/tool-images/{tool.id}.webp', page)
        self.assertIn('data-tool-photo="view"', page)
        self.assertIn('toolPhotoViewerModal', page)

    def test_a_missing_photo_file_is_not_offered_as_a_picture(self):
        """A checkout copied without its uploads must not render broken images."""
        tool = self._make_tool(code='T-GONE', name='Lost Photo')
        tool.image_path = f'tool-images/{tool.id}.webp'
        db.session.commit()
        self.assertFalse(os.path.exists(self._photo_file(tool.id)))

        page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        self.assertNotIn(f'tool-images/{tool.id}.webp', page)
        self.assertIn('data-tool-photo="edit"', page)

    # ==================================================================
    # P-10 — readers see photos, writers change them
    # ==================================================================
    def test_reader_sees_the_photo_but_cannot_change_it(self):
        tool = self._make_tool(code='T-READ', name='Measuring Wheel')
        self._post_photo(tool, image=_jpeg())
        tool_id = int(tool.id)      # switching context detaches the ORM object

        reader = HDCUser(username='storekeeper', role='staff',
                         password_hash=generate_password_hash('Store@1234'))
        db.session.add(reader)
        db.session.commit()

        self._switch_user('storekeeper', 'Store@1234')
        listing = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        self.assertIn(f'/hdc_static/tool-images/{tool_id}.webp', listing,
                      'a reader cannot see the saved photo')
        self.assertIn('data-tool-photo="view"', listing)
        self.assertNotIn('data-tool-photo="edit"', listing,
                         'a reader is offered the photo uploader')
        self.assertNotIn('toolPhotoUploadModal', listing)

        refused = self.client.post(
            f'/hdc/tool-rental/inventory/{tool_id}/photo',
            data={'_csrf_token': self._token(), 'action': 'remove'},
            content_type='multipart/form-data')
        self.assertEqual(refused.status_code, 302)
        self.assertNotIn('/hdc/tool-rental/inventory', refused.headers['Location'])
        self.assertIsNotNone(self._tool(tool_id).image_path,
                             'a reader removed the photo')
        self.assertTrue(os.path.isfile(self._photo_file(tool_id)))

    # ==================================================================
    # P-11 — the dialog returns only to a tools page
    # ==================================================================
    def test_posted_next_page_is_confined_to_the_tools_section(self):
        tool = self._make_tool(code='T-NEXT', name='Angle Grinder')
        for hostile in ('https://evil.example/steal', '//evil.example/steal',
                        '/hdc/settings', 'javascript:alert(1)',
                        '/hdc/tool-rental/../../hdc/settings'):
            response = self._post_photo(tool, image=_jpeg(), payload={'next': hostile})
            self.assertEqual(response.status_code, 302, hostile)
            self.assertTrue(
                response.headers['Location'].startswith('/hdc/tool-rental/inventory'),
                f'next={hostile} redirected to {response.headers["Location"]}')

        allowed = '/hdc/tool-rental/inventory?q=T-NEXT'
        response = self._post_photo(tool, image=_jpeg(), payload={'next': allowed})
        self.assertEqual(response.headers['Location'], allowed)

    # ==================================================================
    # P-12 — CSRF regression for the shared tool dialogs
    # ==================================================================
    def test_shared_tool_dialogs_carry_a_real_csrf_token(self):
        self._make_tool(code='T-CSRF', name='Formwork Panel')
        page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)

        match = _TOKEN_IN_FORM.search(page)
        self.assertIsNotNone(match, 'the purchase dialog form is missing')
        token = match.group(1)
        self.assertTrue(token.strip(),
                        'the purchase dialog renders an empty CSRF token')
        self.assertEqual(token, self._token())

        tool = Tool.query.filter_by(tool_code='T-CSRF').first()
        response = self.client.post('/hdc/tool-rental/inventory/purchase', data={
            '_csrf_token': token, 'tool_id': str(tool.id),
            'qty': '2', 'unit_cost': '1500'})
        self.assertEqual(response.status_code, 302,
                         'posting the dialog exactly as rendered was refused')


if __name__ == '__main__':
    unittest.main(verbosity=2)
