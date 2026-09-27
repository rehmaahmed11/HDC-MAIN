"""Backup/restore safety gates: isolated live target + independent archives."""
import io
import os
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import patch
from qa_support import IsolatedAppTest
from hdc.extensions import db
from hdc.models.projects import Project
from hdc.services.backups import _create_backup_zip, _backup_filename, _prune_saved_backups
from hdc.core.admin import _restore_from_backup_zip, _restore_from_paths


class RecoveryTest(IsolatedAppTest):
    def setUp(self):
        super().setUp()
        with self.app.app_context():
            db.session.add(Project(project_code='SAFE',name='Must survive',client='Owner'))
            db.session.commit()

    def assert_target_survives(self):
        with self.app.app_context():
            self.assertEqual(Project.query.filter_by(project_code='SAFE').one().name,'Must survive')
        with sqlite3.connect(self.path) as con:
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_raw_corrupt_db_rejected_before_target_replacement(self):
        path=Path(self.tmp.name)/'corrupt.db';path.write_bytes(b'not a sqlite database')
        with self.app.app_context():
            with self.assertRaises((ValueError,sqlite3.DatabaseError)):
                _restore_from_paths(str(path))
        self.assert_target_survives()

    def test_empty_partial_and_orphan_db_rejected_before_replacement(self):
        for kind in ('empty','partial','orphan'):
            with self.subTest(kind=kind):
                path=Path(self.tmp.name)/(kind+'.db')
                with sqlite3.connect(path) as con:
                    if kind=='partial':con.execute('CREATE TABLE hdc_user (id INTEGER PRIMARY KEY)')
                    if kind=='orphan':
                        with sqlite3.connect(self.path) as src:src.backup(con)
                        con.execute('PRAGMA foreign_keys=OFF')
                        con.execute("INSERT INTO hdc_stage(project_id,name) VALUES(999999,'orphan')")
                with self.app.app_context():
                    with self.assertRaises(ValueError):_restore_from_paths(str(path))
                self.assert_target_survives()

    def test_archive_paths_missing_db_and_corruption_do_not_replace_target(self):
        for name in ('../escape','/absolute','nested/../../escape','..\\escape','no_database.txt'):
            with self.subTest(name=name):
                path=Path(self.tmp.name)/'bad.zip'
                with zipfile.ZipFile(path,'w') as z:z.writestr(name,b'bad')
                with self.app.app_context():
                    with self.assertRaises(ValueError):_restore_from_backup_zip(str(path))
                self.assert_target_survives()

    def test_backup_roundtrip_wal_contents_and_prune(self):
        archive=Path(self.tmp.name)/'roundtrip.zip'
        with self.app.app_context():_create_backup_zip(str(archive))
        with zipfile.ZipFile(archive) as z:
            self.assertIn('hdc_erp.db',z.namelist());self.assertIn('hdc_data_export.xlsx',z.namelist())
            import openpyxl
            workbook=openpyxl.load_workbook(io.BytesIO(z.read('hdc_data_export.xlsx')),read_only=True)
            self.assertIn('hdc_project',workbook.sheetnames);workbook.close()
        with self.app.app_context():
            Project.query.filter_by(project_code='SAFE').one().name='Changed after backup';db.session.commit()
            _restore_from_backup_zip(str(archive))
        self.assert_target_survives()
        self.client=self.app.test_client();self.login()
        for page in ('/hdc/projects','/hdc/workers','/hdc/accounts','/hdc/expenses','/hdc/purchase-v2','/hdc/reports'):
            self.assertEqual(self.client.get(page).status_code,200)
        saved=Path(self.tmp.name)/'backups'
        for i in range(4):
            p=saved/f'qa-{i}.zip';p.write_bytes(archive.read_bytes());os.utime(p,(i+1,i+1))
        with self.app.app_context():
            self.assertEqual(_prune_saved_backups(2)['deleted'],2)
        self.assertEqual(sorted(p.name for p in saved.glob('qa-*.zip')),['qa-2.zip','qa-3.zip'])

    def test_backup_names_do_not_collide_within_same_second(self):
        with patch('hdc.services.backups._pkt_now_naive') as now:
            from datetime import datetime
            now.return_value=datetime(2026,9,27,12,0,0)
            self.assertNotEqual(_backup_filename(),_backup_filename())

    def test_failed_migration_restores_previous_database_and_estimation(self):
        candidate=Path(self.tmp.name)/'candidate.db'
        with sqlite3.connect(self.path) as src:
            with sqlite3.connect(candidate) as dst:src.backup(dst)
        with sqlite3.connect(candidate) as con:
            con.execute("UPDATE hdc_project SET name='Imported candidate'")
        estimation=Path(self.tmp.name)/'project_estimations.json';estimation.write_text('{"previous":true}')
        new_estimation=Path(self.tmp.name)/'new-estimation.json';new_estimation.write_text('{"new":true}')
        with self.app.app_context(), patch('hdc.core.admin._ensure_bootstrap_once', side_effect=RuntimeError('Injected migration failure')):
            with self.assertRaisesRegex(RuntimeError,'Injected'):
                _restore_from_paths(str(candidate),str(new_estimation))
        self.assert_target_survives()
        self.assertEqual(estimation.read_text(),'{"previous":true}')

    def test_restore_supports_separate_instance_and_database_filesystems(self):
        import tempfile
        from hdc.app import create_app
        if not os.path.isdir('/dev/shm'):
            self.skipTest('cross-filesystem fixture requires Linux /dev/shm')
        with tempfile.TemporaryDirectory(prefix='hdc-restore-crossfs-',dir='/dev/shm') as folder:
            app=create_app({'TESTING':True,'HDC_INSTANCE_DIR':self.tmp.name,
                            'HDC_DB_PATH':os.path.join(folder,'target.db')})
            try:
                with app.app_context():
                    _restore_from_paths(self.path)
                    self.assertEqual(Project.query.filter_by(project_code='SAFE').one().name,'Must survive')
            finally:
                with app.app_context():db.session.remove();db.engine.dispose()
