"""Source network analysis — relationships + weekly snapshots.

Two tables:

  source_relationships
    Pairwise stats between two sources that have predicted on ≥ 5 markets.
    Classification drives the "echo chamber" / "independent" badges on
    source detail pages, and feeds the network-adjusted consensus (down-
    weights duplicates within the same echo cluster).

  source_networks
    One row per weekly snapshot. Holds cluster assignments + the top-N
    most-independent sources. Lets the admin/debug UI show network state
    over time without recomputing.
"""

revision = "054"
down_revision = "053"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS source_relationships (
            id                        INTEGER PRIMARY KEY AUTOINCREMENT,
            source_a                  TEXT NOT NULL,
            source_b                  TEXT NOT NULL,
            markets_both_predicted    INTEGER NOT NULL DEFAULT 0,
            agreement_rate            REAL,
            both_correct_rate         REAL,
            independent_signal_score  REAL,
            relationship_type         TEXT,
            last_computed_at          INTEGER NOT NULL,
            UNIQUE(source_a, source_b)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_source_rel_a ON source_relationships(source_a)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_source_rel_b ON source_relationships(source_b)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_source_rel_type ON source_relationships(relationship_type, last_computed_at)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS source_networks (
            id                         INTEGER PRIMARY KEY AUTOINCREMENT,
            computed_at                INTEGER NOT NULL,
            echo_chamber_clusters      TEXT NOT NULL DEFAULT '[]',
            most_independent_sources   TEXT NOT NULL DEFAULT '[]',
            stats_json                 TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_source_networks_time ON source_networks(computed_at DESC)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_source_networks_time")
    c.execute("DROP TABLE IF EXISTS source_networks")
    c.execute("DROP INDEX IF EXISTS idx_source_rel_type")
    c.execute("DROP INDEX IF EXISTS idx_source_rel_b")
    c.execute("DROP INDEX IF EXISTS idx_source_rel_a")
    c.execute("DROP TABLE IF EXISTS source_relationships")
