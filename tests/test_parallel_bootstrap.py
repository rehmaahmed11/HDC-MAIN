"""Cold-start independent worker processes against the same empty SQLite DB."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class ParallelBootstrapTest(unittest.TestCase):
    def test_three_independent_workers_initialize_one_database(self):
        with tempfile.TemporaryDirectory(prefix='hdc-bootstrap-race-') as tmp:
            path=Path(tmp)/'qa.db'
            env=dict(os.environ,HDC_ENV='prod',HDC_SECRET_KEY='bootstrap-race-test-only',
                     HDC_BOOTSTRAP_ADMIN_PASSWORD='Bootstrap-Race-Only-123',
                     HDC_INSTANCE_DIR=tmp,HDC_DB_PATH=str(path))
            # Hashing the first admin's password happens in assign_password(), the
            # one place that sets a password (hash + administrator-viewable copy),
            # so that is where the race window is stretched.
            script='''
import time
from hdc.services import password_vault
original = password_vault.generate_password_hash
def slow_hash(*args, **kwargs):
    time.sleep(0.3)
    return original(*args, **kwargs)
password_vault.generate_password_hash = slow_hash
from hdc.app import create_app
app = create_app()
with app.test_client() as client:
    assert client.get('/hdc/login').status_code == 200
'''
            def boot(_):
                return subprocess.run([sys.executable,'-c',script],cwd=ROOT,env=env,
                                      capture_output=True,text=True,timeout=45)
            with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(boot,range(3)))
            # Do not include password hashes or SQL parameter dumps in failures.
            self.assertEqual([r.returncode for r in results],[0,0,0],
                             'Parallel bootstrap failed; reproduce in the isolated child process fixture')
            with sqlite3.connect(path) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM hdc_user WHERE username='admin'").fetchone()[0],1)
                self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(),[])
