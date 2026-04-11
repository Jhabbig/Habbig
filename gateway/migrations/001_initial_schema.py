"""Initial schema marker.

The original schema is defined in db.py::SCHEMA and applied by init_db().
This migration is a no-op that records the baseline so subsequent
migrations have a known starting point in schema_version.
"""

revision = "001"
down_revision = None


def upgrade(c):
    # The SCHEMA constant in db.py already ran; nothing to do here.
    # This migration exists only to mark the baseline.
    pass


def downgrade(c):
    # No-op: the baseline is db.py's SCHEMA and cannot be rolled back
    # without dropping the entire database.
    pass
