"""HDC services.password_vault - administrator-viewable copy of account passwords.

Login always authenticates against ``HDCUser.password_hash`` (a one-way hash)
and never reads anything from this module.  On top of that hash, an
administrator can *view* an account's current password: whenever a password is
set through the app (new user, reset, first admin, command-line reset) a second
copy is stored **encrypted** in ``HDCUser.password_vault`` and is decrypted only
when an administrator asks for it (``POST /hdc/users/<id>/password``).

Why encrypted rather than a plain text column
---------------------------------------------
Database files travel: backups, copies for support, and - once already in this
project's history (see PRODUCTION_HARDENING.md) - a commit to Git.  The key
lives outside the database (``HDC_PASSWORD_VAULT_KEY``, or when that is unset a
key derived from ``HDC_SECRET_KEY``), so a leaked database file or backup does
not by itself expose a single password.

Rules this module keeps
-----------------------
* **Never break a login or user-management flow.**  Sealing failures leave the
  copy empty; the account is still saved with its hash.
* **Never show a wrong password.**  A copy is only returned when it decrypts
  *and* still matches the account's current hash, so a password changed by a
  path that does not know about the vault (raw SQL, an older script) reads as
  "not saved" instead of showing a stale value.
* **Never silently downgrade to plain text.**  Without the crypto library or a
  key, nothing is stored.
* **Old passwords cannot be recovered.**  A one-way hash cannot be reversed, so
  accounts whose password predates this feature have no copy; they become
  viewable the next time an administrator sets their password.

Rotating the key (or changing ``HDC_SECRET_KEY`` when no dedicated key is set)
makes every stored copy unreadable.  Nothing else is affected: logins keep
working and the copies come back as soon as passwords are set again.
"""

import base64

from flask import current_app
from werkzeug.security import check_password_hash, generate_password_hash

# Domain-separation label for the key derivation; bump if the scheme changes.
_KEY_INFO = b'hdc-password-vault-v1'

MSG_NO_LIBRARY = (
    'The "cryptography" package is not installed on this server. Run '
    '"pip install -r requirements.txt" in the site\'s virtualenv, then reload the site.')
MSG_NO_KEY = ('No encryption key is configured. Set HDC_SECRET_KEY '
              '(or a dedicated HDC_PASSWORD_VAULT_KEY).')


def _key_material():
    """The secret the vault key is derived from ('' when there is none)."""
    try:
        config = current_app.config
    except RuntimeError:  # no application context
        return ''
    dedicated = str(config.get('HDC_PASSWORD_VAULT_KEY') or '').strip()
    return dedicated or str(config.get('SECRET_KEY') or '')


def _cipher():
    """Return ``(Fernet, None)`` when usable, else ``(None, reason)``."""
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except Exception:  # missing or broken install must never break a login flow
        return None, MSG_NO_LIBRARY
    secret = _key_material()
    if not secret:
        return None, MSG_NO_KEY
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=_KEY_INFO).derive(secret.encode('utf-8'))
    return Fernet(base64.urlsafe_b64encode(key)), None


def vault_problem():
    """Why password viewing cannot work on this server, or '' when it can."""
    return _cipher()[1] or ''


def seal_password(raw_password):
    """Encrypt ``raw_password`` into a storable token.

    Returns None when there is nothing to store or the vault is unavailable;
    never raises.
    """
    if not raw_password:
        return None
    cipher, _problem = _cipher()
    if cipher is None:
        return None
    try:
        return cipher.encrypt(str(raw_password).encode('utf-8')).decode('ascii')
    except Exception:
        return None


def open_password(token):
    """Decrypt a stored token; None if it is empty, damaged or from another key."""
    if not token:
        return None
    cipher, _problem = _cipher()
    if cipher is None:
        return None
    try:
        return cipher.decrypt(str(token).encode('ascii')).decode('utf-8')
    except Exception:  # InvalidToken, wrong key, truncated or non-ASCII data
        return None


def assign_password(user, raw_password):
    """Set ``user``'s password: the login hash plus the administrator copy.

    This is the only supported way to change a password.  It does not commit,
    and it leaves ``auth_version`` alone so each caller keeps its own rule for
    signing the user out.  When the vault is unavailable the previous copy is
    cleared rather than left behind, so it can never describe an old password.
    """
    user.password_hash = generate_password_hash(raw_password)
    user.password_vault = seal_password(raw_password)


def viewable_password(user):
    """The account's current password, or None when it cannot be shown truthfully."""
    plain = open_password(getattr(user, 'password_vault', None))
    if plain is None:
        return None
    try:
        matches = check_password_hash(getattr(user, 'password_hash', '') or '', plain)
    except Exception:  # a corrupt hash row makes Werkzeug raise; treat it as "no match"
        return None
    return plain if matches else None
