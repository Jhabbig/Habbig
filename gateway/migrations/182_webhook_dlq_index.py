"""DLQ partial index on first_failed_at for fast admin list."""
revision = "182"
down_revision = "181"

def upgrade(c):
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_webhook_dlq_open_recent
        ON webhook_dead_letter(first_failed_at DESC)
        WHERE requeued_at IS NULL
    """)

def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_webhook_dlq_open_recent")
