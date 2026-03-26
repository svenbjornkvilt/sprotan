#!/usr/bin/env python3
"""
Scrape all Faroese words and grammar from sprotin.fo (FØ-FØ dictionary).
Dictionary ID 1 = Føroyskt-Føroyskt (Faroese-Faroese), ~67,487 words.

Outputs structured JSON files ready for database indexing.
Saves progress incrementally so you can see data appearing as it scrapes.
"""

import json
import os
import re
import time
import urllib.request
import urllib.parse
import sys
from html.parser import HTMLParser

BASE_URL = "https://sprotin.fo/dictionary_search_json.php"
RESULTS_PER_PAGE = 100
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RAW_DIR = os.path.join(OUTPUT_DIR, "raw")
PROCESSED_DIR = os.path.join(OUTPUT_DIR, "processed")

# Dictionary IDs on sprotin.fo
FO_FO_DICTIONARY_ID = 1   # Føroysk-Føroysk (66,813 words)
FO_EN_DICTIONARY_ID = 2   # Føroyskt-Enskt (80,178 words)
EN_FO_DICTIONARY_ID = 3   # Enskt-Føroyskt (78,897 words)

# Faroese lowercase alphabet + special prefix. The API is case-insensitive
# so we skip uppercase duplicates.
FO_SEARCH_PREFIXES = ["-"] + list("aábdðefghiíjklmnoóprstúuvyýæø") + list("0123456789")
EN_FO_SEARCH_PREFIXES = list("abcdefghijklmnopqrstuvwxyz")

# EN-FO output paths
EN_FO_RAW_DIR = os.path.join(OUTPUT_DIR, "raw_en_fo")
EN_FO_PROCESSED_DIR = os.path.join(OUTPUT_DIR, "processed_en_fo")

# FO-EN output paths
FO_EN_RAW_DIR = os.path.join(OUTPUT_DIR, "raw_fo_en")
FO_EN_PROCESSED_DIR = os.path.join(OUTPUT_DIR, "processed_fo_en")

# Delay between API requests (seconds)
REQUEST_DELAY = 0.15


def log(msg):
    print(msg, flush=True)


class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def get_text(self):
        return "".join(self.parts).strip()


def strip_html(html_str):
    if not html_str:
        return ""
    extractor = HTMLTextExtractor()
    extractor.feed(html_str)
    return extractor.get_text()


def parse_grammar_comment(html_str):
    if not html_str:
        return {}
    result = {}
    k_match = re.search(r'class="_k"[^>]*>([^<]+)', html_str)
    if k_match:
        result["word_class_code"] = k_match.group(1).strip()
    c_match = re.search(r'class="_c"[^>]*>([^<]+)', html_str)
    if c_match:
        result["inflection_class"] = c_match.group(1).strip()
    result["raw_text"] = strip_html(html_str)
    return result


def parse_explanation(html_str):
    if not html_str:
        return {"text": "", "references": []}
    refs = []
    for match in re.finditer(r'class="word_link"[^>]*>([^<]+)', html_str):
        refs.append(match.group(1).strip())
    return {"text": strip_html(html_str), "references": refs}


def clean_word_entry(raw):
    grammar = parse_grammar_comment(raw.get("GrammarComment"))
    explanation = parse_explanation(raw.get("Explanation"))
    return {
        "id": raw["Id"],
        "search_word": raw.get("SearchWord", ""),
        "display_word": raw.get("DisplayWord", ""),
        "grammar": grammar,
        "short_inflected_form": strip_html(raw.get("ShortInflectedForm")),
        "inflected_forms": raw.get("InflectedForm") or [],
        "explanation": explanation,
        "phonetic": raw.get("Phonetic"),
        "origin": strip_html(raw.get("Origin")),
        "origin_source": strip_html(raw.get("OriginSource")),
        "groups": raw.get("Groups", []),
        "date": raw.get("Date"),
    }


def fetch_page(prefix, page, dictionary_id=FO_FO_DICTIONARY_ID):
    params = urllib.parse.urlencode({
        "DictionaryId": dictionary_id,
        "DictionaryPage": page,
        "SearchFor": prefix,
        "SearchInflections": 0,
        "SearchDescriptions": 0,
        "Group": "",
        "SkipOtherDictionariesResults": 1,
        "SkipSimilarWords": 1,
        "_l": "fo",
    })
    url = f"{BASE_URL}?{params}"

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt < 2:
                log(f"  Retry {attempt+1} for '{prefix}' p{page}: {e}")
                time.sleep(2 * (attempt + 1))
            else:
                log(f"  FAILED '{prefix}' p{page}: {e}")
                return None


def scrape_prefix(prefix, seen_ids, dictionary_id=FO_FO_DICTIONARY_ID):
    """Scrape all pages for a prefix. Returns new unique raw word entries."""
    new_words = []
    page = 1

    data = fetch_page(prefix, page, dictionary_id)
    if not data or data.get("status") != "success" or data.get("total", 0) == 0:
        return new_words, data

    total = data["total"]
    total_pages = (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE
    log(f"  '{prefix}': {total} words across {total_pages} pages")

    # Collect words from first page
    for w in data.get("words", []):
        if w["Id"] not in seen_ids:
            seen_ids.add(w["Id"])
            new_words.append(w)

    # Fetch remaining pages
    for page in range(2, total_pages + 1):
        time.sleep(REQUEST_DELAY)
        pdata = fetch_page(prefix, page, dictionary_id)
        if pdata and pdata.get("status") == "success":
            for w in pdata.get("words", []):
                if w["Id"] not in seen_ids:
                    seen_ids.add(w["Id"])
                    new_words.append(w)
        else:
            log(f"  Warning: failed page {page}/{total_pages} for '{prefix}'")

    return new_words, data


def clean_en_fo_entry(raw):
    """Clean an EN-FO dictionary entry."""
    explanation = parse_explanation(raw.get("Explanation"))
    return {
        "id": raw["Id"],
        "source_word": raw.get("SearchWord", ""),
        "display_word": raw.get("DisplayWord", ""),
        "translation_html": raw.get("Explanation", ""),
        "translation_text": explanation["text"],
        "references": explanation["references"],
        "groups": raw.get("Groups", []),
        "date": raw.get("Date"),
    }


def scrape_en_fo():
    """Scrape EN-FO dictionary from sprotin.fo."""
    os.makedirs(EN_FO_RAW_DIR, exist_ok=True)
    os.makedirs(EN_FO_PROCESSED_DIR, exist_ok=True)

    progress_file = os.path.join(OUTPUT_DIR, "progress_en_fo.json")
    all_words_raw = {}
    seen_ids = set()
    completed_prefixes = set()

    if os.path.exists(progress_file):
        with open(progress_file, "r", encoding="utf-8") as f:
            progress = json.load(f)
            completed_prefixes = set(progress.get("completed_prefixes", []))
        raw_path = os.path.join(EN_FO_RAW_DIR, "all_words_raw.json")
        if os.path.exists(raw_path):
            with open(raw_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
                for w in existing:
                    all_words_raw[w["Id"]] = w
                    seen_ids.add(w["Id"])
            log(f"Resumed: {len(seen_ids)} words from {len(completed_prefixes)} prefixes")

    prefixes = EN_FO_SEARCH_PREFIXES
    log(f"\nScraping EN-FO dictionary ({len(prefixes)} prefixes, {len(completed_prefixes)} done)...\n")

    metadata_saved = False

    for i, prefix in enumerate(prefixes):
        if prefix in completed_prefixes:
            log(f"[{i+1}/{len(prefixes)}] '{prefix}' — skipped (already done)")
            continue

        log(f"[{i+1}/{len(prefixes)}] Prefix '{prefix}'...")
        new_words, data = scrape_prefix(prefix, seen_ids, EN_FO_DICTIONARY_ID)

        if not metadata_saved and data and "dictionary" in data:
            meta = data["dictionary"]
            meta_path = os.path.join(OUTPUT_DIR, "dictionary_metadata_en_fo.json")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            log(f"  Saved metadata ({meta.get('TotalWords', '?')} total words)")
            metadata_saved = True

        for w in new_words:
            all_words_raw[w["Id"]] = w

        log(f"  +{len(new_words)} new (total unique: {len(seen_ids)})")
        completed_prefixes.add(prefix)

        raw_path = os.path.join(EN_FO_RAW_DIR, "all_words_raw.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(list(all_words_raw.values()), f, ensure_ascii=False)

        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump({"completed_prefixes": list(completed_prefixes), "total_words": len(seen_ids)}, f)

        time.sleep(REQUEST_DELAY)

    log(f"\n=== EN-FO scraping complete: {len(all_words_raw)} unique entries ===\n")
    log("Processing EN-FO data...")

    processed = [clean_en_fo_entry(raw) for raw in all_words_raw.values()]
    processed.sort(key=lambda w: w["source_word"].lower())

    processed_path = os.path.join(EN_FO_PROCESSED_DIR, "translations.json")
    with open(processed_path, "w", encoding="utf-8") as f:
        json.dump(processed, f, ensure_ascii=False, indent=2)
    log(f"Saved: {processed_path} ({len(processed)} entries)")

    if os.path.exists(progress_file):
        os.remove(progress_file)
    log("EN-FO scraping done!")


def clean_fo_en_entry(raw):
    """Clean a FO-EN dictionary entry."""
    explanation = parse_explanation(raw.get("Explanation"))
    return {
        "id": raw["Id"],
        "source_word": raw.get("SearchWord", ""),
        "display_word": raw.get("DisplayWord", ""),
        "translation_html": raw.get("Explanation", ""),
        "translation_text": explanation["text"],
        "references": explanation["references"],
        "groups": raw.get("Groups", []),
        "date": raw.get("Date"),
    }


def scrape_fo_en():
    """Scrape FO-EN dictionary (Føroyskt-Enskt) from sprotin.fo."""
    os.makedirs(FO_EN_RAW_DIR, exist_ok=True)
    os.makedirs(FO_EN_PROCESSED_DIR, exist_ok=True)

    progress_file = os.path.join(OUTPUT_DIR, "progress_fo_en.json")
    all_words_raw = {}
    seen_ids = set()
    completed_prefixes = set()

    if os.path.exists(progress_file):
        with open(progress_file, "r", encoding="utf-8") as f:
            progress = json.load(f)
            completed_prefixes = set(progress.get("completed_prefixes", []))
        raw_path = os.path.join(FO_EN_RAW_DIR, "all_words_raw.json")
        if os.path.exists(raw_path):
            with open(raw_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
                for w in existing:
                    all_words_raw[w["Id"]] = w
                    seen_ids.add(w["Id"])
            log(f"Resumed: {len(seen_ids)} words from {len(completed_prefixes)} prefixes")

    # FO-EN uses Faroese prefixes (same alphabet as FO-FO)
    prefixes = FO_SEARCH_PREFIXES
    log(f"\nScraping FO-EN dictionary ({len(prefixes)} prefixes, {len(completed_prefixes)} done)...\n")

    metadata_saved = False

    for i, prefix in enumerate(prefixes):
        if prefix in completed_prefixes:
            log(f"[{i+1}/{len(prefixes)}] '{prefix}' — skipped (already done)")
            continue

        log(f"[{i+1}/{len(prefixes)}] Prefix '{prefix}'...")
        new_words, data = scrape_prefix(prefix, seen_ids, FO_EN_DICTIONARY_ID)

        if not metadata_saved and data and "dictionary" in data:
            meta = data["dictionary"]
            meta_path = os.path.join(OUTPUT_DIR, "dictionary_metadata_fo_en.json")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            log(f"  Saved metadata ({meta.get('TotalWords', '?')} total words)")
            metadata_saved = True

        for w in new_words:
            all_words_raw[w["Id"]] = w

        log(f"  +{len(new_words)} new (total unique: {len(seen_ids)})")
        completed_prefixes.add(prefix)

        raw_path = os.path.join(FO_EN_RAW_DIR, "all_words_raw.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(list(all_words_raw.values()), f, ensure_ascii=False)

        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump({"completed_prefixes": list(completed_prefixes), "total_words": len(seen_ids)}, f)

        time.sleep(REQUEST_DELAY)

    log(f"\n=== FO-EN scraping complete: {len(all_words_raw)} unique entries ===\n")
    log("Processing FO-EN data...")

    processed = [clean_fo_en_entry(raw) for raw in all_words_raw.values()]
    processed.sort(key=lambda w: w["source_word"].lower())

    processed_path = os.path.join(FO_EN_PROCESSED_DIR, "translations.json")
    with open(processed_path, "w", encoding="utf-8") as f:
        json.dump(processed, f, ensure_ascii=False, indent=2)
    log(f"Saved: {processed_path} ({len(processed)} entries)")

    if os.path.exists(progress_file):
        os.remove(progress_file)
    log("FO-EN scraping done!")


def scrape_fo_fo():
    """Scrape FO-FO dictionary (original behavior)."""
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    # Check for existing progress
    progress_file = os.path.join(OUTPUT_DIR, "progress.json")
    all_words_raw = {}
    seen_ids = set()
    completed_prefixes = set()

    if os.path.exists(progress_file):
        with open(progress_file, "r", encoding="utf-8") as f:
            progress = json.load(f)
            completed_prefixes = set(progress.get("completed_prefixes", []))
        # Load existing raw data
        raw_path = os.path.join(RAW_DIR, "all_words_raw.json")
        if os.path.exists(raw_path):
            with open(raw_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
                for w in existing:
                    all_words_raw[w["Id"]] = w
                    seen_ids.add(w["Id"])
            log(f"Resumed: {len(seen_ids)} words from {len(completed_prefixes)} prefixes")

    log(f"\nScraping {len(FO_SEARCH_PREFIXES)} prefixes ({len(completed_prefixes)} already done)...\n")

    metadata_saved = False

    for i, prefix in enumerate(FO_SEARCH_PREFIXES):
        if prefix in completed_prefixes:
            log(f"[{i+1}/{len(FO_SEARCH_PREFIXES)}] '{prefix}' — skipped (already done)")
            continue

        log(f"[{i+1}/{len(FO_SEARCH_PREFIXES)}] Prefix '{prefix}'...")
        new_words, data = scrape_prefix(prefix, seen_ids)

        # Save metadata from first successful response
        if not metadata_saved and data and "dictionary" in data:
            meta = data["dictionary"]
            meta_path = os.path.join(OUTPUT_DIR, "dictionary_metadata.json")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            log(f"  Saved metadata ({meta.get('TotalWords', '?')} total words in dictionary)")
            metadata_saved = True

        for w in new_words:
            all_words_raw[w["Id"]] = w

        log(f"  +{len(new_words)} new (total unique: {len(seen_ids)})")

        completed_prefixes.add(prefix)

        # Save progress incrementally every prefix
        raw_path = os.path.join(RAW_DIR, "all_words_raw.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(list(all_words_raw.values()), f, ensure_ascii=False)

        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump({"completed_prefixes": list(completed_prefixes), "total_words": len(seen_ids)}, f)

        time.sleep(REQUEST_DELAY)

    log(f"\n=== Scraping complete: {len(all_words_raw)} unique words ===\n")
    log("Processing and structuring data...")

    # Process all words
    processed_words = [clean_word_entry(raw) for raw in all_words_raw.values()]
    processed_words.sort(key=lambda w: w["search_word"].lower())

    # Save full processed dataset
    processed_path = os.path.join(PROCESSED_DIR, "words.json")
    with open(processed_path, "w", encoding="utf-8") as f:
        json.dump(processed_words, f, ensure_ascii=False, indent=2)
    log(f"Saved: {processed_path} ({len(processed_words)} words)")

    # Per-letter files
    by_letter = {}
    for w in processed_words:
        letter = w["search_word"][0].lower() if w["search_word"] else "_"
        by_letter.setdefault(letter, []).append(w)

    for letter, words in by_letter.items():
        safe_name = urllib.parse.quote(letter, safe="")
        letter_path = os.path.join(PROCESSED_DIR, f"words_{safe_name}.json")
        with open(letter_path, "w", encoding="utf-8") as f:
            json.dump(words, f, ensure_ascii=False, indent=2)
    log(f"Saved {len(by_letter)} per-letter files")

    # Word index
    index = [{"id": w["id"], "word": w["search_word"], "grammar": w["grammar"].get("raw_text", "")}
             for w in processed_words]
    index_path = os.path.join(PROCESSED_DIR, "word_index.json")
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    log(f"Saved word index: {index_path}")

    # Inflection map
    inflection_map = {}
    for w in processed_words:
        for form in w["inflected_forms"]:
            inflection_map.setdefault(form, []).append({
                "id": w["id"],
                "search_word": w["search_word"],
            })
    inflection_path = os.path.join(PROCESSED_DIR, "inflection_map.json")
    with open(inflection_path, "w", encoding="utf-8") as f:
        json.dump(inflection_map, f, ensure_ascii=False, indent=2)
    log(f"Saved inflection map: {len(inflection_map)} inflected forms")

    # Stats
    stats = {
        "total_words": len(processed_words),
        "total_inflected_forms": len(inflection_map),
        "words_with_inflections": sum(1 for w in processed_words if w["inflected_forms"]),
        "words_by_letter": {k: len(v) for k, v in sorted(by_letter.items())},
        "grammar_classes": {},
    }
    for w in processed_words:
        gc = w["grammar"].get("word_class_code", "unknown")
        stats["grammar_classes"][gc] = stats["grammar_classes"].get(gc, 0) + 1
    stats_path = os.path.join(OUTPUT_DIR, "stats.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    log(f"Saved stats: {stats_path}")

    # Clean up progress file
    os.remove(progress_file)
    log("\nDone!")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "en-fo":
        scrape_en_fo()
    elif len(sys.argv) > 1 and sys.argv[1] == "fo-en":
        scrape_fo_en()
    else:
        scrape_fo_fo()


if __name__ == "__main__":
    main()
