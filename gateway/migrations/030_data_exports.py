"""GDPR data export requests — track per-user data portability exports.

Each row represents one ZIP-export job. Status flows:
    pending → processing → ready → expired
    or → failed

The actual ZIP file lives on disk under DATA_EXPORT_DIR (default
/tmp/narve-exports/). The download_url is a signed (HMAC) URL valid
until expires_at; after that the file is purged by a cleanup job.
"""

revision = "032"
down_revision = "026"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS data_export_requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            requested_at    INTEGER NOT NULL,
            completed_at    INTEGER,
            download_url    TEXT,
            expires_at      INTEGER,
            status          TEXT NOT NULL DEFAULT 'pending',
            file_size_bytes INTEGER,
            file_path       TEXT,
            error           TEXT
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_dxr_user "
        "ON data_export_requests(user_id, requested_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_dxr_status "
        "ON data_export_requests(status)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_dxr_expires "
        "ON data_export_requests(expires_at)"
    )


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS data_export_requests")
