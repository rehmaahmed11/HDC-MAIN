"""Account classification registry — the single authoritative definition of the
Category → Subcategory → Account Type → {channel, linked entity} hierarchy.

Ported from the AMS accounts model (``blueprints/accounts/classification.py``)
and re-voiced for a construction ERP.

Why a registry?
---------------
HDC's legacy Account form exposes three overlapping selectors (``group``,
``mode``, ``type``) which permit contradictory combinations such as
``credit_debit + bank + client``.  This module replaces that with **one
controlled hierarchy**, so an invalid combination can never be saved: the
server validates against the same tree the browser renders.

The registry is intentionally *data only*.  No accounting or ledger behaviour
lives here — it governs how an account is *classified* and which detail fields
a given classification requires.

Legacy compatibility
--------------------
Accounts predate this hierarchy.  :func:`legacy_to_classification` maps the old
``type`` (company / cash / bank / person / vendor / client) onto a valid
classification so historical rows keep loading after the schema upgrade, and
:func:`backfill_account_classification` fills the new columns in place.  The
legacy columns are never removed or rewritten.
"""

from __future__ import annotations

# Channel / medium vocabulary.  ``ledger_only`` means the balance is tracked on
# the account ledger only — there is no physical cash / bank / wallet
# instrument behind it (e.g. a client receivable or a worker advance).
CHANNELS = ("cash", "bank", "digital_wallet", "ledger_only", "other")

CHANNEL_LABELS = {
    "cash": "Cash",
    "bank": "Bank",
    "digital_wallet": "Digital Wallet",
    "ledger_only": "Ledger Only",
    "other": "Other",
}

# Linked-entity vocabulary (descriptive in HDC: the ledger resolves parties).
ENTITY_TYPES = ("none", "client", "supplier", "worker", "subcontractor", "partner", "other")

ENTITY_LABELS = {
    "none": "None",
    "client": "Client",
    "supplier": "Supplier",
    "worker": "Worker",
    "subcontractor": "Subcontractor",
    "partner": "Partner",
    "other": "Other",
}

# ``status`` lives on Account.status (active / inactive / archived).
STATUSES = ("active", "inactive", "archived")


def _node(channel, entity="none"):
    """Build a leaf definition.

    ``channel`` may be a single channel (forced) or a list of allowed channels
    (the first entry is the default).  ``entity`` declares the linked entity
    type this classification expects.
    """
    if isinstance(channel, (list, tuple)):
        allowed = list(channel)
        default = allowed[0]
    else:
        allowed = [channel]
        default = channel
    return {"channels": allowed, "default_channel": default, "entity": entity}


# The authoritative hierarchy: (Category, Subcategory, Account Type).
CLASSIFICATION = {
    "Assets": {
        "label": "Assets",
        "description": "Money or value the business owns, or that is receivable by the business.",
        "subcategories": {
            "Cash": {
                "label": "Cash",
                "account_types": {
                    "Main Cash": _node("cash"),
                    "Petty Cash": _node("cash"),
                    "Site Cash": _node("cash"),
                    "Cash Drawer": _node("cash"),
                    "Temporary Cash": _node("cash"),
                },
            },
            "Bank": {
                "label": "Bank",
                "account_types": {
                    "Operating Bank": _node("bank"),
                    "Collection Bank": _node("bank"),
                    "Savings Bank": _node("bank"),
                    "Settlement Bank": _node("bank"),
                },
            },
            "Digital Wallet": {
                "label": "Digital Wallet",
                "account_types": {
                    "Mobile Wallet": _node("digital_wallet"),
                    "Payment App": _node("digital_wallet"),
                },
            },
            "Client Receivables": {
                "label": "Client Receivables",
                "account_types": {
                    "Client Ledger": _node("ledger_only", "client"),
                    "Client Advance Receivable": _node("ledger_only", "client"),
                },
            },
            "Advances Given": {
                "label": "Advances Given",
                "account_types": {
                    "Advance to Worker": _node("ledger_only", "worker"),
                    "Advance to Subcontractor": _node("ledger_only", "subcontractor"),
                    "Advance to Supplier": _node("ledger_only", "supplier"),
                    "Staff Advance": _node("ledger_only", "worker"),
                    "Advance to Party": _node("ledger_only", "other"),
                },
            },
            "Other Assets": {
                "label": "Other Assets",
                "account_types": {
                    "General Asset": _node(["ledger_only", "cash", "bank", "other"]),
                    "Security Deposit Given": _node("ledger_only", "other"),
                },
            },
        },
    },
    "Liabilities": {
        "label": "Liabilities",
        "description": "Money or obligations the business owes.",
        "subcategories": {
            "Supplier Payables": {
                "label": "Supplier Payables",
                "account_types": {
                    "Supplier Ledger": _node("ledger_only", "supplier"),
                    "Supplier Outstanding": _node("ledger_only", "supplier"),
                },
            },
            "Subcontractor Payables": {
                "label": "Subcontractor Payables",
                "account_types": {
                    "Subcontractor Ledger": _node("ledger_only", "subcontractor"),
                    "Subcontractor Retention": _node("ledger_only", "subcontractor"),
                },
            },
            "Worker Payables": {
                "label": "Worker Payables",
                "account_types": {
                    "Wage Payable": _node("ledger_only", "worker"),
                    "Worker Ledger": _node("ledger_only", "worker"),
                },
            },
            "Loans & Borrowings": {
                "label": "Loans & Borrowings",
                "account_types": {
                    "Bank Loan": _node("bank"),
                    "Private Loan": _node(["ledger_only", "cash", "bank"], "other"),
                    "Credit Line": _node("bank"),
                },
            },
            "Other Liabilities": {
                "label": "Other Liabilities",
                "account_types": {
                    "Security Deposit Received": _node("ledger_only", "other"),
                    "Tax Payable": _node("ledger_only"),
                    "General Liability": _node(["ledger_only", "cash", "bank", "other"]),
                },
            },
        },
    },
    "Equity": {
        "label": "Equity",
        "description": "Owner capital and drawings — the business's own funds.",
        "subcategories": {
            "Owner Capital": {
                "label": "Owner Capital",
                "account_types": {
                    "Owner Equity": _node(["ledger_only", "bank", "cash"]),
                    "Partner Capital": _node(["ledger_only", "bank", "cash"], "partner"),
                },
            },
            "Drawings": {
                "label": "Drawings",
                "account_types": {
                    "Owner Drawings": _node("ledger_only", "partner"),
                },
            },
        },
    },
    "Income": {
        "label": "Income",
        "description": "Value the business earns.",
        "subcategories": {
            "Project Income": {
                "label": "Project Income",
                "account_types": {
                    "Contract Receipt": _node("ledger_only", "client"),
                    "Owner Payment": _node("ledger_only", "client"),
                },
            },
            "Other Income": {
                "label": "Other Income",
                "account_types": {
                    "Scrap Sale": _node(["ledger_only", "cash"]),
                    "Rental Income": _node(["ledger_only", "cash", "bank"]),
                    "General Income": _node(["ledger_only", "cash", "bank", "other"]),
                },
            },
        },
    },
    "Expenses": {
        "label": "Expenses",
        "description": "Cost the business incurs.",
        "subcategories": {
            "Direct Project Cost": {
                "label": "Direct Project Cost",
                "account_types": {
                    "Material Cost": _node("ledger_only", "supplier"),
                    "Labour Cost": _node("ledger_only", "worker"),
                    "Subcontract Cost": _node("ledger_only", "subcontractor"),
                    "Site Expense": _node("ledger_only"),
                },
            },
            "Indirect & Overhead": {
                "label": "Indirect & Overhead",
                "account_types": {
                    "Office Expense": _node("ledger_only"),
                    "Fuel & Transport": _node(["ledger_only", "cash"]),
                    "Equipment & Machinery": _node("ledger_only"),
                    "General Overhead": _node("ledger_only"),
                },
            },
            "Payroll & Benefits": {
                "label": "Payroll & Benefits",
                "account_types": {
                    "Staff Salary": _node("ledger_only", "worker"),
                    "Wage Expense": _node("ledger_only", "worker"),
                },
            },
            "Personal": {
                "label": "Personal",
                "account_types": {
                    "Personal Expense": _node("ledger_only"),
                    "Owner Personal": _node("ledger_only", "partner"),
                },
            },
        },
    },
}


def categories():
    """Top-level category names in declaration order."""
    return list(CLASSIFICATION.keys())


def subcategories(category):
    """Subcategory names for ``category`` (``[]`` when unknown)."""
    node = CLASSIFICATION.get(category) or {}
    return list((node.get("subcategories") or {}).keys())


def account_types(category, subcategory):
    """Account-type names for ``category`` / ``subcategory`` (``[]`` when unknown)."""
    sub = ((CLASSIFICATION.get(category) or {}).get("subcategories") or {}).get(subcategory) or {}
    return list((sub.get("account_types") or {}).keys())


def leaf(category, subcategory, account_type):
    """The leaf definition dict, or ``None`` when the triple is not valid."""
    sub = ((CLASSIFICATION.get(category) or {}).get("subcategories") or {}).get(subcategory) or {}
    return (sub.get("account_types") or {}).get(account_type)


def is_valid(category, subcategory, account_type, channel=None, entity=None):
    """True when the triple (and optional channel / entity) is a legal combination."""
    node = leaf(category, subcategory, account_type)
    if not node:
        return False
    if channel and channel not in node["channels"]:
        return False
    if entity is not None and entity != node["entity"] and not (entity == "none" and node["entity"] == "none"):
        # ``entity`` is a hint, not a hard constraint: only reject a *declared*
        # mismatch when the leaf requires a specific link.
        if node["entity"] != "none" and entity != node["entity"]:
            return False
    return True


def default_channel(category, subcategory, account_type):
    """Default channel for a classification (falls back to ``ledger_only``)."""
    node = leaf(category, subcategory, account_type)
    return (node or {}).get("default_channel", "ledger_only")


def required_entity(category, subcategory, account_type):
    """Linked entity type the classification expects (``'none'`` when free)."""
    node = leaf(category, subcategory, account_type)
    return (node or {}).get("entity", "none")


def classification_tree():
    """JSON-serialisable projection for client-side cascading selects."""
    out = {}
    for cat, cat_node in CLASSIFICATION.items():
        subs = {}
        for sub_name, sub_node in (cat_node.get("subcategories") or {}).items():
            types = {}
            for type_name, leaf_node in (sub_node.get("account_types") or {}).items():
                types[type_name] = {
                    "channels": leaf_node["channels"],
                    "default_channel": leaf_node["default_channel"],
                    "entity": leaf_node["entity"],
                }
            subs[sub_name] = {"label": sub_node.get("label", sub_name), "account_types": types}
        out[cat] = {
            "label": cat_node.get("label", cat),
            "description": cat_node.get("description", ""),
            "subcategories": subs,
        }
    return out


# ---------------------------------------------------------------------------
# Legacy mapping
# ---------------------------------------------------------------------------

# HDC's legacy Account.type -> a valid classification triple.
_LEGACY_MAP = {
    "company": ("Assets", "Cash", "Main Cash"),
    "cash": ("Assets", "Cash", "Main Cash"),
    "bank": ("Assets", "Bank", "Operating Bank"),
    "client": ("Assets", "Client Receivables", "Client Ledger"),
    "vendor": ("Liabilities", "Supplier Payables", "Supplier Ledger"),
    "person": ("Assets", "Advances Given", "Advance to Party"),
}

_LEGACY_ENTITY = {
    "company": "none",
    "cash": "none",
    "bank": "none",
    "client": "client",
    "vendor": "supplier",
    "person": "other",
}


def legacy_to_classification(acc_type, *, is_bank=False):
    """Map HDC's legacy ``Account.type`` onto a valid classification triple.

    ``is_bank`` distinguishes a company account that carries bank details from
    a plain cash account (HDC stores both as ``type='company'`` historically).
    """
    raw = (acc_type or "").strip().lower()
    if raw in ("company", "cash") and is_bank:
        return ("Assets", "Bank", "Operating Bank")
    if raw not in _LEGACY_MAP:
        return ("Assets", "Other Assets", "General Asset")
    return _LEGACY_MAP[raw]


def legacy_entity(acc_type):
    """Linked entity type implied by HDC's legacy ``Account.type``."""
    return _LEGACY_ENTITY.get((acc_type or "").strip().lower(), "none")


def backfill_account_classification(Account, db, *, commit=True):
    """Fill ``class_*`` / ``channel`` / ``linked_entity_type`` on legacy accounts.

    Only rows with an empty classification are touched, so the function is
    idempotent and safe to run on every startup.  Returns the number of rows
    updated.
    """
    from hdc.utils.money import to_minor

    changed = 0
    try:
        rows = Account.query.all()
    except Exception:
        return 0
    for acc in rows:
        if (acc.class_category or "").strip() and (acc.class_account_type or "").strip():
            # Already classified — just make sure the minor mirror exists.
            if getattr(acc, "opening_balance_minor", None) is None:
                try:
                    acc.opening_balance_minor = to_minor(acc.opening_balance or 0)
                    changed += 1
                except Exception:
                    pass
            continue
        raw_type = (acc.type or "").strip().lower()
        is_bank = bool((acc.bank_name or "").strip()) or bool((acc.account_number or "").strip()) or raw_type == "bank"
        cat, sub, atype = legacy_to_classification(raw_type, is_bank=is_bank)
        acc.class_category = cat
        acc.class_subcategory = sub
        acc.class_account_type = atype
        acc.channel = default_channel(cat, sub, atype)
        acc.linked_entity_type = legacy_entity(raw_type)
        if getattr(acc, "opening_balance_minor", None) is None:
            try:
                acc.opening_balance_minor = to_minor(acc.opening_balance or 0)
            except Exception:
                acc.opening_balance_minor = 0
        changed += 1
    if changed and commit:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    return changed
