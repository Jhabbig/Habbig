"""Source network analysis — echo chamber detection + independence scoring.

Adds:
  - source_relationships: pairwise metrics for every source pair with >= 5
    shared markets (agreement_rate, both_correct_rate, independence score,
    relationship classification)
  - source_networks: periodic snapshots of the full network graph including
    echo chamber clusters and most-independent sources
"""

revision = "021"
down_revision = "020"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS source_relationships (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            source_a                TEXT NOT NULL,
            source_b                TEXT NOT NULL,
            markets_both_predicted  INTEGER NOT NULL DEFAULT 0,
            agreement_rate          REAL NOT NULL DEFAULT 0.0,
            both_correct_rate       REAL NOT NULL DEFAULT 0.0,
            independent_signal_score REAL NOT NULL DEFAULT 0.5,
            relationship_type       TEXT NOT NULL DEFAULT 'independent',
            last_computed_at        INTEGER NOT NULL,
            UNIQUE(source_a, source_b)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_src_rel_a ON source_relationships(source_a)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_src_rel_b ON source_relationships(source_b)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_src_rel_type ON source_relationships(relationship_type)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS source_networks (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            computed_at             INTEGER NOT NULL,
            total_sources           INTEGER NOT NULL DEFAULT 0,
            total_relationships     INTEGER NOT NULL DEFAULT 0,
            echo_chamber_clusters   TEXT NOT NULL DEFAULT '[]',
            most_independent_sources TEXT NOT NULL DEFAULT '[]'
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_src_net_at ON source_networks(computed_at)")


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS source_networks")
    c.execute("DROP TABLE IF EXISTS source_relationships")
