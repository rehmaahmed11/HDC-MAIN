"""HDC models.materials — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive, _pkt_today

class MaterialUsage(db.Model):
    __tablename__ = 'hdc_material_usage'
    id         = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    stage_id   = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    material_id= db.Column(db.Integer, db.ForeignKey('hdc_material.id'), nullable=False)
    qty        = db.Column(db.Float, default=0.0)
    rate       = db.Column(db.Float, default=0.0)
    total      = db.Column(db.Float, default=0.0)
    used_at    = db.Column(db.Date, default=_pkt_today)
    activity_at= db.Column(db.DateTime, default=_pkt_now_naive)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    project  = db.relationship('Project', backref='material_usages')
    stage    = db.relationship('Stage', backref='material_usages')
    material = db.relationship('Material', backref='usages')


class MaterialCategory(db.Model):
    """Item type a material — and therefore every purchase/delivery/usage — belongs to.

    Categories are the vocabulary the field asked for by name: Electric
    Materials, Plumbing Materials, Cement, Steel …  A material carries one
    category; a purchase line may repeat it or override it for that line only
    (see ``PurchaseV2.category_id``).
    """
    __tablename__ = 'hdc_material_category'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), nullable=False)
    status     = db.Column(db.String(20), default='active')
    is_void    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive)


class Supplier(db.Model):
    __tablename__ = 'hdc_supplier'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), nullable=False)
    phone      = db.Column(db.String(30))
    status     = db.Column(db.String(20), default='active')
    is_void    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at = db.Column(db.DateTime, default=_pkt_now_naive)


class SupplierCategory(db.Model):
    """Which categories (item types) a supplier deals in — stated, not derived.

    The supplier list can always be narrowed by what a supplier has actually
    supplied (that comes from purchases); this table lets the operator say
    "this supplier deals in Cement and Steel" up front, so the list is useful
    before the first purchase is recorded.
    """
    __tablename__ = 'hdc_supplier_category'
    id          = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('hdc_supplier.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('hdc_material_category.id'), nullable=False)
    created_at  = db.Column(db.DateTime, default=_pkt_now_naive)

    supplier = db.relationship('Supplier', backref='category_links')
    category = db.relationship('MaterialCategory', backref='supplier_links')


class SupplierSite(db.Model):
    """Which site/project a supplier is selected for — supplier ⇄ site tracking.

    Both directions read from here: a supplier shows the sites it is assigned
    to, and a site shows the suppliers selected for it.  Actual deliveries are
    tracked separately (they come from ``Delivery``), so the two together
    answer "who supplies this site" and "where does this supplier supply".
    """
    __tablename__ = 'hdc_supplier_site'
    id          = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('hdc_supplier.id'), nullable=False)
    project_id  = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    note        = db.Column(db.String(200))
    created_at  = db.Column(db.DateTime, default=_pkt_now_naive)

    supplier = db.relationship('Supplier', backref='site_links')
    project  = db.relationship('Project', backref='supplier_links')


class MaterialV2(db.Model):
    __tablename__ = 'hdc_material_v2'
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(120), nullable=False)
    unit        = db.Column(db.String(20), default='KG')
    category_id = db.Column(db.Integer, db.ForeignKey('hdc_material_category.id'), nullable=True)
    status      = db.Column(db.String(20), default='active')
    is_void     = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at  = db.Column(db.DateTime, default=_pkt_now_naive)

    category = db.relationship('MaterialCategory', backref='materials')


class PurchaseV2(db.Model):
    __tablename__ = 'hdc_purchase_v2'
    id             = db.Column(db.Integer, primary_key=True)
    supplier_id    = db.Column(db.Integer, db.ForeignKey('hdc_supplier.id'), nullable=False)
    material_id    = db.Column(db.Integer, db.ForeignKey('hdc_material_v2.id'), nullable=False)
    # The line's own category.  Filled from the material when the purchase is
    # recorded (so every purchase row carries a category from day one) and
    # editable afterwards, which is the "full control" the field asked for.
    category_id    = db.Column(db.Integer, db.ForeignKey('hdc_material_category.id'), nullable=True)
    unit_price     = db.Column(db.Float, default=0.0)
    quantity       = db.Column(db.Float, default=0.0)
    total_amount   = db.Column(db.Float, default=0.0)
    payment_status = db.Column(db.String(20), default='unpaid')
    date           = db.Column(db.Date, default=_pkt_today)
    notes          = db.Column(db.String(300))
    challan_no     = db.Column(db.String(80))
    is_void        = db.Column(db.Boolean, default=False)
    void_reason    = db.Column(db.String(250))
    voided_at      = db.Column(db.DateTime, nullable=True)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)
    updated_at     = db.Column(db.DateTime, default=_pkt_now_naive)

    supplier = db.relationship('Supplier', backref='purchases_v2')
    material = db.relationship('MaterialV2', backref='purchases_v2')
    category = db.relationship('MaterialCategory', backref='purchases_v2')


class SupplierLedger(db.Model):
    __tablename__ = 'hdc_supplier_ledger'
    id             = db.Column(db.Integer, primary_key=True)
    supplier_id    = db.Column(db.Integer, db.ForeignKey('hdc_supplier.id'), nullable=False)
    entry_type     = db.Column(db.String(20), nullable=False)  # debit / credit
    amount         = db.Column(db.Float, default=0.0)
    reference_type = db.Column(db.String(50))
    reference_id   = db.Column(db.Integer)
    note           = db.Column(db.String(300))
    is_void        = db.Column(db.Boolean, default=False)
    void_reason    = db.Column(db.String(250))
    voided_at      = db.Column(db.DateTime, nullable=True)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)

    supplier = db.relationship('Supplier', backref='ledger_rows')


class Delivery(db.Model):
    __tablename__ = 'hdc_delivery'
    id             = db.Column(db.Integer, primary_key=True)
    purchase_id    = db.Column(db.Integer, db.ForeignKey('hdc_purchase_v2.id'), nullable=False)
    material_id    = db.Column(db.Integer, db.ForeignKey('hdc_material_v2.id'), nullable=False)
    project_id     = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    stage_id       = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    quantity       = db.Column(db.Float, default=0.0)
    date           = db.Column(db.Date, default=_pkt_today)
    notes          = db.Column(db.String(300))
    delivery_person= db.Column(db.String(120))
    is_void        = db.Column(db.Boolean, default=False)
    void_reason    = db.Column(db.String(250))
    voided_at      = db.Column(db.DateTime, nullable=True)
    created_at     = db.Column(db.DateTime, default=_pkt_now_naive)

    purchase = db.relationship('PurchaseV2', backref='deliveries')
    material = db.relationship('MaterialV2', backref='deliveries')
    project  = db.relationship('Project', backref='deliveries_v2')
    stage    = db.relationship('Stage', backref='deliveries_v2')


class UsageLogV2(db.Model):
    __tablename__ = 'hdc_usage_log_v2'
    id         = db.Column(db.Integer, primary_key=True)
    purchase_id= db.Column(db.Integer, db.ForeignKey('hdc_purchase_v2.id'), nullable=True)
    material_id= db.Column(db.Integer, db.ForeignKey('hdc_material_v2.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    stage_id   = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    quantity   = db.Column(db.Float, default=0.0)
    cost       = db.Column(db.Float, default=0.0)
    date       = db.Column(db.Date, default=_pkt_today)
    notes      = db.Column(db.String(300))
    is_void    = db.Column(db.Boolean, default=False)
    void_reason= db.Column(db.String(250))
    voided_at  = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)

    purchase = db.relationship('PurchaseV2', backref='usage_logs_v2')
    material = db.relationship('MaterialV2', backref='usage_logs_v2')
    project  = db.relationship('Project', backref='usage_logs_v2')
    stage    = db.relationship('Stage', backref='usage_logs_v2')


class Material(db.Model):
    __tablename__ = 'hdc_material'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), nullable=False)
    unit       = db.Column(db.String(30), nullable=False, default='Nos')
    is_active  = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_pkt_now_naive)
    purchases  = db.relationship('Purchase', backref='material', lazy=True)


class Purchase(db.Model):
    __tablename__ = 'hdc_purchase'
    id          = db.Column(db.Integer, primary_key=True)
    project_id  = db.Column(db.Integer, db.ForeignKey('hdc_project.id'), nullable=False)
    stage_id    = db.Column(db.Integer, db.ForeignKey('hdc_stage.id'), nullable=True)
    material_id = db.Column(db.Integer, db.ForeignKey('hdc_material.id'), nullable=False)
    stage       = db.relationship('Stage', backref='purchase_records')
    entry_type  = db.Column(db.String(20), default='purchase')  # purchase / return
    date        = db.Column(db.Date, default=_pkt_today)
    qty         = db.Column(db.Float, default=0.0)
    rate        = db.Column(db.Float, default=0.0)
    total       = db.Column(db.Float, default=0.0)
    supplier_name = db.Column(db.String(120))
    return_ref  = db.Column(db.String(80))
    return_reason = db.Column(db.String(200))
    approved_by = db.Column(db.String(100))
    notes       = db.Column(db.String(200))
    activity_at = db.Column(db.DateTime, default=_pkt_now_naive)
    created_at  = db.Column(db.DateTime, default=_pkt_now_naive)
