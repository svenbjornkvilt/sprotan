#!/usr/bin/env python3
"""Import scraped Faroese dictionary JSON into SQLite with FTS5 indexes."""

import json
import os
import re
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORDS_JSON = os.path.join(BASE_DIR, "data", "processed", "words.json")
EN_FO_JSON = os.path.join(BASE_DIR, "data", "processed_en_fo", "translations.json")
FO_EN_JSON = os.path.join(BASE_DIR, "data", "processed_fo_en", "translations.json")
DB_PATH = os.path.join(BASE_DIR, "db", "sprotin.db")


def create_schema(cur):
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS words (
            id              INTEGER PRIMARY KEY,
            search_word     TEXT NOT NULL,
            display_word    TEXT NOT NULL,
            word_class_code TEXT,
            inflection_class TEXT,
            grammar_raw     TEXT,
            short_inflected_form TEXT,
            explanation_text TEXT,
            phonetic        TEXT,
            origin          TEXT,
            origin_source   TEXT,
            date_added      TEXT
        );

        CREATE TABLE IF NOT EXISTS inflected_forms (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id         INTEGER NOT NULL REFERENCES words(id),
            form_index      INTEGER NOT NULL,
            form            TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS word_references (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id         INTEGER NOT NULL REFERENCES words(id),
            referenced_word TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_words_search ON words(search_word);
        CREATE INDEX IF NOT EXISTS idx_words_search_lower ON words(search_word COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_words_class ON words(word_class_code);
        CREATE INDEX IF NOT EXISTS idx_inflected_word ON inflected_forms(word_id);
        CREATE INDEX IF NOT EXISTS idx_inflected_form ON inflected_forms(form);
        CREATE INDEX IF NOT EXISTS idx_inflected_form_lower ON inflected_forms(form COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_references_word ON word_references(word_id);

        CREATE TABLE IF NOT EXISTS translations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_word     TEXT NOT NULL,
            source_lang     TEXT NOT NULL DEFAULT 'en',
            target_word_id  INTEGER REFERENCES words(id),
            target_word     TEXT NOT NULL,
            explanation     TEXT,
            dictionary_id   INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_translations_source ON translations(source_word COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_translations_target ON translations(target_word COLLATE NOCASE);

        CREATE TABLE IF NOT EXISTS word_embeddings (
            word_id     INTEGER PRIMARY KEY REFERENCES words(id),
            embedding   BLOB NOT NULL
        );
    """)

    # FTS5 for full-text search on definitions
    cur.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS words_fts USING fts5(
            search_word, explanation_text, content=words, content_rowid=id
        )
    """)

    # FTS5 for inflected form search
    cur.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS inflections_fts USING fts5(
            form, content=inflected_forms, content_rowid=id
        )
    """)


def import_words(cur, words):
    word_rows = []
    inflection_rows = []
    reference_rows = []

    for w in words:
        grammar = w.get("grammar", {})
        explanation = w.get("explanation", {})

        word_rows.append((
            w["id"],
            w.get("search_word", ""),
            w.get("display_word", ""),
            grammar.get("word_class_code"),
            grammar.get("inflection_class"),
            grammar.get("raw_text", ""),
            w.get("short_inflected_form", ""),
            explanation.get("text", ""),
            w.get("phonetic"),
            w.get("origin", ""),
            w.get("origin_source", ""),
            w.get("date"),
        ))

        for idx, form in enumerate(w.get("inflected_forms", [])):
            inflection_rows.append((w["id"], idx, form))

        for ref in explanation.get("references", []):
            reference_rows.append((w["id"], ref))

    cur.executemany(
        "INSERT OR REPLACE INTO words VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        word_rows
    )
    cur.executemany(
        "INSERT INTO inflected_forms (word_id, form_index, form) VALUES (?,?,?)",
        inflection_rows
    )
    cur.executemany(
        "INSERT INTO word_references (word_id, referenced_word) VALUES (?,?)",
        reference_rows
    )

    return len(word_rows), len(inflection_rows), len(reference_rows)


def rebuild_fts(cur):
    cur.execute("INSERT INTO words_fts(words_fts) VALUES('rebuild')")
    cur.execute("INSERT INTO inflections_fts(inflections_fts) VALUES('rebuild')")


def import_en_fo(cur, translations):
    """Import EN-FO translation entries, linking to existing FO words where possible."""
    rows = []
    for t in translations:
        source = t.get("source_word", "")
        target_text = t.get("translation_text", "")
        if not source or not target_text:
            continue

        # Try to link to an existing Faroese headword
        # Extract first word of translation as likely headword
        first_word = target_text.split(",")[0].split(";")[0].strip()
        # Remove leading numbers like "1 " or "1. "
        first_word = re.sub(r"^\d+\.?\s*", "", first_word).strip()

        target_word_id = None
        if first_word:
            match = cur.execute(
                "SELECT id FROM words WHERE search_word = ? COLLATE NOCASE LIMIT 1",
                (first_word,),
            ).fetchone()
            if match:
                target_word_id = match[0]

        rows.append((
            source,
            "en",
            target_word_id,
            target_text,
            None,  # explanation
            3,  # EN_FO_DICTIONARY_ID
        ))

    cur.executemany(
        "INSERT INTO translations (source_word, source_lang, target_word_id, target_word, explanation, dictionary_id) VALUES (?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def import_fo_en(cur, translations):
    """Import FO-EN translation entries, linking to existing FO words where possible."""
    rows = []
    for t in translations:
        source = t.get("source_word", "")
        target_text = t.get("translation_text", "")
        if not source or not target_text:
            continue

        # Try to link to an existing Faroese headword
        target_word_id = None
        match = cur.execute(
            "SELECT id FROM words WHERE search_word = ? COLLATE NOCASE LIMIT 1",
            (source,),
        ).fetchone()
        if match:
            target_word_id = match[0]

        rows.append((
            source,
            "fo",
            target_word_id,
            target_text,
            None,  # explanation
            2,  # FO_EN_DICTIONARY_ID
        ))

    cur.executemany(
        "INSERT INTO translations (source_word, source_lang, target_word_id, target_word, explanation, dictionary_id) VALUES (?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def generate_embeddings(cur):
    """Generate semantic embeddings for all words using sentence-transformers."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print("sentence-transformers not installed. Skipping embedding generation.")
        print("Install with: pip install sentence-transformers")
        return 0

    import numpy as np

    print("Loading embedding model (paraphrase-multilingual-MiniLM-L12-v2)...")
    model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

    # Get all words with definitions
    words = cur.execute(
        "SELECT id, search_word, explanation_text FROM words WHERE explanation_text IS NOT NULL AND explanation_text != ''"
    ).fetchall()
    print(f"Generating embeddings for {len(words)} words...")

    batch_size = 256
    total = 0
    for i in range(0, len(words), batch_size):
        batch = words[i:i + batch_size]
        texts = [f"{row[1]}: {row[2]}" for row in batch]
        embeddings = model.encode(texts, show_progress_bar=False)

        rows = [(row[0], embeddings[j].astype(np.float32).tobytes()) for j, row in enumerate(batch)]
        cur.executemany("INSERT OR REPLACE INTO word_embeddings (word_id, embedding) VALUES (?,?)", rows)
        total += len(batch)

        if total % 5000 == 0 or total == len(words):
            print(f"  {total}/{len(words)} embeddings generated")

    return total


def main():
    print(f"Loading {WORDS_JSON}...")
    with open(WORDS_JSON, "r", encoding="utf-8") as f:
        words = json.load(f)
    print(f"Loaded {len(words)} words")

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    # Remove old DB for clean import
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Performance settings for bulk import
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA cache_size=-64000")  # 64MB cache

    print("Creating schema...")
    create_schema(cur)

    print("Importing words...")
    n_words, n_inflections, n_refs = import_words(cur, words)
    print(f"  {n_words} words, {n_inflections} inflections, {n_refs} references")

    print("Building FTS indexes...")
    rebuild_fts(cur)

    conn.commit()

    # Import EN-FO translations if available
    if os.path.exists(EN_FO_JSON):
        print(f"Loading EN-FO translations from {EN_FO_JSON}...")
        with open(EN_FO_JSON, "r", encoding="utf-8") as f:
            translations = json.load(f)
        print(f"Loaded {len(translations)} EN-FO entries")
        n_trans = import_en_fo(cur, translations)
        print(f"  {n_trans} translations imported")
        conn.commit()
    else:
        print(f"No EN-FO data found at {EN_FO_JSON}, skipping translations.")

    # Import FO-EN translations if available
    if os.path.exists(FO_EN_JSON):
        print(f"Loading FO-EN translations from {FO_EN_JSON}...")
        with open(FO_EN_JSON, "r", encoding="utf-8") as f:
            translations = json.load(f)
        print(f"Loaded {len(translations)} FO-EN entries")
        n_trans = import_fo_en(cur, translations)
        print(f"  {n_trans} translations imported")
        conn.commit()
    else:
        print(f"No FO-EN data found at {FO_EN_JSON}, skipping.")

    # Generate embeddings if sentence-transformers is available
    if "--embeddings" in sys.argv:
        n_emb = generate_embeddings(cur)
        if n_emb:
            print(f"  {n_emb} embeddings generated")
        conn.commit()

    # Optimize
    print("Optimizing database...")
    cur.execute("ANALYZE")
    cur.execute("INSERT INTO words_fts(words_fts) VALUES('optimize')")
    cur.execute("INSERT INTO inflections_fts(inflections_fts) VALUES('optimize')")
    conn.commit()
    cur.execute("VACUUM")
    conn.commit()
    conn.close()

    size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
    print(f"\nDone! Database: {DB_PATH} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
