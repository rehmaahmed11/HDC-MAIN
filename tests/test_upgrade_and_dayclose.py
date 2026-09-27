"""Repeatable schema, legacy migration, production and day-close boundaries."""
import os
import sqlite3
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from qa_support import IsolatedAppTest
from hdc.app import create_app
from hdc.extensions import db
from hdc.core.bootstrap import _ensure_bootstrap_once
from hdc.models.projects import Project
from hdc.models.workforce import Worker, Attendance, TimeEntry
from hdc.models.auth import HDCUser
from hdc.services.cashflow_register import day_positions, save_counted_position, lock_cash_day, is_day_locked
from hdc.utils.dates import _pkt_today


class UpgradeAndDayCloseTest(IsolatedAppTest):
    def test_missing_historical_columns_heal_without_changing_rows_or_indexes(self):
        with self.app.app_context():
            db.session.add(Project(project_code='HISTORY',name='Historic site',client='Owner'))
            db.session.commit();db.session.remove();db.engine.dispose()
        backup=Path(self.tmp.name)/'before-migration.db'
        with sqlite3.connect(self.path) as con:
            with sqlite3.connect(backup) as dst:con.backup(dst)
            # SQLite cannot DROP a column named by a table-level FK; rebuild
            # this empty fixture using its original DDL minus that later field.
            import re
            ddl=con.execute("SELECT sql FROM sqlite_master WHERE name='hdc_time_entry'").fetchone()[0]
            ddl='\n'.join(line for line in ddl.splitlines() if 'attendance_day_id' not in line)
            ddl=re.sub(r',\s*\)', '\n)', ddl)
            con.execute('DROP TABLE hdc_time_entry');con.execute(ddl)
            for table,column in (('hdc_purchase_v2','notes'),
                                 ('hdc_purchase_v2','challan_no'),('hdc_delivery','date')):
                con.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
        for _ in range(3):
            with self.app.app_context():
                _ensure_bootstrap_once(force=True)
                self.assertEqual(Project.query.filter_by(project_code='HISTORY').one().name,'Historic site')
                self.assertEqual(HDCUser.query.filter_by(username='admin').count(),1)
        with sqlite3.connect(self.path) as con:
            self.assertIn('attendance_day_id',[r[1] for r in con.execute('PRAGMA table_info(hdc_time_entry)')])
            names=[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")]
            self.assertEqual(len(names),len(set(names)))
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_legacy_attendance_migration_and_void_survive_repeat_boot(self):
        with self.app.app_context():
            p=Project(project_code='LEGACY',name='Legacy site',client='Owner')
            w=Worker(worker_code='LEGACY-W',name='Legacy worker',base_daily_wage=800)
            db.session.add_all([p,w]);db.session.flush()
            attendance=Attendance(worker_id=w.id,project_id=p.id,date=_pkt_today(),hours_worked=8,total_wage=800)
            db.session.add(attendance);db.session.commit();aid=attendance.id
            from sqlalchemy import text
            db.session.execute(text("DELETE FROM hdc_runtime_flag WHERE key IN ('time_entry_migration_done','time_entry_dedupe_done')"));db.session.commit()
            for _ in range(2):_ensure_bootstrap_once(force=True)
            entry=TimeEntry.query.filter_by(attendance_id=aid).one()
            self.assertEqual(entry.wage_calculated,800);entry.is_void=True;db.session.commit()
            for _ in range(2):_ensure_bootstrap_once(force=True)
            self.assertTrue(TimeEntry.query.filter_by(attendance_id=aid).one().is_void)

    def test_partial_initialization_empty_noncore_tables_is_idempotent(self):
        with self.app.app_context():db.session.remove();db.engine.dispose()
        with sqlite3.connect(self.path) as con:
            # A previously interrupted additive feature initialization.
            con.execute('DROP TABLE hdc_user_activity')
        with self.app.app_context():
            for _ in range(2):_ensure_bootstrap_once(force=True)
        with sqlite3.connect(self.path) as con:
            self.assertTrue(con.execute("SELECT name FROM sqlite_master WHERE name='hdc_user_activity'").fetchone())

    def test_day_close_threshold_below_equal_above_and_disabled(self):
        with self.app.app_context():
            for offset,(threshold,difference,confirmation,reason,allowed) in enumerate((
                (5000,4999,False,'',True),(5000,5000,False,'',True),
                (5000,5001,False,'',False),(5000,5001,True,'',False),
                (5000,5001,False,'Explanation',False),(5000,5001,True,'Explanation',True),
                (0,10000,False,'',True))):
                with self.subTest(threshold=threshold,difference=difference,confirmation=confirmation,reason=reason):
                    day=_pkt_today()+timedelta(days=offset)
                    self.app.config['HDC_DAY_CLOSE_DIFFERENCE_THRESHOLD']=threshold
                    positions=day_positions(day)
                    for i,pos in enumerate(positions):
                        save_counted_position(day,pos.account_id,pos.expected_closing+(difference if i==0 else 0))
                    if allowed:
                        lock_cash_day(day,actor='qa',confirm_difference=confirmation,note=reason)
                        self.assertTrue(is_day_locked(day))
                    else:
                        with self.assertRaises(ValueError):lock_cash_day(day,actor='qa',confirm_difference=confirmation,note=reason)
                        self.assertFalse(is_day_locked(day));db.session.rollback()

    def test_production_refuses_missing_secret_or_initial_admin_password(self):
        for missing in ('HDC_SECRET_KEY','HDC_BOOTSTRAP_ADMIN_PASSWORD'):
            with self.subTest(missing=missing):
                folder=Path(self.tmp.name)/missing;folder.mkdir()
                config={'HDC_ENV':'prod','HDC_SECRET_KEY':'temporary-qa-secret',
                        'HDC_BOOTSTRAP_ADMIN_PASSWORD':'Production-QA-Only-123',missing:''}
                with patch.dict(os.environ,config):
                    with self.assertRaisesRegex(RuntimeError,missing):
                        create_app({'HDC_INSTANCE_DIR':str(folder),'HDC_DB_PATH':str(folder/'qa.db')})
