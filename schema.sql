-- Schema for importing scraped Sprotin.fo Faroese dictionary data
-- Compatible with SQLite, PostgreSQL, MySQL (adjust types as needed)

CREATE TABLE IF NOT EXISTS words (
    id              INTEGER PRIMARY KEY,       -- Original sprotin.fo word ID
    search_word     TEXT NOT NULL,              -- Headword for searching
    display_word    TEXT NOT NULL,              -- Display form of the word
    word_class_code TEXT,                       -- e.g. 'k' (noun), 'kvk' (fem noun), 'so' (verb), 'lh' (adjective)
    inflection_class TEXT,                      -- Inflection paradigm number/code
    grammar_raw     TEXT,                       -- Full grammar annotation text
    short_inflected_form TEXT,                  -- Short inflection summary (e.g. "-s -ar")
    explanation_text TEXT,                      -- Plain text definition/explanation
    phonetic        TEXT,                       -- Phonetic transcription (if available)
    origin          TEXT,                       -- Etymology
    origin_source   TEXT,                       -- Etymology source
    date_added      TEXT                        -- Date the entry was added/updated
);

CREATE TABLE IF NOT EXISTS inflected_forms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id         INTEGER NOT NULL REFERENCES words(id),
    form_index      INTEGER NOT NULL,           -- Position in inflection paradigm
    form            TEXT NOT NULL                -- The inflected form
);

CREATE TABLE IF NOT EXISTS word_references (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id         INTEGER NOT NULL REFERENCES words(id),
    referenced_word TEXT NOT NULL                -- Cross-referenced word from explanation
);

CREATE TABLE IF NOT EXISTS word_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id         INTEGER NOT NULL REFERENCES words(id),
    group_name      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS translations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_word     TEXT NOT NULL,              -- English word
    source_lang     TEXT NOT NULL DEFAULT 'en', -- Source language code
    target_word_id  INTEGER REFERENCES words(id),  -- Link to FO word if match found
    target_word     TEXT NOT NULL,              -- Faroese translation text
    explanation     TEXT,                       -- Translation context/notes
    dictionary_id   INTEGER                    -- Source dictionary ID
);

CREATE TABLE IF NOT EXISTS word_embeddings (
    word_id     INTEGER PRIMARY KEY REFERENCES words(id),
    embedding   BLOB NOT NULL  -- 384-dim float32 vector, stored as raw bytes
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_words_search ON words(search_word);
CREATE INDEX IF NOT EXISTS idx_words_class ON words(word_class_code);
CREATE INDEX IF NOT EXISTS idx_inflected_word ON inflected_forms(word_id);
CREATE INDEX IF NOT EXISTS idx_inflected_form ON inflected_forms(form);
CREATE INDEX IF NOT EXISTS idx_references_word ON word_references(word_id);
CREATE INDEX IF NOT EXISTS idx_translations_source ON translations(source_word COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_translations_target ON translations(target_word COLLATE NOCASE);
