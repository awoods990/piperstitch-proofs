"""Empties Proofs entirely -- every account, proof, contact, file and
event -- the clean start before going live, after testing. Proofs has no
admin login of its own (every user is an account), so this runs from a
shell on the service:

    railway run --service <proofs-service> python scripts/reset_all_data.py --yes-really

or, in Railway's web shell for the service, the same command without
`railway run`. Refuses to run without the flag. Generated artifacts under
ARTIFACT_DIR go too. Stripe is not touched.
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app import config, db  # noqa: E402


def main() -> None:
    if "--yes-really" not in sys.argv:
        print("This removes ALL Proofs data (accounts, proofs, contacts, files). Re-run with --yes-really to proceed.")
        sys.exit(2)
    db.init_db()
    tables = [t.name for t in reversed(db.Base.metadata.sorted_tables)]
    removed = {}
    with db.engine.begin() as conn:
        for name in tables:
            n = conn.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar() or 0
            conn.execute(text(f"DELETE FROM {name}"))
            removed[name] = n
    if config.DATABASE_URL.startswith("sqlite"):
        with db.engine.begin() as conn:
            conn.execute(text("VACUUM"))
    artifacts = Path(config.ARTIFACT_DIR)
    files = 0
    if artifacts.is_dir():
        for child in artifacts.iterdir():
            files += 1
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    total = sum(removed.values())
    for name, n in removed.items():
        if n:
            print(f"  {name}: {n}")
    print(f"Removed {total} rows across {len(tables)} tables and {files} artifact entries. Proofs is clean.")


if __name__ == "__main__":
    main()
