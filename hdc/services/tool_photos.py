"""Tool photos: store one compact WebP per tool and hand templates its URL.

Why this module exists
──────────────────────
The inventory form could already accept a photo, but the picture had nowhere to
live on screen: it was rendered as a 48 px thumbnail inside one crowded table
cell, an existing tool could never be given a photo at all (the edit dialog had
no file field), and nothing told the operator whether the upload had actually
worked.  So a saved photo looked exactly like no photo.

This module keeps both halves of the feature in one place:

* write side — :func:`save_tool_photo` / :func:`remove_tool_photo` validate the
  upload, resize it and store it as ``static/hdc/tool-images/<tool id>.webp``;
* read side — :func:`tool_photo_url` builds the URL every tools screen uses,
  stamped with the stored file's modified time so a replaced photo is never
  served from the browser cache.  A path whose file has gone missing reads as
  "no photo", which keeps a copied checkout from rendering broken images.

The stored file name is derived from the tool id (never from the uploaded
name), and the value written to ``Tool.image_path`` is always relative to the
static folder, so a deployment can move its checkout without breaking links.
"""

from __future__ import annotations

import os
import stat

from flask import current_app, has_app_context, has_request_context, url_for
from PIL import Image, ImageOps, UnidentifiedImageError

from hdc.config import BASE_DIR
from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive

# Folder inside the app's static folder (served as /hdc_static/tool-images/…).
TOOL_PHOTO_DIRNAME = 'tool-images'
# Upload ceiling: phone photos are 3–8 MB, so 12 MB rejects only the absurd.
MAX_TOOL_PHOTO_BYTES = 12 * 1024 * 1024
# Stored size: big enough to read a nameplate in the lightbox, small enough
# that a hundred tools stay under a few MB on disk.
MAX_TOOL_PHOTO_PX = 640
MAX_TOOL_PHOTO_PIXELS = 25_000_000          # decompression-bomb guard
TOOL_PHOTO_QUALITY = 55
TOOL_PHOTO_STATIC_URL = '/hdc_static/' + TOOL_PHOTO_DIRNAME


def _static_folder(app=None):
    """Absolute path of the static folder, with or without an app context."""
    target = app if app is not None else (current_app if has_app_context() else None)
    folder = getattr(target, 'static_folder', None) if target is not None else None
    return folder or os.path.join(BASE_DIR, 'static', 'hdc')


def tool_photo_dir(app=None) -> str:
    """Directory that holds the stored tool photos."""
    return os.path.join(_static_folder(app), TOOL_PHOTO_DIRNAME)


def tool_photo_filename(tool_id) -> str:
    return f'{int(tool_id)}.webp'


def tool_photo_path(tool, app=None) -> str | None:
    """Absolute path of a tool's photo file, or None when it has none.

    ``Tool.image_path`` stores a static-relative path; anything that does not
    resolve *inside* the static folder is ignored rather than followed.
    """
    relative = (getattr(tool, 'image_path', None) or '').strip()
    if not relative:
        return None
    root = os.path.abspath(_static_folder(app))
    candidate = os.path.abspath(os.path.join(root, relative.replace('\\', '/')))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate


def _photo_stat(tool, app=None):
    """``os.stat_result`` for a tool's photo file, or None when there is none.

    One stat answers both questions a list page has about a photo: is it there
    at all, and when did it last change.  A stored path that has gone missing
    (a checkout copied without its uploads) is reported as "no photo", so the
    page offers to add one instead of rendering a broken image.
    """
    path = tool_photo_path(tool, app)
    if not path:
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return info


def tool_has_photo(tool) -> bool:
    """True when the tool points at a photo file that is actually on disk."""
    return _photo_stat(tool) is not None


def tool_photo_stamp(tool, info=None) -> int:
    """Cache-buster for a photo URL, in microseconds since the epoch.

    Replacing a photo keeps the same file name, so the browser would happily
    show the old picture forever.  The file's mtime changes on every write and
    is precise enough that two uploads in the same second still differ; the
    row's timestamps cover a photo whose file has been copied around.
    """
    if info is None:
        info = _photo_stat(tool)
    if info is not None:
        return max(1, int(info.st_mtime_ns // 1000))
    for value in (getattr(tool, 'updated_at', None), getattr(tool, 'created_at', None)):
        try:
            if value:
                return max(1, int(value.timestamp()))
        except (AttributeError, ValueError, OSError, OverflowError):
            continue
    return 1


def tool_photo_url(tool) -> str | None:
    """URL of a tool's photo, or None when it has none.

    Safe to call outside a request context (receipts, scripts): it falls back
    to the literal static prefix the deployment serves.
    """
    info = _photo_stat(tool)
    if info is None:
        return None
    relative = (tool.image_path or '').strip().replace('\\', '/')
    stamp = tool_photo_stamp(tool, info)
    if has_request_context():
        return url_for('static', filename=relative, v=stamp)
    return f'{TOOL_PHOTO_STATIC_URL}/{relative}?v={stamp}'


# --------------------------------------------------------------------------- #
# write side
# --------------------------------------------------------------------------- #
def save_tool_photo(tool, upload, app=None, commit=False):
    """Validate ``upload`` (a werkzeug FileStorage) and store it for ``tool``.

    Returns ``(ok, message)``.  Nothing is committed unless ``commit=True``;
    the caller decides whether the photo is part of a bigger transaction (the
    add-tool form saves the tool and its opening stock in one commit).
    """
    filename = (getattr(upload, 'filename', '') or '').strip()
    if not filename:
        return False, 'Choose a photo file first.'

    stream = getattr(upload, 'stream', None)
    if stream is None:
        return False, 'Please upload a valid image file.'

    try:
        try:
            stream.seek(0, os.SEEK_END)
            upload_bytes = stream.tell()
        except (AttributeError, OSError, ValueError):
            upload_bytes = 0
        finally:
            try:
                stream.seek(0)
            except (AttributeError, OSError, ValueError):
                pass
        if upload_bytes > MAX_TOOL_PHOTO_BYTES:
            return False, (f'That photo is {upload_bytes / (1024 * 1024):.1f} MB — '
                           f'the limit is {MAX_TOOL_PHOTO_BYTES // (1024 * 1024)} MB.')

        Image.MAX_IMAGE_PIXELS = MAX_TOOL_PHOTO_PIXELS
        with Image.open(stream) as source:
            image = ImageOps.exif_transpose(source)      # phone photos carry rotation
            image.thumbnail((MAX_TOOL_PHOTO_PX, MAX_TOOL_PHOTO_PX), Image.Resampling.LANCZOS)
            if image.mode not in ('RGB', 'RGBA'):        # WebP cannot store P/CMYK
                image = image.convert('RGB')
            photo_dir = tool_photo_dir(app)
            os.makedirs(photo_dir, exist_ok=True)
            target = os.path.join(photo_dir, tool_photo_filename(tool.id))
            image.save(target, format='WEBP',
                       quality=TOOL_PHOTO_QUALITY, method=6)
    except UnidentifiedImageError:
        return False, 'That file is not a readable image — use a JPG or PNG photo.'
    except (OSError, ValueError) as exc:
        message = str(exc).strip()
        return False, (message or 'The photo could not be saved.')

    previous = tool_photo_path(tool, app)
    tool.image_path = f'{TOOL_PHOTO_DIRNAME}/{tool_photo_filename(tool.id)}'
    tool.updated_at = _pkt_now_naive()
    if commit:
        db.session.commit()
    # Replacing a photo must not leave the previous file behind when it was
    # stored under another name (an upload from before this module existed).
    if previous and os.path.abspath(previous) != os.path.abspath(
            tool_photo_path(tool, app) or previous):
        discard_tool_photo(previous)
    return True, f'Photo saved for {tool.name}.'


def remove_tool_photo(tool, app=None, commit=False):
    """Delete a tool's photo file and clear its ``image_path``.

    Returns ``(ok, message)``; removing a photo from a tool that has none is
    reported as a plain notice rather than an error.
    """
    if not (getattr(tool, 'image_path', None) or '').strip():
        return False, f'{tool.name} has no photo to remove.'
    path = tool_photo_path(tool, app)
    tool.image_path = None
    tool.updated_at = _pkt_now_naive()
    if commit:
        db.session.commit()
    discard_tool_photo(path)
    return True, f'Photo removed from {tool.name}.'


def discard_tool_photo(path):
    """Delete a photo file whose tool row was rolled back.

    The add-tool form writes the picture before the opening purchase behind it
    is validated, so a refused purchase must not leave the file behind: SQLite
    can hand the rolled-back id to the next tool, which would then show a
    picture that was never its own.
    """
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass

