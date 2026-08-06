#!/usr/bin/env python3
"""Undo metadata a test run wrote over real entries.

`test_formats` used to ingest into POLICY.EXPENSES, OPS.ONCALL_ROTA, PROC.VENDOR_LIST and
IT.VPN_ACCESS, stamping category "Test" and owner "tester" on all four. That reached the
product — a user asking about the VPN saw "Category: Test · Owner: @tester" under their
answer. The test now uses its own TEST.FMT_* keys; this repairs what it already broke.

The category is derived from the entry's own key prefix, which is what the keys encode.
The owner is CLEARED rather than guessed: the original is not recoverable, and inventing a
name in a field the audit trail treats as accountability would be worse than leaving it
blank for a senior to set in the Studio.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sb import db   # noqa: E402

PREFIX_CATEGORY = {
    "POLICY": "Policy",
    "OPS": "Operations",
    "PROC": "Procurement",
    "IT": "IT",
    "DEPLOY": "Deployment",
    "ACCESS": "Access",
    "ENV": "Environment",
    "ONCALL": "On-call",
    "PEOPLE": "People",
    "MODERNSIGNAL": "Company",
}

db.init_db()
fixed = 0
for card in db.get_catalog():
    cat = (card.get("category") or "").strip().lower()
    owner = (card.get("owner") or "").strip().lower()
    if cat != "test" and owner != "tester":
        continue
    prefix = card["wf_key"].split(".")[0]
    new_cat = PREFIX_CATEGORY.get(prefix, "Uncategorized")
    db.update_workflow_meta(card["wf_key"], {"category": new_cat, "owner": ""},
                            actor="repair script")
    print(f"  {card['wf_key']:24s} category Test -> {new_cat}, owner cleared")
    fixed += 1

print(f"\n{fixed} repaired" if fixed else "\nnothing to repair")
if fixed:
    print("Owners are blank on purpose — set them in Knowledge Studio → Catalog → Edit.")
