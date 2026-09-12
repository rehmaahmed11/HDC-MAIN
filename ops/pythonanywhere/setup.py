#!/usr/bin/env python3
"""Unattended PythonAnywhere setup for HDC ERP. Standard library only; Python 3.10+.

Runs with ZERO questions.  Every value is resolved in this order:

  1. values already saved in ``~/.config/hdc/production.env`` (secrets reused),
  2. ``HDC_*`` environment variables,
  3. defaults derived from this checkout and your account name.

In one run it:

  * finds the checkout (clones it when the folder is missing or empty),
  * verifies the deployment files exist and the working tree is clean,
  * generates any missing secrets (site key, webhook secret, deploy token),
  * saves ``~/.config/hdc/production.env`` (mode 600, timestamped backup),
  * writes ``<checkout>/github_hook_credentials.txt`` (mode 600, gitignored)
    with every field needed to add the GitHub hook,
  * creates the virtualenv if needed and installs the requirements,
  * writes the WSGI dispatcher (backed up first) and runs the guarded
    ``deploy.sh`` whenever the PythonAnywhere WSGI file can be located or
    created.

Rerunning is always safe: saved secrets are reused, existing files are
backed up, and nothing is ever asked interactively.  PythonAnywhere allows
exactly two browser-only steps that no script can click: creating the web
app once in the Web tab, and watching the reload.  When the WSGI file cannot
be located or created, the setup still finishes everything else and prints
that single remaining step instead of asking.

Optional environment overrides:

  HDC_APP_DIR            checkout folder (default: the one containing this script)
  HDC_DOMAIN             public web domain (default: <username>.pythonanywhere.com)
  HDC_PYTHON             interpreter used to build the virtualenv (default: python3)
  HDC_SETUP_NO_DEPLOY=1  save config + credentials + dependencies and stop
                         before the deploy.sh / WSGI-replace step
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

REPO = 'rehmaahmed11/HDC-MAIN'
CREDENTIALS_FILE = 'github_hook_credentials.txt'
PLACEHOLDER = ('replace-with-', 'yourname')


def run(*args):
    subprocess.run([str(a) for a in args], check=True)


def read_env(path):
    """Read the simple KEY=VALUE format shared by WSGI and deploy.sh."""
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def usable(value):
    return bool(value) and not any(p in value for p in PLACEHOLDER)


def serialize(values):
    lines = ['# Private HDC production settings. Never commit this file.']
    for key, value in sorted(values.items()):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError(f'Invalid environment key: {key}')
        # Both readers understand single-quoted literal values, but not shell escapes.
        if any(c in value for c in "'\n\r\x00"):
            raise ValueError(f'{key} contains unsupported quotes or line breaks; no file written.')
        lines.append(f"{key}='{value}'")
    return '\n'.join(lines) + '\n'


def backup(path):
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        target = path.with_name(path.name + '.backup-' + stamp)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(path.read_bytes())
        print(f'Backup saved: {target}')


def write_private(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.hdc-setup-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(content)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def absolute(value):
    return Path(value).expanduser().resolve()


def strong_password():
    """16 random characters with at least one upper, lower, digit and symbol.

    Avoids quote/backtick characters so the value survives the single-quoted
    ``production.env`` serialization and every shell context untouched.
    """
    upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    lower = 'abcdefghijkmnopqrstuvwxyz'
    digits = '23456789'
    symbols = '@!%*?=-_+'
    alphabet = upper + lower + digits + symbols
    rng = secrets.SystemRandom()
    chars = [rng.choice(upper), rng.choice(lower), rng.choice(digits), rng.choice(symbols)]
    chars += [rng.choice(alphabet) for _ in range(16 - len(chars))]
    rng.shuffle(chars)
    return ''.join(chars)


def build_credentials(domain, webhook_secret, deploy_token, admin=None, log_dir=''):
    """Return the plain-text content of ``github_hook_credentials.txt``.

    ``admin`` is an optional ``(username, password)`` pair, included only for
    a brand-new database so the first login never has to be hunted for.
    """
    base = f'https://{domain}'
    lines = [
        '=' * 70,
        'HDC GITHUB HOOK CREDENTIALS  -  PRIVATE: never commit, never share',
        '=' * 70,
        f'Generated:   {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}',
        f'Repository:  {REPO}',
        'Deploys:     pushes to branch main only (other refs are ignored)',
        '',
        '1) GITHUB HOOK  (Settings -> Webhooks -> Add webhook)',
        f'   Settings page:  https://github.com/{REPO}/settings/hooks',
        f'   Payload URL:    {base}/deploy/github',
        '   Content type:   application/json',
        '   SSL verification: Enable SSL verification',
        '   Events:         Just the push event',
        '   Secret:         ' + webhook_secret,
        '   Branch field:   not needed (the receiver filters to main itself)',
        '',
        '2) MANUAL TRIGGER / ROLLBACK  (optional, off by default)',
        f'   Endpoint:       POST {base}/deploy/trigger',
        '   Bearer token:   ' + deploy_token,
        '   Answered only when production.env has HDC_DEPLOY_ALLOW_MANUAL=1',
        '',
        '3) CHECK IT',
        f'   Health (public):  GET {base}/deploy/health',
        f'   Log tail (auth):  GET {base}/deploy/status   (Authorization: Bearer {deploy_token})',
        '   Deploy log file:  ' + (f'{log_dir}/deploy.log' if log_dir else '<instance dir>/deploy/deploy.log'),
        '',
    ]
    if admin:
        lines += [
            '4) FRESH-INSTALL LOGIN  (brand-new database only)',
            f'   Login page:   {base}/hdc/login',
            '   Username:     ' + admin[0],
            '   Password:     ' + admin[1],
            '',
        ]
    lines += [
        'RULES: This file holds live secrets. It is listed in .gitignore and',
        'kept at mode 600. The same values live in ~/.config/hdc/production.env.',
        '=' * 70,
    ]
    return '\n'.join(lines) + '\n'


def main(project=None):
    print('\nHDC PythonAnywhere setup (unattended) - no questions will be asked.\n')
    home = Path.home()
    username = getpass.getuser()

    # 1. Locate (or clone) the checkout.
    if project is not None:
        project = absolute(project)
    else:
        default = Path(__file__).resolve().parents[2]
        project = absolute(os.environ.get('HDC_APP_DIR', '').strip() or str(default))
    if not (project / '.git').is_dir():
        if project.exists() and any(project.iterdir()):
            raise ValueError('That folder is not a Git checkout and is not empty. '
                             'Keep your ZIP/data folder; point HDC_APP_DIR at an empty '
                             'folder and rerun.')
        print(f'Cloning https://github.com/{REPO}.git into {project} ...')
        run('git', 'clone', f'https://github.com/{REPO}.git', project)
    for relative in ('deploy_receiver.py', 'ops/pythonanywhere/deploy.sh',
                     'ops/pythonanywhere/wsgi_deploy_with_receiver.example.py'):
        if not (project / relative).is_file():
            raise ValueError(f'Missing {relative}. Update the checkout to the merged deployment code first.')
    dirty = subprocess.check_output(['git', '-C', str(project), 'diff', '--name-only'], text=True)
    staged = subprocess.check_output(['git', '-C', str(project), 'diff', '--cached', '--name-only'], text=True)
    if dirty or staged:
        raise ValueError('Tracked local changes exist. Save/reconcile them before deploying; '
                         'this setup will not discard them.')
    print(f'Code: {project}')

    # 2. Load existing configuration and keep every secret that is still valid.
    env_path = home / '.config/hdc/production.env'
    if env_path.is_relative_to(project):
        raise ValueError('Production settings must be outside the Git checkout.')
    values = read_env(env_path)
    local = read_env(project / '.env')
    # Preserve app-specific settings from the existing .env, without importing the app/DB.
    for key, value in local.items():
        if key.startswith('HDC_'):
            values.setdefault(key, value)
    for key in list(values):
        if not usable(values[key]):
            del values[key]

    # The ERP resolves relative paths against its checkout, not the console cwd.
    for key in ('HDC_INSTANCE_DIR', 'HDC_DB_PATH', 'HDC_BACKUP_DIR'):
        if values.get(key):
            path = Path(values[key]).expanduser()
            values[key] = str(path if path.is_absolute() else project / path)

    # 3. Web domain and WSGI file.
    domain = (os.environ.get('HDC_DOMAIN', '').strip()
              or values.get('HDC_DOMAIN', '').strip()
              or f'{username}.pythonanywhere.com')
    if not re.fullmatch(r'[A-Za-z0-9.-]+', domain) or '.' not in domain:
        raise ValueError(f'HDC_DOMAIN is not a valid domain: {domain!r}')
    print(f'Web domain: {domain}')
    wsgi = absolute(values.get('HDC_WSGI_FILE', '').strip()
                    or f'/var/www/{domain.replace(".", "_")}_wsgi.py')
    wsgi_ready = wsgi.is_file()
    if not wsgi_ready:
        # PythonAnywhere creates /var/www/<domain>_wsgi.py for the web app.  If
        # the app has not been created yet we write the dispatcher there
        # anyway, so the only remaining human step is the one Web-tab click.
        try:
            template = (project / 'ops/pythonanywhere/wsgi_deploy_with_receiver.example.py').read_text()
            template = template.replace('/home/yourname', str(home))
            template = template.replace(
                f'PROJECT_DIR_DEFAULT = "{home}/HDC-MAIN"', f'PROJECT_DIR_DEFAULT = {str(project)!r}')
            compile(template, str(wsgi), 'exec')
            write_private(wsgi, template)
            wsgi_ready = True
            print(f'WSGI file not found; dispatcher written to {wsgi} '
                  f'(still create the web app in the Web tab).')
        except OSError:
            print(f'WSGI file not found at {wsgi} and it cannot be created here; '
                  f'the deploy step will be skipped.')

    # 4. Instance folder and database.
    instance = absolute(values.get('HDC_INSTANCE_DIR', '').strip() or str(project / 'hdc_instance'))
    candidates = [instance / 'hdc_erp_integrated.db', instance / 'hdc_erp.db']
    db = absolute(values.get('HDC_DB_PATH', '').strip()
                  or str(next((p for p in candidates if p.is_file()), candidates[-1])))
    fresh = not db.exists()
    if fresh:
        print(f'WARNING: No database exists at {db}; an empty ERP database '
              f'will be created on first start.')
    elif not db.is_file() or db.stat().st_size == 0:
        raise ValueError('Database is not a nonempty file. Check HDC_DB_PATH before continuing.')
    if not fresh and not instance.is_dir():
        raise ValueError('Existing instance folder not found. Check the data path.')
    print(f'Database: {db}' + ('  (fresh install)' if fresh else '  (existing data kept in place)'))

    # 5. Virtualenv and dependencies.
    venv = absolute(values.get('HDC_VENV_PATH', '').strip() or str(home / '.virtualenvs/hdc'))
    if not (venv / 'bin/python').is_file():
        interpreter = (os.environ.get('HDC_PYTHON', '').strip()
                       or f'python{sys.version_info.major}.{sys.version_info.minor}'
                       or 'python3')
        if not shutil.which(interpreter):
            interpreter = 'python3'
        if not shutil.which(interpreter):
            raise ValueError(f'{interpreter} not found; set HDC_PYTHON to the Python '
                             f'matching your web app.')
        print(f'Creating virtualenv at {venv} with {interpreter}')
        run(interpreter, '-m', 'venv', venv)
    print(f'Virtualenv: {venv}')

    # 6. Resolve every setting; generate secrets only where missing.
    values.update({
        'HDC_ENV': 'prod', 'HDC_APP_DIR': str(project),
        'HDC_INSTANCE_DIR': str(instance), 'HDC_DB_PATH': str(db),
        'HDC_VENV_PATH': str(venv), 'HDC_WSGI_FILE': str(wsgi), 'HDC_DOMAIN': domain,
        'HDC_DEPLOY_REPO': REPO, 'HDC_DEPLOY_BRANCH': 'main',
        'HDC_DEPLOY_ALLOW_MANUAL': '0', 'HDC_DEPLOY_DISABLED': '0',
        'HDC_ALLOW_NEW_DB': '1' if fresh else '0',
        'HDC_BACKUP_DIR': values.get('HDC_BACKUP_DIR', str(instance / 'backups/deployments')),
        'HDC_DEPLOY_ENV_FILE': str(env_path),
    })
    values.setdefault('HDC_SECRET_KEY', secrets.token_hex(32))
    values.setdefault('HDC_DEPLOY_WEBHOOK_SECRET', secrets.token_hex(32))
    values.setdefault('HDC_DEPLOY_TOKEN', secrets.token_urlsafe(32))
    if fresh:
        admin_user = values.get('HDC_BOOTSTRAP_ADMIN_USERNAME', '').strip() or 'admin'
        values['HDC_BOOTSTRAP_ADMIN_USERNAME'] = admin_user
        values.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', strong_password())

    # 7. Persist config + hook credentials (both backed up, both mode 600).
    log_dir = values.get('HDC_DEPLOY_STATE_DIR', '').strip() or str(instance / 'deploy')
    admin = ((values['HDC_BOOTSTRAP_ADMIN_USERNAME'], values['HDC_BOOTSTRAP_ADMIN_PASSWORD'])
             if fresh else None)
    credentials = build_credentials(domain, values['HDC_DEPLOY_WEBHOOK_SECRET'],
                                    values['HDC_DEPLOY_TOKEN'], admin=admin, log_dir=log_dir)
    credentials_path = project / CREDENTIALS_FILE
    backup(env_path)
    write_private(env_path, serialize(values))
    backup(credentials_path)
    write_private(credentials_path, credentials)
    print(f'Config saved: {env_path} (mode 600; previous file backed up)')
    print(f'Hook credentials saved: {credentials_path} (mode 600)')
    print('Existing databases may receive application schema migrations during deployment.')

    # 8. Install dependencies, then deploy main and install the WSGI dispatcher.
    no_deploy = os.environ.get('HDC_SETUP_NO_DEPLOY', '').strip().lower() in ('1', 'true', 'yes', 'on')
    run(venv / 'bin/pip', 'install', '-r', project / 'requirements.txt')
    deployed = False
    if no_deploy:
        print('\nHDC_SETUP_NO_DEPLOY is set: deploy.sh was skipped; '
              'rerun without it to deploy.')
    elif wsgi_ready:
        env = os.environ.copy()
        env['HDC_DEPLOY_ENV_FILE'] = str(env_path)
        subprocess.run(['bash', str(project / 'ops/pythonanywhere/deploy.sh')], env=env, check=True)
        deployed = True
        if fresh:
            values['HDC_ALLOW_NEW_DB'] = '0'
            write_private(env_path, serialize(values))
    else:
        print('\nDeploy skipped: no PythonAnywhere WSGI file was found or creatable.')
        print('Create/select a web app in the Web tab (Manual configuration), then rerun:')
        print('    python3 ops/pythonanywhere/setup.py')
        print('It will ask nothing and finish the deploy with the saved secrets.')

    if wsgi_ready:
        # Existing WSGI stays in place until the first deployment passes all checks.
        template = (project / 'ops/pythonanywhere/wsgi_deploy_with_receiver.example.py').read_text()
        template = template.replace('/home/yourname', str(home))
        template = template.replace(
            f'PROJECT_DIR_DEFAULT = "{home}/HDC-MAIN"', f'PROJECT_DIR_DEFAULT = {str(project)!r}')
        compile(template, str(wsgi), 'exec')
        backup(wsgi)
        # PythonAnywhere manages the WSGI inode; write it in place, then touch to reload.
        wsgi.write_text(template)
        wsgi.touch()

    print('\n' + '=' * 70)
    print('SETUP ' + ('COMPLETE - reload requested. Verify /hdc/ and the health URL below.'
                      if deployed else
                      'PREPARATION COMPLETE - one remaining step printed above.'))
    print('=' * 70)
    print(f'Code:          {project}')
    print(f'Database:      {db}')
    print(f'Instance:      {instance}')
    print(f'Config:        {env_path}')
    print(f'Credentials:   {credentials_path}')
    print(f'Web-tab Source code: {project}   |   Virtualenv: {venv}')
    print('The Web-tab Python version MUST match that virtualenv; this script cannot change Web-tab settings.')
    print('Deployment follows origin/main. Unmerged feature-branch changes will not be deployed.')
    print('')
    print('GITHUB HOOK CREDENTIALS (also in the credentials file):')
    print(f'   Settings page:   https://github.com/{REPO}/settings/hooks')
    print(f'   Payload URL:     https://{domain}/deploy/github')
    print('   Content type:    application/json')
    print('   SSL verification: on')
    print('   Events:          Just the push event')
    print('   Secret:          ' + values['HDC_DEPLOY_WEBHOOK_SECRET'])
    print('   Manual trigger:  ' + values['HDC_DEPLOY_TOKEN']
          + '  (bearer token; only when HDC_DEPLOY_ALLOW_MANUAL=1)')
    print(f'   Health URL:      https://{domain}/deploy/health')
    if admin:
        print('   Fresh-install login: ' + admin[0] + ' / ' + admin[1])
    print('')
    print('Health must show deploy_enabled: true before activating the GitHub hook.')
    print(f'Deploy log: {log_dir}/deploy.log')
    print('A GitHub ping only tests connectivity. A push 202 means accepted; '
          'check the deploy log for completion.')
    print('Never send these secrets in chat or commit them.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'\nSETUP STOPPED: {exc}\nNo database was deleted or moved. Fix the problem '
              f'and rerun; saved secrets are reused.', file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print('\nSetup cancelled. Any configuration already saved remains in place.', file=sys.stderr)
        raise SystemExit(1)
