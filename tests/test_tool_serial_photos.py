#!/usr/bin/env python3
"""Tool photos per *serial piece*, not per item type.

A tool type is a kind of thing; a serial piece is the actual object on site.
"One shovel" is a tool type with five pieces — ``Shovel No 1`` … ``Shovel
No 5`` — and every one of them can look different in the yard (a new handle,
a paint mark, a bent blade).  So the picture belongs to the *piece*: the first
photo feature stored one picture per tool type, which meant five shovels could
only ever show one picture between them.

These tests pin the behaviour the tools screens now have:

  S-01  a photo saved for one piece is stored for that piece alone
  S-02  all five pieces of one tool type can carry their own picture
  S-03  the tool's position page offers a picture for every piece
  S-04  the *Track by Serial* page offers the piece's own picture
  S-05  the *Manage Markings* page offers the piece's own picture
  S-06  removing one piece's picture leaves the other pieces alone
  S-07  a reader sees the piece pictures but cannot change them
  S-08  the stored piece picture is served, as an image, at the rendered URL
  S-09  the inventory row says how many of the tool's pieces are photographed
  S-10  the posted `next` page can only ever be another tools page
  S-11  a refused upload (not an image / too large) leaves the piece as it was

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' python -m unittest tests.test_tool_serial_photos -v
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
from hdc.models.tool_rental import Tool, ToolCategory, ToolSerial   # noqa: E402
from hdc.services import tool_photos                        # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']

_SERIAL_PHOTO_URL = re.compile(r'(/hdc_static/tool-images/serial-\d+\.webp)\?v=\d+')


def _jpeg(width=1200, height=900, colour=(198, 40, 40)):
    """A real encoded photo, big enough to prove the resize happened."""
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), colour).save(buffer, format='JPEG')
    buffer.seek(0)
    return buffer


class SerialPhotoTestCase(unittest.TestCase):
    """One isolated app, database and static folder per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-serial-photo-test-')
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

        self.category = ToolCategory(name='Hand Tools')
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
        """Sign this test in as somebody else (a fresh context *and* client)."""
        self.ctx.pop()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self._login(username, password)

    def _token(self):
        with self.client.session_transaction() as session:
            return session.get('_csrf_token', '')

    def _make_shovel(self, pieces=5):
        """A tool type with five serial pieces — the case from the field."""
        tool = Tool(tool_code='SHV-0001', name='Shovel', unit='pcs',
                    category_id=self.category.id, total_quantity=float(pieces),
                    purchase_cost=900.0, rental_rate_per_day=150.0,
                    condition='good', status='active', is_void=False)
        db.session.add(tool)
        db.session.flush()
        serials = []
        for number in range(1, pieces + 1):
            serial = ToolSerial(serial_number=f'Shovel No {number}',
                                tool_id=tool.id, is_in_store=True,
                                is_scrapped=False, status='in_store',
                                condition='good')
            db.session.add(serial)
            serials.append(serial)
        db.session.commit()
        return tool, serials

    def _serial(self, serial_id):
        db.session.expire_all()
        return db.session.get(ToolSerial, int(serial_id))

    def _photo_file(self, serial_id):
        return os.path.join(self.photo_dir, f'serial-{int(serial_id)}.webp')

    def _post_photo(self, serial, payload=None, image=None, filename='photo.jpg'):
        data = {'_csrf_token': self._token(), 'action': 'upload'}
        data.update(payload or {})
        if image is not None:
            data['tool_image'] = (image, filename)
        return self.client.post(f'/hdc/tool-rental/serial/{serial.id}/photo',
                                data=data, content_type='multipart/form-data')

    def _serial_photo_urls(self, html):
        return _SERIAL_PHOTO_URL.findall(html or '')

    def _serial_ids_in_urls(self, html):
        """The serial ids whose picture a page points at."""
        return [int(re.search(r'serial-(\d+)\.webp', path).group(1))
                for path in self._serial_photo_urls(html)]

    # ==================================================================
    # S-01 — the picture belongs to the piece
    # ==================================================================
    def test_photo_is_stored_for_the_piece_and_not_for_the_tool_type(self):
        tool, serials = self._make_shovel()
        first, second = serials[0], serials[1]

        response = self._post_photo(first, image=_jpeg())
        self.assertEqual(response.status_code, 302)

        saved = self._serial(first.id)
        self.assertEqual(saved.image_path, f'tool-images/serial-{first.id}.webp')
        self.assertTrue(os.path.isfile(self._photo_file(first.id)))

        # The tool type keeps its own (empty) picture, and the other piece of
        # the same type is untouched.
        db.session.expire_all()
        self.assertIsNone(db.session.get(Tool, tool.id).image_path)
        self.assertIsNone(self._serial(second.id).image_path)
        self.assertFalse(os.path.exists(self._photo_file(second.id)))

    # ==================================================================
    # S-02 — five shovels, five pictures
    # ==================================================================
    def test_every_piece_of_one_tool_type_can_carry_its_own_picture(self):
        tool, serials = self._make_shovel()
        colours = [(200, 30, 30), (30, 200, 30), (30, 30, 200),
                   (240, 200, 20), (240, 120, 20)]

        for serial, colour in zip(serials, colours):
            response = self._post_photo(serial, image=_jpeg(colour=colour))
            self.assertEqual(response.status_code, 302)

        for serial in serials:
            saved = self._serial(serial.id)
            self.assertEqual(saved.image_path,
                             f'tool-images/serial-{serial.id}.webp')
            self.assertTrue(os.path.isfile(self._photo_file(serial.id)))

        self.assertEqual(len(os.listdir(self.photo_dir)), 5,
                         'the five pieces did not get five separate pictures')

        # The inventory row says how many of the pieces are photographed.
        page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        self.assertIn('5/5', page)

    def test_a_piece_picture_does_not_replace_the_tool_type_picture(self):
        """A type picture and a piece picture are two different files."""
        # The tool is created the way the field creates it: with a picture of
        # the type and three pieces of opening stock.
        page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        response = self.client.post(
            '/hdc/tool-rental/inventory',
            data={'_csrf_token': self._csrf(page), 'action': 'add_tool',
                  'name': 'Wheelbarrow', 'category_id': str(self.category.id),
                  'unit': 'pcs', 'total_quantity': '3',
                  'tool_image': (_jpeg(), 'wheelbarrow.jpg')},
            content_type='multipart/form-data')
        self.assertEqual(response.status_code, 302)
        tool = Tool.query.filter_by(name='Wheelbarrow').first()
        self.assertIsNotNone(tool)
        type_photo = tool.image_path
        self.assertTrue(type_photo.endswith('.webp'))
        self.assertNotIn('serial-', type_photo)

        serials = tool.active_serials
        self.assertEqual(len(serials), 3)
        self._post_photo(serials[0], image=_jpeg())

        db.session.expire_all()
        self.assertEqual(db.session.get(Tool, tool.id).image_path, type_photo,
                         'the piece picture overwrote the type picture')
        self.assertEqual(self._serial(serials[0].id).image_path,
                         f'tool-images/serial-{serials[0].id}.webp')
        # Both files are on disk, side by side.
        self.assertTrue(os.path.isfile(os.path.join(self.static_dir, type_photo)))
        self.assertTrue(os.path.isfile(self._photo_file(serials[0].id)))

    # ==================================================================
    # S-03 / S-04 / S-05 — the screens that name a piece
    # ==================================================================
    def test_position_page_offers_a_picture_for_every_piece(self):
        tool, serials = self._make_shovel()
        self._post_photo(serials[0], image=_jpeg())

        page = self.client.get(f'/hdc/tool-rental/tool/{tool.id}').get_data(as_text=True)
        for serial in serials:
            self.assertIn(f'data-photo-target-id="{serial.id}"', page,
                          f'no picture control for {serial.serial_number}')
        self.assertIn('1 of 5 pieces photographed', page)
        # The saved picture is shown, the others still offer to be added.
        self.assertIn(f'/hdc_static/tool-images/serial-{serials[0].id}.webp', page)
        self.assertIn('data-tool-photo="edit"', page)

    def test_track_by_serial_page_offers_the_pieces_own_picture(self):
        tool, serials = self._make_shovel()
        self._post_photo(serials[1], image=_jpeg())

        page = self.client.get('/hdc/tool-rental/serials').get_data(as_text=True)
        for serial in serials:
            self.assertIn(f'data-photo-target-id="{serial.id}"', page,
                          f'no picture control for {serial.serial_number}')
        self.assertIn(f'/hdc_static/tool-images/serial-{serials[1].id}.webp', page)
        self.assertIn('toolPhotoViewerModal', page)
        self.assertIn('tool_photos.js', page)

    def test_manage_markings_page_offers_the_pieces_own_picture(self):
        tool, serials = self._make_shovel()
        self._post_photo(serials[2], image=_jpeg())

        page = self.client.get(
            f'/hdc/tool-rental/inventory/{tool.id}/serials/create').get_data(as_text=True)
        for serial in serials:
            self.assertIn(f'data-photo-target-id="{serial.id}"', page,
                          f'no picture control for {serial.serial_number}')
        self.assertIn('1 of 5 pieces photographed', page)

    def test_the_piece_picture_is_served_as_an_image(self):
        tool, serials = self._make_shovel()
        self._post_photo(serials[0], image=_jpeg())

        page = self.client.get('/hdc/tool-rental/serials').get_data(as_text=True)
        path = next(p for p in self._serial_photo_urls(page)
                    if f'serial-{serials[0].id}.webp' in p)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/webp')
        self.assertGreater(len(response.get_data()), 100)

    # ==================================================================
    # S-06 — removing one picture touches only that piece
    # ==================================================================
    def test_removing_one_piece_picture_leaves_the_others_alone(self):
        tool, serials = self._make_shovel()
        self._post_photo(serials[0], image=_jpeg())
        self._post_photo(serials[1], image=_jpeg())

        response = self.client.post(
            f'/hdc/tool-rental/serial/{serials[0].id}/photo',
            data={'_csrf_token': self._token(), 'action': 'remove'},
            content_type='multipart/form-data')
        self.assertEqual(response.status_code, 302)

        self.assertIsNone(self._serial(serials[0].id).image_path)
        self.assertFalse(os.path.exists(self._photo_file(serials[0].id)))
        self.assertEqual(self._serial(serials[1].id).image_path,
                         f'tool-images/serial-{serials[1].id}.webp')
        self.assertTrue(os.path.isfile(self._photo_file(serials[1].id)))

    def test_removing_a_picture_that_was_never_there_says_so(self):
        tool, serials = self._make_shovel()
        response = self.client.post(
            f'/hdc/tool-rental/serial/{serials[0].id}/photo',
            data={'_csrf_token': self._token(), 'action': 'remove'},
            follow_redirects=True)
        self.assertIn('no photo to remove', response.get_data(as_text=True))

    # ==================================================================
    # S-07 — readers see the pictures, writers change them
    # ==================================================================
    def test_reader_sees_the_piece_pictures_but_cannot_change_them(self):
        tool, serials = self._make_shovel()
        self._post_photo(serials[0], image=_jpeg())
        serial_id = int(serials[0].id)

        reader = HDCUser(username='storekeeper', role='staff',
                         password_hash=generate_password_hash('Store@1234'))
        db.session.add(reader)
        db.session.commit()

        self._switch_user('storekeeper', 'Store@1234')
        listing = self.client.get('/hdc/tool-rental/serials').get_data(as_text=True)
        self.assertIn(f'/hdc_static/tool-images/serial-{serial_id}.webp', listing,
                      'a reader cannot see the saved piece picture')
        self.assertNotIn('data-tool-photo="edit"', listing,
                         'a reader is offered the piece photo uploader')

        refused = self.client.post(
            f'/hdc/tool-rental/serial/{serial_id}/photo',
            data={'_csrf_token': self._token(), 'action': 'remove'},
            content_type='multipart/form-data')
        self.assertEqual(refused.status_code, 302)
        self.assertIsNotNone(self._serial(serial_id).image_path,
                             'a reader removed the piece picture')
        self.assertTrue(os.path.isfile(self._photo_file(serial_id)))

    # ==================================================================
    # S-08 — the row tells the operator what is still missing
    # ==================================================================
    def test_inventory_row_shows_how_many_pieces_are_photographed(self):
        tool, serials = self._make_shovel()
        self._post_photo(serials[0], image=_jpeg())
        self._post_photo(serials[1], image=_jpeg())

        page = self.client.get('/hdc/tool-rental/inventory').get_data(as_text=True)
        self.assertIn('2/5', page)
        self.assertIn(f'/hdc/tool-rental/tool/{tool.id}#pieces', page,
                      'the count does not lead to the pieces')

    # ==================================================================
    # S-09 — the dialog returns only to a tools page
    # ==================================================================
    def test_posted_next_page_is_confined_to_the_tools_section(self):
        tool, serials = self._make_shovel()
        for hostile in ('https://evil.example/steal', '//evil.example/steal',
                        '/hdc/settings', 'javascript:alert(1)',
                        '/hdc/tool-rental/../../hdc/settings'):
            response = self._post_photo(serials[0], image=_jpeg(),
                                        payload={'next': hostile})
            self.assertEqual(response.status_code, 302, hostile)
            self.assertTrue(
                response.headers['Location'].startswith('/hdc/tool-rental/inventory'),
                f'next={hostile} redirected to {response.headers["Location"]}')

        allowed = '/hdc/tool-rental/serials?tool_id=%s' % tool.id
        response = self._post_photo(serials[0], image=_jpeg(),
                                    payload={'next': allowed})
        self.assertEqual(response.headers['Location'], allowed)

    # ==================================================================
    # S-10 — a refused upload leaves the piece as it was
    # ==================================================================
    def test_non_image_upload_is_refused_and_keeps_the_old_picture(self):
        tool, serials = self._make_shovel()
        self._post_photo(serials[0], image=_jpeg())
        before = self._serial(serials[0].id).image_path

        response = self._post_photo(serials[0], image=io.BytesIO(b'plain text'),
                                    filename='notes.txt')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._serial(serials[0].id).image_path, before,
                         'a refused upload changed the stored picture')
        self.assertTrue(os.path.isfile(self._photo_file(serials[0].id)))

    def test_oversize_upload_is_refused(self):
        tool, serials = self._make_shovel()
        oversized = io.BytesIO(b'0' * (tool_photos.MAX_TOOL_PHOTO_BYTES + 1024))
        response = self._post_photo(serials[0], image=oversized, filename='huge.jpg')
        self.assertEqual(response.status_code, 302)

        body = self.client.get('/hdc/tool-rental/serials').get_data(as_text=True)
        self.assertIn('the limit is 12 MB', body)
        self.assertIsNone(self._serial(serials[0].id).image_path)
        self.assertFalse(os.path.exists(self._photo_file(serials[0].id)))

    def test_upload_without_a_file_is_refused(self):
        tool, serials = self._make_shovel()
        response = self._post_photo(serials[0])          # no tool_image at all
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self._serial(serials[0].id).image_path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
