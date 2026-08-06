"""DEMO DATA ONLY — a fictional company, so there is something to ask about out of the box.

Do not run this on a deployment you care about. The logins below have published passwords,
and the workflows belong to an invented company that is not yours.

For a real install use `setup.py`, which creates one admin account with a password you
choose or one that is generated, and leaves the knowledge base empty:

    python3 setup.py --org "Your Company" --admin you@yourcompany.com
    python3 ingest.py ./your-documents --publish

These workflows are hand-authored in the shape a senior's material takes after ingestion,
and they exercise every retrieval path: a scoped answer, an error-code match, two similar
workflows that must not be confused, and a question spanning several documents.

Run:  python3 seed.py
"""
from sb import auth, db

USERS = [
    # email, name, role, password
    ("yaswanth@spacelabs.dev", "Yaswanth (new hire)", "user", "yaswanth123"),
    ("roshan@spacelabs.dev", "Roshan (senior)", "author", "roshan123"),
    ("admin@spacelabs.dev", "Admin", "admin", "admin123"),
]

WORKFLOWS = [
    {
        "wf_key": "DEPLOY.PROD_ROLLBACK",
        "name": "Roll back a production deploy",
        "category": "Deployment",
        "owner": "sarah",
        "verified_by": "sarah",
        "verified_at": "2026-07-19",
        "summary": "Revert a bad production release in Argo CD back to the previous healthy revision.",
        "trigger_phrases": ["rollback", "roll back", "revert deploy", "bad deploy", "production rollback",
                            "prod rollback", "undo release"],
        "steps": [
            {"title": "Confirm the bad deploy", "body": "Open Argo CD → the prod app. Confirm the current "
             "revision is the faulty one and note the previous healthy revision hash.",
             "verification": "You can see both the current (unhealthy) and previous (healthy) revisions listed."},
            {"title": "Put the service in maintenance mode", "body": "Run `spacectl maint on --svc <name>` "
             "to drain traffic before rolling back.",
             "verification": "The status banner shows 'maintenance' and traffic drops to zero in Grafana."},
            {"title": "Roll back the release", "body": "In Argo CD click History → select the previous healthy "
             "revision → Rollback. Or run `argocd app rollback <app> <revision>`.",
             "verification": "Argo shows the previous revision as Synced and Healthy.",
             "mistakes": ["Rolling back before enabling maintenance mode can drop in-flight requests."]},
            {"title": "Re-run smoke tests", "body": "Run `spacectl smoke --svc <name>` and wait for green.",
             "verification": "All smoke checks pass."},
            {"title": "Exit maintenance mode", "body": "Run `spacectl maint off --svc <name>`.",
             "verification": "Traffic returns to normal in Grafana and the banner clears."},
        ],
        "known_errors": [
            {"code": "ERR_LEASE_HELD", "cause": "The previous rollback still holds the deployment lease.",
             "resolution": "Check the stuck lease with `spacectl lease ls`, force-release it with "
             "`spacectl lease release <id>`, then re-run the rollback step."},
        ],
        "faqs": [
            {"question": "Can I roll back without maintenance mode?",
             "answer": "Only for stateless read paths. For anything handling writes, enable maintenance first."},
        ],
    },
    {
        "wf_key": "DEPLOY.STAGING_ROLLBACK",
        "name": "Roll back a staging deploy",
        "category": "Deployment",
        "owner": "sarah",
        "verified_by": "sarah",
        "verified_at": "2026-07-10",
        "summary": "Revert a broken staging release. Lighter process than production — no maintenance window.",
        "trigger_phrases": ["rollback", "roll back", "revert deploy", "staging rollback", "undo staging"],
        "steps": [
            {"title": "Open the staging app in Argo CD", "body": "Find the staging app and the previous revision.",
             "verification": "Previous revision hash is visible."},
            {"title": "Rollback directly", "body": "Click Rollback on the previous revision. No maintenance mode "
             "is needed in staging.", "verification": "Argo shows Synced + Healthy."},
        ],
        "known_errors": [],
        "faqs": [],
    },
    {
        "wf_key": "ENV.LOCAL_SETUP",
        "name": "Set up your local dev environment",
        "category": "Onboarding",
        "owner": "arjun",
        "verified_by": "arjun",
        "verified_at": "2026-07-15",
        "summary": "Get the SpaceLabs monorepo building and running on your laptop.",
        "trigger_phrases": ["local setup", "dev environment", "first week", "new hire", "getting started",
                            "onboarding", "set up my machine", "clone the repo"],
        "steps": [
            {"title": "Install prerequisites", "body": "Install Node 22, Python 3.12, and Docker Desktop.",
             "verification": "`node -v`, `python3 --version`, and `docker --version` all print versions."},
            {"title": "Clone and bootstrap", "body": "Clone the monorepo and run `make bootstrap`.",
             "verification": "`make bootstrap` finishes without errors."},
            {"title": "Run the app", "body": "Run `make dev` and open http://localhost:3000.",
             "verification": "The local app loads."},
        ],
        "known_errors": [],
        "faqs": [{"question": "make bootstrap fails on Docker",
                  "answer": "Make sure Docker Desktop is running before `make bootstrap`."}],
    },
    {
        "wf_key": "ACCESS.AWS_REQUEST",
        "name": "Request AWS access",
        "category": "Onboarding",
        "owner": "priya",
        "verified_by": "priya",
        "verified_at": "2026-07-12",
        "summary": "Get IAM access to the SpaceLabs AWS accounts through the access portal.",
        "trigger_phrases": ["aws access", "iam", "cloud access", "first week", "new hire", "onboarding",
                            "request access", "permissions"],
        "steps": [
            {"title": "Open the access portal", "body": "Go to the internal access portal and pick 'AWS'.",
             "verification": "You see the list of AWS accounts."},
            {"title": "Request the developer role", "body": "Request the `developer` role for the dev account. "
             "Your manager approves.", "verification": "Request shows 'pending approval'."},
        ],
        "known_errors": [],
        "faqs": [],
    },
    {
        "wf_key": "ONCALL.FIRST_SHIFT",
        "name": "Prepare for your first on-call shift",
        "category": "Onboarding",
        "owner": "sarah",
        "verified_by": "sarah",
        "verified_at": "2026-07-18",
        "summary": "What to set up and know before your first on-call rotation.",
        "trigger_phrases": ["on-call", "oncall", "first shift", "pager", "first week", "new hire", "onboarding"],
        "steps": [
            {"title": "Install PagerDuty", "body": "Install the PagerDuty app and confirm you're in the rotation.",
             "verification": "You appear in the on-call schedule."},
            {"title": "Bookmark the runbooks", "body": "Bookmark the deploy and rollback workflows in Spacebot.",
             "verification": "You can open them quickly."},
        ],
        "known_errors": [],
        "faqs": [],
    },
    {
        "wf_key": "PROC.VENDOR_APPROVAL",
        "name": "Get a vendor approved",
        "category": "Procurement",
        "owner": "meena",
        "verified_by": "meena",
        "verified_at": "2026-07-11",
        "summary": "Approve a new vendor in SAP before you can raise a purchase order for them.",
        "trigger_phrases": ["vendor approval", "approve vendor", "new vendor", "vendor onboarding"],
        "steps": [
            {"title": "Create the vendor record", "body": "In SAP → Vendor Master → Create, fill company + tax ID.",
             "verification": "Vendor record saves with a vendor number."},
            {"title": "Submit for approval", "body": "Submit the vendor for finance approval (2–3 days).",
             "verification": "Vendor status shows 'pending approval'."},
        ],
        "known_errors": [
            {"code": "ERR_NO_TAX_ID", "cause": "Vendor tax ID field is empty.",
             "resolution": "Enter the vendor's tax ID in the Tax Information panel, then resubmit."}],
        "faqs": [],
    },
    {
        "wf_key": "PROC.CREATE_ORDER",
        "name": "Create a purchase order",
        "category": "Procurement",
        "owner": "meena",
        "verified_by": "meena",
        "verified_at": "2026-07-16",
        "summary": "Raise a purchase order in SAP for an already-approved vendor.",
        "trigger_phrases": ["purchase order", "create po", "raise po", "procurement order", "buy from vendor"],
        "steps": [
            {"title": "Open Create PO", "body": "SAP → Procurement → Create PO.",
             "verification": "The Create PO form opens."},
            {"title": "Select the approved vendor", "body": "Pick the vendor. They must be approved first.",
             "verification": "Vendor auto-fills with address + tax details.",
             "mistakes": ["If the vendor isn't approved, start with Vendor Approval first."]},
            {"title": "Add lines and submit", "body": "Add line items, attach the quote, submit for approval.",
             "verification": "PO shows status 'Submitted'."},
        ],
        "known_errors": [],
        "faqs": [],
    },
]

RELATIONS = [
    ("PROC.CREATE_ORDER", "PROC.VENDOR_APPROVAL", "prerequisite"),
    ("DEPLOY.PROD_ROLLBACK", "DEPLOY.STAGING_ROLLBACK", "alternative"),
    ("ENV.LOCAL_SETUP", "ACCESS.AWS_REQUEST", "next"),
    ("ACCESS.AWS_REQUEST", "ONCALL.FIRST_SHIFT", "next"),
]


def main():
    db.init_db()
    if db.list_workflows():
        print("This database already has content. seed.py loads DEMO data over the top,")
        print("including logins whose passwords are published in this file.")
        print("If that is what you want, pass --yes.\n")
        import sys
        if "--yes" not in sys.argv:
            return
    for wf in WORKFLOWS:
        db.upsert_workflow(wf)
        print(f"  seeded {wf['wf_key']}  ({len(wf['steps'])} steps)")
    for a, b, kind in RELATIONS:
        db.add_relation(a, b, kind)
    for email, name, role, pw in USERS:
        auth.ensure_user(email, name, role, pw)
    print(f"\nDone. {len(WORKFLOWS)} workflows, {len(RELATIONS)} relations, {len(USERS)} users.")
    print("\nDemo logins (passwords are public — demo use only):")
    for email, name, role, pw in USERS:
        print(f"  {role:6} {email}  /  {pw}")
    print("\nFor your own deployment instead: python3 setup.py --org ... --admin ...")


if __name__ == "__main__":
    main()
