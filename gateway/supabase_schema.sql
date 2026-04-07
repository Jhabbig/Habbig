-- ============================================================================
-- Supabase Schema for Polymarket Dashboard Platform
-- ============================================================================
-- Run this in the Supabase SQL Editor after creating your project.
-- This creates all tables across gateway + dashboards in one Postgres database.
-- ============================================================================

-- ── Gateway: Profiles (extends Supabase Auth users) ────────────────────────
CREATE TABLE IF NOT EXISTS profiles (
    id          UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    username    TEXT UNIQUE NOT NULL,
    email       TEXT UNIQUE NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,  -- 0=user, 1=admin, 2=super_admin
    suspended   INTEGER NOT NULL DEFAULT 0,
    default_dashboard TEXT,
    invite_token_id BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_profiles_username ON profiles(username);

-- Auto-create profile on signup via trigger
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO profiles (id, username, email, created_at)
    VALUES (
        NEW.id,
        COALESCE(NEW.raw_user_meta_data->>'username', split_part(NEW.email, '@', 1)),
        NEW.email,
        now()
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION handle_new_user();


-- ── Gateway: Sessions (custom session tokens for cookie-based auth) ────────
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    created_at  BIGINT NOT NULL,
    expires_at  BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);


-- ── Gateway: Subscriptions ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subscriptions (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    dashboard_key   TEXT NOT NULL,
    plan            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    started_at      BIGINT NOT NULL,
    expires_at      BIGINT,
    stripe_sub_id   TEXT,
    source          TEXT NOT NULL DEFAULT 'placeholder',
    UNIQUE(user_id, dashboard_key)
);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_subs_dashboard ON subscriptions(dashboard_key);


-- ── Gateway: Invite Tokens ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS invite_tokens (
    id                  BIGSERIAL PRIMARY KEY,
    token               TEXT UNIQUE NOT NULL,
    status              TEXT NOT NULL DEFAULT 'unclaimed',
    claimed_by_user_id  UUID REFERENCES profiles(id),
    claimed_by_email    TEXT,
    note                TEXT DEFAULT '',
    target_email        TEXT,
    created_at          BIGINT NOT NULL,
    claimed_at          BIGINT
);
CREATE INDEX IF NOT EXISTS idx_invite_token ON invite_tokens(token);
CREATE INDEX IF NOT EXISTS idx_invite_status ON invite_tokens(status);


-- ── Gateway: Enquiries ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS enquiries (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT NOT NULL,
    job_title   TEXT NOT NULL,
    message     TEXT NOT NULL,
    created_at  BIGINT NOT NULL,
    read        INTEGER NOT NULL DEFAULT 0
);


-- ── Gateway: Password Resets ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS password_resets (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    token       TEXT UNIQUE NOT NULL,
    created_at  BIGINT NOT NULL,
    expires_at  BIGINT NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token);


-- ============================================================================
-- CRYPTO DASHBOARD TABLES
-- ============================================================================

CREATE TABLE IF NOT EXISTS crypto_predictions (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL,
    window_start        TEXT NOT NULL,
    pred_direction      TEXT NOT NULL,
    pred_delta          REAL NOT NULL,
    pred_prob           REAL NOT NULL,
    confidence          REAL NOT NULL,
    ensemble_agreement  TEXT,
    model_details       TEXT,
    actual_direction    TEXT,
    actual_delta        REAL,
    was_correct         INTEGER,
    created_at          TIMESTAMPTZ DEFAULT now(),
    resolved_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_crypto_pred_ticker ON crypto_predictions(ticker);
CREATE INDEX IF NOT EXISTS idx_crypto_pred_resolved ON crypto_predictions(was_correct);
CREATE UNIQUE INDEX IF NOT EXISTS idx_crypto_pred_unique ON crypto_predictions(ticker, window_start);

CREATE TABLE IF NOT EXISTS crypto_watchlists (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    name        TEXT DEFAULT 'Default',
    tickers     JSONB NOT NULL DEFAULT '[]',
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crypto_alert_preferences (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    ticker          TEXT NOT NULL,
    min_confidence  REAL DEFAULT 0.6,
    alert_email     INTEGER DEFAULT 1,
    alert_browser   INTEGER DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, ticker)
);

CREATE TABLE IF NOT EXISTS crypto_alert_history (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID REFERENCES profiles(id) ON DELETE SET NULL,
    ticker      TEXT NOT NULL,
    alert_type  TEXT NOT NULL,
    message     TEXT NOT NULL,
    confidence  REAL,
    delivered   INTEGER DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crypto_accuracy_daily (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL,
    date                TEXT NOT NULL,
    total_predictions   INTEGER DEFAULT 0,
    correct_predictions INTEGER DEFAULT 0,
    high_conf_total     INTEGER DEFAULT 0,
    high_conf_correct   INTEGER DEFAULT 0,
    avg_confidence      REAL DEFAULT 0,
    avg_mae             REAL DEFAULT 0,
    UNIQUE(ticker, date)
);

CREATE TABLE IF NOT EXISTS crypto_kalshi_markets (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,
    title           TEXT NOT NULL,
    category        TEXT,
    status          TEXT,
    yes_price       REAL,
    no_price        REAL,
    volume          INTEGER DEFAULT 0,
    last_updated    TIMESTAMPTZ DEFAULT now(),
    data            JSONB
);
CREATE INDEX IF NOT EXISTS idx_crypto_kalshi_ticker ON crypto_kalshi_markets(ticker);


-- ============================================================================
-- SPORTS DASHBOARD TABLES
-- ============================================================================

CREATE TABLE IF NOT EXISTS sports_user_settings (
    user_id                 UUID PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
    default_sport           TEXT DEFAULT 'basketball_nba',
    divergence_threshold    REAL DEFAULT 5.0,
    notifications_enabled   INTEGER DEFAULT 1,
    theme                   TEXT DEFAULT 'dark'
);

CREATE TABLE IF NOT EXISTS sports_user_activity (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID REFERENCES profiles(id) ON DELETE SET NULL,
    action      TEXT NOT NULL,
    detail      TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sports_edge_history (
    id                  BIGSERIAL PRIMARY KEY,
    sport               TEXT,
    home_team           TEXT,
    away_team           TEXT,
    outcome             TEXT,
    sharp_prob          REAL,
    poly_prob           REAL,
    divergence          REAL,
    kelly_pct           REAL,
    confidence_score    REAL,
    detected_at         TIMESTAMPTZ DEFAULT now(),
    resolved            INTEGER DEFAULT 0,
    resolution          TEXT,
    resolved_at         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sports_trades (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID REFERENCES profiles(id) ON DELETE SET NULL,
    market_name TEXT,
    outcome     TEXT,
    entry_price REAL,
    amount      REAL,
    status      TEXT DEFAULT 'open',
    exit_price  REAL,
    pnl         REAL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sports_watchlist (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID REFERENCES profiles(id) ON DELETE CASCADE,
    market_key  TEXT,
    home_team   TEXT,
    away_team   TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, market_key)
);

CREATE TABLE IF NOT EXISTS sports_user_layout (
    user_id                 UUID PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
    visible_widgets         JSONB DEFAULT '["stats","top_opps","hero","events"]',
    visible_data_points     JSONB DEFAULT '["volume","spread","sharp_book","bookmakers","24h_change","match_confidence","kelly","edge","consensus","sharp_prob","poly_prob"]',
    card_expanded_default   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sports_market_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    sport           TEXT NOT NULL,
    event_name      TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    book_prob       REAL,
    poly_prob       REAL,
    kalshi_prob     REAL,
    divergence      REAL,
    poly_volume     REAL,
    kalshi_volume   REAL,
    snapshot_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sports_snap_sport_time ON sports_market_snapshots(sport, snapshot_at);
CREATE INDEX IF NOT EXISTS idx_sports_snap_event ON sports_market_snapshots(event_name, snapshot_at);

CREATE TABLE IF NOT EXISTS sports_historical_markets (
    id              BIGSERIAL PRIMARY KEY,
    sport           TEXT,
    event_title     TEXT NOT NULL,
    market_question TEXT,
    outcome         TEXT,
    final_price     REAL,
    volume          REAL,
    start_date      TEXT,
    end_date        TEXT,
    resolution      TEXT,
    source          TEXT DEFAULT 'polymarket',
    slug            TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(source, slug, outcome)
);
CREATE INDEX IF NOT EXISTS idx_sports_hist_sport ON sports_historical_markets(sport, end_date);


-- ============================================================================
-- MIDTERM DASHBOARD TABLES
-- ============================================================================

CREATE TABLE IF NOT EXISTS midterm_markets (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    event_id        TEXT,
    title           TEXT NOT NULL,
    event_title     TEXT,
    slug            TEXT,
    race_type       TEXT,
    state           TEXT,
    outcomes        JSONB NOT NULL,
    volume          REAL DEFAULT 0,
    liquidity       REAL DEFAULT 0,
    active          INTEGER DEFAULT 1,
    closed          INTEGER DEFAULT 0,
    end_date        TEXT,
    last_updated    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_midterm_markets_source ON midterm_markets(source, source_id);
CREATE INDEX IF NOT EXISTS idx_midterm_markets_race ON midterm_markets(race_type, state);

CREATE TABLE IF NOT EXISTS midterm_price_history (
    id          BIGSERIAL PRIMARY KEY,
    market_id   BIGINT NOT NULL REFERENCES midterm_markets(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    prices      JSONB NOT NULL,
    volume      REAL,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_midterm_price_market ON midterm_price_history(market_id, timestamp);

CREATE TABLE IF NOT EXISTS midterm_polling_data (
    id          BIGSERIAL PRIMARY KEY,
    poll_type   TEXT NOT NULL,
    state       TEXT,
    candidate   TEXT,
    party       TEXT,
    percentage  REAL,
    pollster    TEXT,
    sample_size INTEGER,
    population  TEXT,
    start_date  TEXT,
    end_date    TEXT,
    race_id     TEXT,
    source      TEXT DEFAULT '538',
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_midterm_polling_state ON midterm_polling_data(state, poll_type);

CREATE TABLE IF NOT EXISTS midterm_polling_averages (
    id          BIGSERIAL PRIMARY KEY,
    state       TEXT NOT NULL,
    race_type   TEXT NOT NULL,
    candidate   TEXT NOT NULL,
    party       TEXT,
    average     REAL NOT NULL,
    num_polls   INTEGER,
    period_days INTEGER DEFAULT 30,
    computed_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(state, race_type, candidate, period_days)
);

CREATE TABLE IF NOT EXISTS midterm_divergence_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    race_key            TEXT NOT NULL,
    state               TEXT,
    race_type           TEXT,
    polymarket_prob     REAL,
    kalshi_prob         REAL,
    predictit_prob      REAL,
    polling_avg         REAL,
    max_divergence      REAL,
    divergence_details  JSONB,
    snapshot_time       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_midterm_div_race ON midterm_divergence_snapshots(race_key, snapshot_time);

CREATE TABLE IF NOT EXISTS midterm_user_watchlists (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    race_key    TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, race_key)
);

CREATE TABLE IF NOT EXISTS midterm_alert_settings (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    race_key    TEXT,
    alert_type  TEXT DEFAULT 'divergence',
    threshold   REAL DEFAULT 5.0,
    enabled     INTEGER DEFAULT 1,
    UNIQUE(user_id, race_key, alert_type)
);

CREATE TABLE IF NOT EXISTS midterm_alert_history (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    race_key        TEXT,
    alert_type      TEXT,
    message         TEXT,
    divergence_value REAL,
    delivered       INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS midterm_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID REFERENCES profiles(id) ON DELETE SET NULL,
    action      TEXT NOT NULL,
    details     TEXT,
    ip_address  TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_midterm_audit_user ON midterm_audit_log(user_id, created_at);


-- ============================================================================
-- WEATHER DASHBOARD TABLES
-- ============================================================================

CREATE TABLE IF NOT EXISTS weather_signals_log (
    id          BIGSERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_id   TEXT NOT NULL,
    question    TEXT,
    category    TEXT,
    yes_price   REAL,
    model_prob  REAL,
    edge        REAL,
    action      TEXT
);
CREATE INDEX IF NOT EXISTS idx_weather_signals_market ON weather_signals_log(market_id);
CREATE INDEX IF NOT EXISTS idx_weather_signals_ts ON weather_signals_log(timestamp);

CREATE TABLE IF NOT EXISTS weather_resolutions (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT NOT NULL,
    resolved_at     TIMESTAMPTZ,
    actual_outcome  TEXT,
    payout          REAL
);
CREATE INDEX IF NOT EXISTS idx_weather_res_market ON weather_resolutions(market_id);

CREATE TABLE IF NOT EXISTS weather_alert_settings (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID REFERENCES profiles(id) ON DELETE CASCADE,
    edge_threshold  REAL NOT NULL DEFAULT 0.08,
    categories      JSONB NOT NULL DEFAULT '[]',
    push_enabled    INTEGER NOT NULL DEFAULT 0,
    email           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS weather_user_activity (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    action      TEXT NOT NULL,
    detail      TEXT,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_weather_activity_user ON weather_user_activity(user_id);
CREATE INDEX IF NOT EXISTS idx_weather_activity_ts ON weather_user_activity(timestamp);

CREATE TABLE IF NOT EXISTS weather_price_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_id   TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'polymarket',
    question    TEXT,
    city        TEXT,
    target_date TEXT,
    yes_price   REAL,
    model_prob  REAL,
    edge        REAL,
    volume      REAL
);
CREATE INDEX IF NOT EXISTS idx_weather_snap_market ON weather_price_snapshots(market_id);
CREATE INDEX IF NOT EXISTS idx_weather_snap_ts ON weather_price_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_weather_snap_city ON weather_price_snapshots(city);
CREATE INDEX IF NOT EXISTS idx_weather_snap_market_ts ON weather_price_snapshots(market_id, timestamp);


-- ============================================================================
-- TRADING: API Credentials (encrypted) & Order History
-- ============================================================================

CREATE TABLE IF NOT EXISTS trading_credentials (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,  -- 'polymarket' or 'kalshi'
    cred_data   TEXT NOT NULL,  -- Fernet-encrypted JSON blob
    created_at  BIGINT NOT NULL,
    updated_at  BIGINT NOT NULL,
    UNIQUE(user_id, platform)
);
CREATE INDEX IF NOT EXISTS idx_trading_creds_user ON trading_credentials(user_id);

CREATE TABLE IF NOT EXISTS trading_orders (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    platform        TEXT NOT NULL,
    market_slug     TEXT NOT NULL,
    market_question TEXT,
    side            TEXT NOT NULL,     -- 'yes' or 'no'
    action          TEXT NOT NULL,     -- 'buy' or 'sell'
    amount          REAL NOT NULL,
    price           REAL NOT NULL,
    shares          REAL,
    order_id        TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    fill_price      REAL,
    error           TEXT,
    created_at      BIGINT NOT NULL,
    resolved_at     BIGINT
);
CREATE INDEX IF NOT EXISTS idx_trading_orders_user ON trading_orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_trading_orders_status ON trading_orders(status);


-- ============================================================================
-- ROW LEVEL SECURITY
-- ============================================================================

ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE invite_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE enquiries ENABLE ROW LEVEL SECURITY;
ALTER TABLE password_resets ENABLE ROW LEVEL SECURITY;

-- Profiles: users can read their own, service role can do anything
CREATE POLICY profiles_self_read ON profiles FOR SELECT USING (auth.uid() = id);
CREATE POLICY profiles_service ON profiles FOR ALL USING (true) WITH CHECK (true);

-- Sessions: service role only (server-side management)
CREATE POLICY sessions_service ON sessions FOR ALL USING (true) WITH CHECK (true);

-- Subscriptions: users can read their own
CREATE POLICY subs_self_read ON subscriptions FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY subs_service ON subscriptions FOR ALL USING (true) WITH CHECK (true);

-- Invite tokens: service role only
CREATE POLICY tokens_service ON invite_tokens FOR ALL USING (true) WITH CHECK (true);

-- Enquiries: service role only
CREATE POLICY enquiries_service ON enquiries FOR ALL USING (true) WITH CHECK (true);

-- Password resets: service role only
CREATE POLICY resets_service ON password_resets FOR ALL USING (true) WITH CHECK (true);

-- Dashboard tables: service role manages, users read their own where applicable
ALTER TABLE crypto_watchlists ENABLE ROW LEVEL SECURITY;
CREATE POLICY crypto_wl_self ON crypto_watchlists FOR ALL USING (auth.uid() = user_id);

ALTER TABLE crypto_alert_preferences ENABLE ROW LEVEL SECURITY;
CREATE POLICY crypto_ap_self ON crypto_alert_preferences FOR ALL USING (auth.uid() = user_id);

ALTER TABLE sports_trades ENABLE ROW LEVEL SECURITY;
CREATE POLICY sports_trades_self ON sports_trades FOR ALL USING (auth.uid() = user_id);

ALTER TABLE sports_watchlist ENABLE ROW LEVEL SECURITY;
CREATE POLICY sports_wl_self ON sports_watchlist FOR ALL USING (auth.uid() = user_id);

ALTER TABLE midterm_user_watchlists ENABLE ROW LEVEL SECURITY;
CREATE POLICY midterm_wl_self ON midterm_user_watchlists FOR ALL USING (auth.uid() = user_id);

ALTER TABLE midterm_alert_settings ENABLE ROW LEVEL SECURITY;
CREATE POLICY midterm_as_self ON midterm_alert_settings FOR ALL USING (auth.uid() = user_id);

ALTER TABLE trading_credentials ENABLE ROW LEVEL SECURITY;
CREATE POLICY trading_creds_service ON trading_credentials FOR ALL USING (true) WITH CHECK (true);

ALTER TABLE trading_orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY trading_orders_self ON trading_orders FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY trading_orders_service ON trading_orders FOR ALL USING (true) WITH CHECK (true);
