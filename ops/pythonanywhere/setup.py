#!/usr/bin/env python3
"""Interactive PythonAnywhere setup. Standard library only; run with Python 3.10+."""
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
PLACEHOLDER = ('replace-with-', 'yourname')


def ask(label, default=''):
    value = input(f'{label}' + (f' [{default}]' if default else '') + ': ').strip()
    return value or default


def confirm(label):
    return ask(label + ' (yes/no)', 'no').lower() == 'yes'


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


def main():
    print('\nHDC PythonAnywhere setup\nNo databases are deleted or moved. Secrets stay outside Git.\n')
    home = Path.home()
    username = getpass.getuser()
    project = absolute(ask('Git checkout folder', str(home / 'HDC-MAIN')))
    if not (project / '.git').is_dir():
        if project.exists() and any(project.iterdir()):
            raise ValueError('That folder is not a Git checkout and is not empty. Keep your ZIP/data folder; choose a different empty folder and rerun.')
        if not confirm(f'Clone https://github.com/{REPO}.git into {project}?'):
            return
        run('git', 'clone', f'https://github.com/{REPO}.git', project)
    for relative in ('deploy_receiver.py', 'ops/pythonanywhere/deploy.sh',
                     'ops/pythonanywhere/wsgi_deploy_with_receiver.example.py'):
        if not (project / relative).is_file():
            raise ValueError(f'Missing {relative}. Update the checkout to the merged deployment code first.')
    dirty = subprocess.check_output(['git', '-C', str(project), 'diff', '--name-only'], text=True)
    staged = subprocess.check_output(['git', '-C', str(project), 'diff', '--cached', '--name-only'], text=True)
    if dirty or staged:
        raise ValueError('Tracked local changes exist. Save/reconcile them before deploying; this setup will not discard them.')

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

    domain = ask('Public web domain (no https or path)', f'{username}.pythonanywhere.com')
    if not re.fullmatch(r'[A-Za-z0-9.-]+', domain) or '.' not in domain:
        raise ValueError('Enter only a domain, such as rehmanahmed92yd.pythonanywhere.com.')
    wsgi_default = values.get('HDC_WSGI_FILE', f'/var/www/{domain.replace(".", "_")}_wsgi.py')
    wsgi = absolute(ask('WSGI file path shown in the PythonAnywhere Web tab', wsgi_default))
    if not wsgi.is_file():
        raise ValueError('WSGI file does not exist. Create/select a Manual configuration web app in the Web tab first, then rerun with its actual WSGI path.')

    # The ERP resolves relative paths against its checkout, not the console cwd.
    for key in ('HDC_INSTANCE_DIR', 'HDC_DB_PATH', 'HDC_BACKUP_DIR'):
        if values.get(key):
            path = Path(values[key]).expanduser()
            values[key] = str(path if path.is_absolute() else project / path)
    instance_default = values.get('HDC_INSTANCE_DIR', str(project / 'hdc_instance'))
    instance = absolute(ask('Existing instance/data folder (or folder for a fresh installation)', instance_default))
    candidates = [instance / 'hdc_erp_integrated.db', instance / 'hdc_erp.db']
    db_default = values.get('HDC_DB_PATH', str(next((p for p in candidates if p.is_file()), candidates[-1])))
    db = absolute(ask('Live SQLite database path (keep your existing DB path to retain data)', db_default))
    fresh = not db.exists()
    if fresh:
        print(f'WARNING: No database exists at {db}. An incorrect path would create an empty ERP.')
        if ask('Type CREATE NEW DATABASE only if you intentionally want an empty ERP') != 'CREATE NEW DATABASE':
            raise ValueError('Stopped without changing configuration. Rerun with your existing database path.')
    elif not db.is_file() or db.stat().st_size == 0:
        raise ValueError('Database is not a nonempty file. Check the path before continuing.')
    if not fresh and not instance.is_dir():
        raise ValueError('Existing instance folder not found. Check the data path.')

    venv = absolute(ask('Virtualenv path (use the existing Web-tab path if available)', values.get('HDC_VENV_PATH', str(home / '.virtualenvs/hdc'))))
    if not (venv / 'bin/python').is_file():
        interpreter = ask('Python executable matching your Web-tab Python version', f'python{sys.version_info.major}.{sys.version_info.minor}')
        if not shutil.which(interpreter):
            raise ValueError(f'{interpreter} not found. Choose an installed Python matching your web app.')
        run(interpreter, '-m', 'venv', venv)

    values.update({
        'HDC_ENV': 'prod', 'HDC_APP_DIR': str(project),
        'HDC_INSTANCE_DIR': str(instance), 'HDC_DB_PATH': str(db),
        'HDC_VENV_PATH': str(venv), 'HDC_WSGI_FILE': str(wsgi),
        'HDC_DEPLOY_REPO': REPO, 'HDC_DEPLOY_BRANCH': 'main',
        'HDC_DEPLOY_ALLOW_MANUAL': '0', 'HDC_DEPLOY_DISABLED': '0',
        'HDC_ALLOW_NEW_DB': '1' if fresh else '0',
        'HDC_BACKUP_DIR': values.get('HDC_BACKUP_DIR', str(instance / 'backups/deployments')),
        'HDC_DEPLOY_ENV_FILE': str(env_path),
    })
    for key in ('HDC_SECRET_KEY', 'HDC_DEPLOY_WEBHOOK_SECRET'):
        values.setdefault(key, secrets.token_hex(32))
    if fresh:
        values['HDC_BOOTSTRAP_ADMIN_USERNAME'] = ask('New admin username', 'admin')
        password = getpass.getpass('New admin password (12+ chars; upper/lowercase, number, symbol): ')
        if (len(password) < 12 or not re.search('[A-Z]', password) or not re.search('[a-z]', password)
                or not re.search('[0-9]', password) or not re.search(r'[^A-Za-z0-9]', password)):
            raise ValueError('Password must have 12+ characters and upper/lowercase, number, symbol.')
        values['HDC_BOOTSTRAP_ADMIN_PASSWORD'] = password
    payload = serialize(values)
    print(f'\nCode: {project}\nDatabase: {db}\nInstance: {instance}\nConfig: {env_path}')
    print('Existing databases may receive application schema migrations during deployment.')
    if not confirm('Have you backed up existing data, and should setup save config and install dependencies?'):
        return
    backup(env_path)
    write_private(env_path, payload)
    print('Private configuration saved (permissions 600). Existing valid secrets preserved.')
    run(venv / 'bin/python', '-m', 'pip', 'install', '-r', project / 'requirements.txt')

    print(f'\nIn the Web tab set Source code to: {project}\nVirtualenv to: {venv}')
    print('The Web-tab Python version MUST match that virtualenv. This script cannot change Web-tab settings.')
    print('Deployment follows origin/main. Unmerged feature-branch changes will not be deployed.')
    if not confirm('Are those Web-tab settings correct, and should setup deploy main and replace WSGI?'):
        print('Configuration saved. Rerun when ready; your secrets will be reused.')
        return
    # Existing WSGI stays in place until the first deployment passes all checks.
    env = os.environ.copy()
    env['HDC_DEPLOY_ENV_FILE'] = str(env_path)
    subprocess.run(['bash', str(project / 'ops/pythonanywhere/deploy.sh')], env=env, check=True)
    if fresh:
        values['HDC_ALLOW_NEW_DB'] = '0'
        write_private(env_path, serialize(values))
    template = (project / 'ops/pythonanywhere/wsgi_deploy_with_receiver.example.py').read_text()
    template = template.replace('/home/yourname', str(home))
    template = template.replace(f'PROJECT_DIR_DEFAULT = "{home}/HDC-MAIN"', f'PROJECT_DIR_DEFAULT = {str(project)!r}')
    compile(template, str(wsgi), 'exec')
    backup(wsgi)
    # PythonAnywhere manages the WSGI inode; write it in place, then touch to reload.
    wsgi.write_text(template)
    wsgi.touch()

    print('\nSETUP COMPLETE — reload requested. Verify both /hdc/ and the health URL below.')
    print(f'Health URL: https://{domain}/deploy/health')
    print('Health must show deploy_enabled: true before activating the GitHub hook.')
    print(f'\nGitHub: https://github.com/{REPO}/settings/hooks')
    print('Add ONE webhook (or edit the existing one):')
    print(f'Payload URL: https://{domain}/deploy/github')
    print('Content type: application/json')
    print('SSL verification: Enable SSL verification')
    print('Events: Just the push event')
    print('Active: checked after the health check succeeds')
    print('Branch filtering: main, handled by the receiver; no GitHub branch field needed.')
    print(f'\nSecret is saved in {env_path}; never send it in chat or commit it.')
    if confirm('Show the webhook secret in this PRIVATE terminal to copy into GitHub?'):
        print('\nSecret: ' + values['HDC_DEPLOY_WEBHOOK_SECRET'] + '\n')
    print(f'Deploy log: {values.get("HDC_DEPLOY_STATE_DIR", str(instance / "deploy"))}/deploy.log')
    print('GitHub ping only tests connectivity. A push 202 means accepted; check the deploy log for completion.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'\nSETUP STOPPED: {exc}\nNo database was deleted. Fix the error and rerun; saved secrets are reused.', file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print('\nSetup cancelled. Any configuration already saved remains in place.', file=sys.stderr)
        sys.exit(1)
