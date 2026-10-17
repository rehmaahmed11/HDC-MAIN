"""Tool photos: store one compact WebP per tool *or per serial piece*.

Why this module exists
──────────────────────
The inventory form could already accept a photo, but the picture had nowhere to
live on screen: it was rendered as a 48 px thumbnail inside one crowded table
cell, an existing tool could never be given a photo at all (the edit dialog had
no file field), and nothing told the operator whether the upload had actually
worked.  So a saved photo looked exactly like no photo.

This module keeps both halves of the feature in one place:

* write side — :func:`save_photo` / :func:`remove_photo` validate the upload,
  resize it and store it as ``static/hdc/tool-images/<file>.webp``;
* read side — :func:`tool_photo_url` builds the URL every tools screen uses,
  stamped with the stored file's modified time so a replaced photo is never
  served from the browser cache.  A path whose file has gone missing reads as
  "no photo", which keeps a copied checkout from rendering broken images.

Two kinds of picture
────────────────────
A tool has two levels, and the field knows the difference:

* the **item type** (``Tool``) — "Shovel" as a kind of thing.  One picture
  answers "what does this tool look like?" and belongs to the whole type;
* the **serial piece** (``ToolSerial``) — "Shovel No 1", "Shovel No 2", … the
  five physical shovels of that type.  Each piece can look different on site
  (a new handle, a paint mark, a broken blade), so each piece keeps its own
  picture.

Both are written through the same functions: the *target* decides the file
name (``<tool id>.webp`` vs ``serial-<serial id>.webp``) and the flash text,
and everything else — validation, resize, re-encode, cache stamp — is identical.

The stored file name is derived from the target id (never from the uploaded
name), and the value written to ``image_path`` is always relative to the static
folder, so a deployment can move its checkout without breaking links.
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

# File-name prefix for a photo that belongs to one serial piece.  A tool type
# keeps the plain "<id>.webp" name it has always had, so pictures saved before
# this module knew about serials keep working untouched.
SERIAL_PHOTO_PREFIX = 'serial-'


def _static_folder(app=None):
    """Absolute path of the static folder, with or without an app context."""
    target = app if app is not None else (current_app if has_app_context() else None)
    folder = getattr(target, 'static_folder', None) if target is not None else None
    return folder or os.path.join(BASE_DIR, 'static', 'hdc')


def tool_photo_dir(app=None) -> str:
    """Directory that holds the stored tool photos."""
    return os.path.join(_static_folder(app), TOOL_PHOTO_DIRNAME)


def is_serial_photo_target(target) -> bool:
    """True when the target is one serial piece rather than a tool type.

    ``ToolSerial`` is imported inside the function so this module stays free
    of model imports at load time (the services and the models import each
    other in different orders depending on the entry point).
    """
    try:
        from hdc.models.tool_rental import ToolSerial
    except Exception:                      # pragma: no cover - defensive
        return hasattr(target, 'serial_number')
    return isinstance(target, ToolSerial)


def photo_kind(target) -> str:
    """``'serial'`` for one serial-marked piece, ``'tool'`` for the item type."""
    return 'serial' if is_serial_photo_target(target) else 'tool'


def tool_photo_filename(tool_id) -> str:
    """File name of a tool *type's* picture (unchanged since the first PR)."""
    return f'{int(tool_id)}.webp'


def serial_photo_filename(serial_id) -> str:
    """File name of one serial piece's picture."""
    return f'{SERIAL_PHOTO_PREFIX}{int(serial_id)}.webp'


def photo_filename_for(target) -> str:
    """File name for whichever kind of target this is."""
    if photo_kind(target) == 'serial':
        return serial_photo_filename(target.id)
    return tool_photo_filename(target.id)


def photo_target_label(target) -> str:
    """How this target is named in a message the operator reads.

    A tool type is just its name; a serial piece is its marking plus the
    type it belongs to, because "Shovel No 3" alone is ambiguous in a flash
    message that lists several tools.
    """
    if photo_kind(target) == 'serial':
        marking = (getattr(target, 'serial_number', '') or '').strip() or 'Serial'
        tool = getattr(target, 'tool', None)
        name = (getattr(tool, 'name', '') or '').strip() if tool is not None else ''
        return f'{marking} — {name}' if name else marking
    return (getattr(target, 'name', '') or '').strip() or 'Tool'


def tool_photo_path(tool, app=None) -> str | None:
    """Absolute path of a target's photo file, or None when it has none.

    ``image_path`` stores a static-relative path; anything that does not
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
    """``os.stat_result`` for a target's photo file, or None when there is none.

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
    """True when the target points at a photo file that is actually on disk."""
    return _photo_stat(tool) is not None


def stored_photo_names(app=None) -> set:
    """File names of the photos that are really on disk (one directory read).

    A list page that shows many rows asks the same question over and over
    ("does this piece have a picture?").  One ``listdir`` answers it for the
    whole page instead of one ``stat`` per row.
    """
    try:
        return set(os.listdir(tool_photo_dir(app)))
    except OSError:
        return set()


def target_photo_on_disk(target, names=None) -> bool:
    """True when the target's photo file is really on disk.

    With ``names`` (the result of :func:`stored_photo_names`) the check costs
    a set lookup instead of a file-system call.
    """
    relative = (getattr(target, 'image_path', None) or '').strip()
    if not relative:
        return False
    if names is None:
        return tool_has_photo(target)
    return os.path.basename(relative.replace('\\', '/')) in names


def photo_progress(targets, app=None, names=None):
    """``(pieces with a picture, pieces in total)`` for a list of targets.

    A tool type owns five serial pieces; this is what a list row shows so the
    operator can see at a glance that only two of them have been photographed.
    Pass ``names`` (see :func:`stored_photo_names`) to reuse one directory
    read across many rows of the same page.
    """
    targets = list(targets or [])
    if names is None:
        names = stored_photo_names(app)
    done = sum(1 for target in targets if target_photo_on_disk(target, names))
    return done, len(targets)


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
    """URL of a target's photo, or None when it has none.

    Works for a tool type and for a serial piece alike: the stored
    ``image_path`` already says which file it is.

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
def save_photo(target, upload, app=None, commit=False):
    """Validate ``upload`` (a werkzeug FileStorage) and store it for ``target``.

    ``target`` is either a tool type or one serial piece; the file name and
    the flash text follow from that, everything else is the same.

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
            stored_name = photo_filename_for(target)
            photo_target = os.path.join(photo_dir, stored_name)
            image.save(photo_target, format='WEBP',
                       quality=TOOL_PHOTO_QUALITY, method=6)
    except UnidentifiedImageError:
        return False, 'That file is not a readable image — use a JPG or PNG photo.'
    except (OSError, ValueError) as exc:
        message = str(exc).strip()
        return False, (message or 'The photo could not be saved.')

    previous = tool_photo_path(target, app)
    target.image_path = f'{TOOL_PHOTO_DIRNAME}/{stored_name}'
    target.updated_at = _pkt_now_naive()
    if commit:
        db.session.commit()
    # Replacing a photo must not leave the previous file behind when it was
    # stored under another name (an upload from before this module existed).
    if previous and os.path.abspath(previous) != os.path.abspath(
            tool_photo_path(target, app) or previous):
        discard_tool_photo(previous)
    return True, f'Photo saved for {photo_target_label(target)}.'


def remove_photo(target, app=None, commit=False):
    """Delete a target's photo file and clear its ``image_path``.

    Returns ``(ok, message)``; removing a photo from a target that has none is
    reported as a plain notice rather than an error.
    """
    if not (getattr(target, 'image_path', None) or '').strip():
        return False, f'{photo_target_label(target)} has no photo to remove.'
    path = tool_photo_path(target, app)
    target.image_path = None
    target.updated_at = _pkt_now_naive()
    if commit:
        db.session.commit()
    discard_tool_photo(path)
    return True, f'Photo removed from {photo_target_label(target)}.'


# --------------------------------------------------------------------------- #
# tool-type wrappers (the item type is the older, original case)
# --------------------------------------------------------------------------- #
def save_tool_photo(tool, upload, app=None, commit=False):
    """Save a photo for a tool *type* (see :func:`save_photo`)."""
    return save_photo(tool, upload, app=app, commit=commit)


def remove_tool_photo(tool, app=None, commit=False):
    """Remove a tool *type's* photo (see :func:`remove_photo`)."""
    return remove_photo(tool, app=app, commit=commit)


def discard_tool_photo(path):
    """Delete a photo file whose target row was rolled back.

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
