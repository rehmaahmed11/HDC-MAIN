"""Seed a demo database for visual QA of the stage expand/collapse feature.

Usage: HDC_DB_PATH=/tmp/qa_demo.db python qa/seed_demo.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hdc_erp import app as hdc_app  # noqa: E402
from hdc.extensions import db  # noqa: E402
from hdc.models.accounts import Account  # noqa: E402
from hdc.models.auth import HDCUser  # noqa: E402
from hdc.models.projects import Project, Stage, StageDrawing  # noqa: E402
from hdc.models.subcontract import Subcontractor  # noqa: E402
from hdc.core.bootstrap import _bootstrap_hdc  # noqa: E402
from hdc.config import STAGE_DRAWINGS_DIR  # noqa: E402


def main():
    with hdc_app.app_context():
        _bootstrap_hdc()
        db.session.rollback()

        if Project.query.count():
            print('DB already seeded; skipping.')
            return

        admin = HDCUser.query.filter_by(username='admin').first()
        print('admin user:', admin.username if admin else 'MISSING')

        # Receiving account for owner payments.
        acc = Account.query.filter_by(name='Company Cash').first()
        if not acc:
            acc = Account(name='Company Cash', type='cash', status='active')
            db.session.add(acc)

        p = Project(name='Ali Residency', project_code='PRJ-DEMO', client='Mr. Ali',
                    location='Lahore', total_constructed_sqft=5000, owner_rate_per_sqft=2400,
                    status='Active')
        db.session.add(p)
        db.session.flush()

        s1 = Stage(project_id=p.id, name='Structure', contract_basis='Per Sq Ft',
                   rate_per_sqft=2500, discount_per_sqft=100, qty_sqft=2000,
                   status='Active', progress=60, execution_mode='subcontractor',
                   assigned_subcontractor_id=None)
        s2 = Stage(project_id=p.id, name='Brick Work', contract_basis='Per Sq Ft',
                   rate_per_sqft=900, qty_sqft=1800, status='Active', progress=30)
        s3 = Stage(project_id=p.id, name='Paint & Finish', contract_basis='Lump Sum',
                   lump_sum_value=850000, status='completed', progress=100,
                   execution_mode='subcontractor')
        for s in (s1, s2, s3):
            db.session.add(s)
        db.session.flush()

        sub1 = Subcontractor(name='Rashid Thala Crew', subcontractor_code='SUB-001',
                             work_type='RCC', stage_id=s1.id, contract_type='sqft',
                             rate_per_sqft=380, total_sqft=1200, retention_percentage=5,
                             work_done_percentage=55)
        sub2 = Subcontractor(name='Bilal Steel Fixers', subcontractor_code='SUB-002',
                             work_type='Steel fixing', stage_id=s1.id, contract_type='sqft',
                             rate_per_sqft=120, total_sqft=600, retention_percentage=5,
                             work_done_percentage=40)
        sub3 = Subcontractor(name='Kamran Paint Works', subcontractor_code='SUB-003',
                             work_type='Paint', stage_id=s3.id, contract_type='lump_sum',
                             lump_sum_amount=420000, retention_percentage=10,
                             work_done_percentage=100)
        db.session.add_all([sub1, sub2, sub3])
        db.session.flush()

        # A fake drawing row pointing at a tiny valid PDF so the link works.
        os.makedirs(STAGE_DRAWINGS_DIR, exist_ok=True)
        pdf_path = os.path.join(STAGE_DRAWINGS_DIR, 'demo_structure.pdf')
        if not os.path.exists(pdf_path):
            minimal_pdf = (b'%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n'
                           b'2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n'
                           b'3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n'
                           b'trailer<</Root 1 0 R>>\n%%EOF')
            with open(pdf_path, 'wb') as f:
                f.write(minimal_pdf)
        d1 = StageDrawing(stage_id=s1.id, stored_name='demo_structure.pdf',
                          original_name='structure-plan.pdf')
        db.session.add(d1)

        db.session.commit()
        print('Seeded project id:', p.id, 'stages:', [s.id for s in (s1, s2, s3)])


if __name__ == '__main__':
    main()
