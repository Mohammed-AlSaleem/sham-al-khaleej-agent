-- Long-term memory schema (SQLite)
-- Design principles:
--   1. Memory is structured facts, not raw chat logs.
--   2. Facts are never overwritten: a change creates a new row and marks the old
--      one 'superseded', so history stays auditable.
--   3. Hard constraints (allergies, diets) are separated from soft tastes so the
--      agent can never trade a safety constraint off against a preference.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,              -- uuid
    username      TEXT NOT NULL UNIQUE,          -- how returning users identify themselves
    display_name  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at    TEXT,
    summary     TEXT                              -- optional short recap, written when the session ends
);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, started_at);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('user','assistant','tool')),
    content          TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id);

-- The core of long-term memory.
CREATE TABLE IF NOT EXISTS preferences (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- What kind of fact this is.
    kind               TEXT NOT NULL CHECK (kind IN (
                           'diet',            -- vegetarian, vegan, halal-only, ...
                           'allergen',        -- must match menu allergen_vocabulary
                           'ingredient',      -- likes / dislikes a specific ingredient
                           'dish',            -- likes / dislikes a specific menu item (subject = item id)
                           'category',        -- prefers starters, skips desserts, ...
                           'spice',           -- tolerance; value_json = {"max_level": 2}
                           'budget',          -- value_json = {"max_item_price": 40} or {"max_total": 120}
                           'other'            -- free-form (e.g. "eating with kids")
                       )),
    subject            TEXT NOT NULL,         -- normalized key: 'vegetarian', 'shellfish', 'mushroom', 'mn-kabsa'
    polarity           TEXT NOT NULL CHECK (polarity IN ('like','avoid','limit')),
    strength           TEXT NOT NULL DEFAULT 'soft' CHECK (strength IN ('hard','soft')),
                                               -- hard = never violate (allergy, diet); soft = tune ranking
    value_json         TEXT,                  -- structured payload for spice/budget; NULL otherwise
    note               TEXT,                  -- original wording from the user, for traceability

    -- Lifecycle: facts evolve, so we never UPDATE in place.
    status             TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active','superseded','retracted')),
    supersedes_id      INTEGER REFERENCES preferences(id),   -- the row this one replaces
    confidence         REAL NOT NULL DEFAULT 0.8 CHECK (confidence BETWEEN 0 AND 1),

    -- Provenance.
    source_message_id  INTEGER REFERENCES messages(id),
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    last_confirmed_at  TEXT,                  -- bumped when the user re-affirms or the fact is used successfully
    superseded_at      TEXT
);

-- Fast lookup of the live memory for one user.
CREATE INDEX IF NOT EXISTS idx_pref_active ON preferences(user_id, status, kind);

-- At most one active fact per (user, kind, subject): a new statement must supersede the old one.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pref_active_subject
    ON preferences(user_id, kind, subject) WHERE status = 'active';

-- Past orders ("previous interactions") so recommendations can use real behavior,
-- not only what the user said.
CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id  TEXT REFERENCES conversations(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    total_price      REAL
);

CREATE TABLE IF NOT EXISTS order_items (
    order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    item_id    TEXT NOT NULL,                 -- references menu.json item id
    quantity   INTEGER NOT NULL DEFAULT 1,
    quantity_for_others INTEGER NOT NULL DEFAULT 0,   -- of `quantity`, how many are for other people (not the guest)
    PRIMARY KEY (order_id, item_id)
);

-- ---------------------------------------------------------------------------
-- Example: user says "I'm vegetarian", later "actually I eat fish now".
--   INSERT diet/vegetarian  -> id 1, status 'active'
--   Later:  UPDATE id 1 SET status='superseded', superseded_at=datetime('now');
--           INSERT diet/pescatarian, supersedes_id=1, status 'active'
-- Active memory query for the agent prompt / tools:
--   SELECT kind, subject, polarity, strength, value_json, note
--   FROM preferences WHERE user_id = ? AND status = 'active'
--   ORDER BY strength = 'hard' DESC, kind;

-- ---------------------------------------------------------------------------
-- Pairings ("combos"): append-only event log of what the guest SAID about a pair.
-- Observed pairings are NOT stored here; they are derived from orders/order_items
-- so they always reflect real order history and can decay with time.
-- Pair is stored with item_a < item_b (unordered). Latest event wins.
CREATE TABLE IF NOT EXISTS combo_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_a             TEXT NOT NULL,
    item_b             TEXT NOT NULL,
    stance             TEXT NOT NULL CHECK (stance IN ('like','drop')),
    source_message_id  INTEGER REFERENCES messages(id),
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (item_a < item_b)
);
CREATE INDEX IF NOT EXISTS idx_combo_user ON combo_events(user_id, item_a, item_b, id);

-- ---------------------------------------------------------------------------
-- Customisations: ingredients left out of an ordered item ("lemonade without sugar").
-- One row per removed ingredient per order line; quantity = units prepared that way.
-- order_items still holds the per-item totals (used for habits and history).
CREATE TABLE IF NOT EXISTS order_modifications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id            INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    item_id             TEXT NOT NULL,
    removed_ingredient  TEXT NOT NULL,
    quantity            INTEGER NOT NULL DEFAULT 1
);

-- ---------------------------------------------------------------------------
-- The order the guest was last quoted in a conversation. Chat history keeps only message text, not tool
-- arguments, so "yes, confirm" would otherwise force the model to rebuild the order (and any customisations)
-- from memory. Confirming records exactly what was quoted.
CREATE TABLE IF NOT EXISTS pending_quotes (
    conversation_id  TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    items_json       TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
