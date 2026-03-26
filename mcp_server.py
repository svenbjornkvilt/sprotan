#!/usr/bin/env python3
"""
Faroese Dictionary MCP Server v2 — Docker-ready, HFST-powered.
6 tools: generate_form, verify_text, lookup_word, translate_text,
check_register, grammar_reference.
"""

import contextlib
import json as _json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sprotin-mcp")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "sprotin.db")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TOOLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "giellalt")

# Also check Docker paths
if not os.path.isdir(TOOLS_DIR) and os.path.isdir("/app/tools/giellalt"):
    TOOLS_DIR = "/app/tools/giellalt"
if not os.path.exists(DB_PATH) and os.path.exists("/app/db/sprotin.db"):
    DB_PATH = "/app/db/sprotin.db"
if not os.path.isdir(DATA_DIR) and os.path.isdir("/app/data"):
    DATA_DIR = "/app/data"

# Find HFST transducers
_ANALYSER = None
_GENERATOR = None
if os.path.isdir(TOOLS_DIR):
    for f in os.listdir(TOOLS_DIR):
        if "analyser" in f and "desc" in f and f.endswith(".hfstol"):
            _ANALYSER = os.path.join(TOOLS_DIR, f)
        if "generator" in f and "norm" in f and f.endswith(".hfstol"):
            _GENERATOR = os.path.join(TOOLS_DIR, f)

log.info(f"Analyser: {_ANALYSER or 'NOT FOUND'}")
log.info(f"Generator: {_GENERATOR or 'NOT FOUND'}")

mcp = FastMCP(
    "faroese-dictionary",
    instructions=(
        "Faroese dictionary MCP server with HFST morphological engine.\n\n"
        "CRITICAL RULES — When writing Faroese:\n"
        "- NEVER change, adapt, or 'correct' words returned by the tools. "
        "If lookup_word says 'blæa', write 'blæa' — not 'bleia', not 'bleija'. "
        "Copy the EXACT spelling from tool results.\n"
        "- NEVER guess Faroese words from memory. ALWAYS look up first.\n"
        "- Do NOT translate word-for-word. Write natural Faroese.\n"
        "- Prefer everyday words over formal/literary ones.\n\n"
        "WORKFLOW:\n"
        "1. BEFORE writing: generate_form (batch with semicolons) for ALL verbs, nouns, adjectives.\n"
        "2. AFTER writing: verify_text to check grammar agreement automatically.\n"
        "3. When answering 'how do you say X in Faroese': return the EXACT word from the tool, "
        "do NOT modify the spelling.\n\n"
        "Keep it silent — fix issues without explaining.\n"
    ),
)


# ── DB helpers ─────────────────────────────────────────────────────────────

@contextlib.contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


# ── HFST helpers ───────────────────────────────────────────────────────────

def _hfst_lookup(transducer: str | None, inputs: list[str]) -> dict[str, list[str]]:
    """Run hfst-optimized-lookup. Returns {input: [results]}."""
    if not transducer or not os.path.exists(transducer):
        return {}
    try:
        text = "\n".join(inputs)
        result = subprocess.run(
            ["hfst-optimized-lookup", "-q", transducer],
            input=text + "\n", capture_output=True, text=True, timeout=10,
        )
        output: dict[str, list[str]] = {}
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2 and "+?" not in parts[1] and parts[1] != parts[0]:
                output.setdefault(parts[0], []).append(parts[1])
        return output
    except Exception:
        return {}


def _parse_analysis(analysis: str) -> dict[str, str]:
    """Parse 'keypmaður+N+Msc+Pl+Nom+Indef' → structured dict."""
    parts = analysis.split("+")
    if len(parts) < 2:
        return {}
    result = {"lemma": parts[0], "raw": analysis}
    tags = set(parts[1:])
    for pos in ("N", "V", "A", "Adv", "Num", "Det", "Pron", "Pr", "CC", "CS"):
        if pos in tags:
            result["pos"] = pos
            break
    for g in ("Msc", "Fem", "Neu"):
        if g in tags:
            result["gender"] = g
            break
    for c in ("Nom", "Acc", "Dat", "Gen"):
        if c in tags:
            result["case"] = c
            break
    for n in ("Sg", "Pl"):
        if n in tags:
            result["number"] = n
            break
    for d in ("Def", "Indef"):
        if d in tags:
            result["def"] = d
            break
    for pt in ("Pers", "Refl", "Dem", "Poss"):
        if pt in tags:
            result["pron_type"] = pt
            break
    # Infer gender for personal pronouns
    _PG = {"hon": "Fem", "hann": "Msc", "tað": "Neu", "teir": "Msc", "tær": "Fem", "tey": "Neu"}
    if result.get("pos") == "Pron" and result["lemma"] in _PG:
        result["gender"] = _PG[result["lemma"]]
    return result


def _best_analysis(analyses: list[str]) -> dict[str, str]:
    """Pick most likely analysis. Prefer Pron > Pr > Det/Num > A > N.
    For Det/Num, prefer Sg+Acc/Nom over Pl+Gen (more common context)."""
    parsed = [_parse_analysis(a) for a in analyses if a]
    parsed = [p for p in parsed if p]
    if not parsed:
        return {}

    for prefer in ("Pron", "Pr"):
        matches = [p for p in parsed if p.get("pos") == prefer]
        if matches:
            return matches[0]

    # For Det/Num: prefer singular accusative/nominative (most common use)
    dets = [p for p in parsed if p.get("pos") in ("Det", "Num")]
    if dets:
        # Prefer Sg+Acc, then Sg+Nom, then Fem over others
        for pref_case in ("Acc", "Nom"):
            for d in dets:
                if d.get("number") == "Sg" and d.get("case") == pref_case:
                    return d
        return dets[0]

    return parsed[0]


_PREP_CASE = {
    "til": "Acc", "um": "Acc", "gjøgnum": "Acc", "eftir": "Acc",
    "uttan": "Acc", "vegna": "Acc", "millum": "Acc", "innan": "Acc",
    "av": "Dat", "frá": "Dat", "hjá": "Dat", "móti": "Dat",
    "undan": "Dat", "úr": "Dat", "við": "Dat",
}


# ── Grammar rules ──────────────────────────────────────────────────────────

def _load_json(name: str) -> dict:
    path = os.path.join(DATA_DIR, name)
    try:
        with open(path, encoding="utf-8") as f:
            return _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        return {}


_GRAMMAR_RULES: dict = {}
_DOMAIN_TERMS: dict = {}


def _get_grammar_rules() -> dict:
    global _GRAMMAR_RULES
    if not _GRAMMAR_RULES:
        _GRAMMAR_RULES = _load_json("faroese_grammar_rules.json")
    return _GRAMMAR_RULES


def _get_domain_terms() -> dict:
    global _DOMAIN_TERMS
    if not _DOMAIN_TERMS:
        _DOMAIN_TERMS = _load_json("domain_terms.json")
    return _DOMAIN_TERMS


# ── Tools ──────────────────────────────────────────────────────────────────

@mcp.tool()
def generate_form(lemma: str, pos: str, tags: str) -> dict[str, Any]:
    """Generate correct Faroese inflected forms using HFST. Batch with semicolons.

    Args:
        lemma: "keypmaður" or batch "keypmaður;vænta;hoyra"
        pos: "N" or batch "N;V;V"
        tags: "Msc+Pl+Nom+Indef" or batch "Msc+Pl+Nom+Indef;Ind+Prt+3Sg;Imp"
            Nouns: Msc/Fem/Neu + Sg/Pl + Nom/Acc/Dat/Gen + Def/Indef
            Verbs: Ind+Prs/Prt + 1Sg/2Sg/3Sg, Imp, PrfPtc
            Adj: Msc/Fem/Neu + Sg/Pl + Nom/Acc/Dat/Gen
    """
    lemmas = [l.strip() for l in lemma.split(";")]
    poses = [p.strip() for p in pos.split(";")]
    taglist = [t.strip() for t in tags.split(";")]

    while len(poses) < len(lemmas): poses.append(poses[-1])
    while len(taglist) < len(lemmas): taglist.append(taglist[-1])

    # Build all analyses
    queries = []
    for l, p, t in zip(lemmas, poses, taglist):
        queries.append(f"{l}+{p}+{t}")
        # Also try variations
        if p == "V" and "Imp" in t and "Sg" not in t:
            queries.append(f"{l}+V+Imp+Sg")
        elif p == "V" and "Ind" not in t and "Imp" not in t:
            queries.append(f"{l}+V+Ind+{t}")

    raw = _hfst_lookup(_GENERATOR, queries)

    results = []
    for l, p, t in zip(lemmas, poses, taglist):
        primary = f"{l}+{p}+{t}"
        form = (raw.get(primary, [None]) or [None])[0]
        if not form:
            # Try variations
            for alt in [f"{l}+V+Imp+Sg", f"{l}+V+Ind+{t}", f"{l}+{p}+{t.replace('+Indef','').replace('+Def','')}"]:
                form = (raw.get(alt, [None]) or [None])[0]
                if form:
                    break
        results.append({"lemma": l, "form": form})

    if len(results) == 1:
        return {"lemma": results[0]["lemma"], "generated_form": results[0]["form"]}
    return {"batch": True, "results": results}


@mcp.tool()
def verify_text(text: str) -> dict[str, Any]:
    """Verify Faroese text grammar using HFST morphological analyser.
    Analyses EVERY word and checks: article+noun gender, preposition+case,
    pronoun+adjective gender, reflexive case, unknown words.

    Args:
        text: Faroese text to verify
    """
    if not text or not text.strip():
        return {"issues": [], "total_issues": 0}

    word_re = re.compile(r"[A-Za-zÁáÍíÓóÚúÝýÆæØøÐð]+(?:-[A-Za-zÁáÍíÓóÚúÝýÆæØøÐð]+)*")
    tokens = [(m.group(), m.start()) for m in word_re.finditer(text)]
    if not tokens:
        return {"issues": [], "total_issues": 0}

    words = [t for t, _ in tokens]
    raw = _hfst_lookup(_ANALYSER, words)

    # Parse analyses
    analysed = []
    for word, pos in tokens:
        analyses = raw.get(word, [])
        parsed = _best_analysis(analyses)
        entry = {"word": word, "a": parsed}
        if not analyses and len(word) > 2:
            entry["unknown"] = True
        analysed.append(entry)

    issues: list[dict[str, Any]] = []

    for i, token in enumerate(analysed):
        a = token["a"]
        word = token["word"]

        # Words with Err/Orth tag = spelling/orthography error
        all_analyses = raw.get(word, [])
        if all_analyses and all("+Err/" in a for a in all_analyses):
            issues.append({"type": "spelling_error", "word": word,
                           "message": f"'{word}' has orthographic error according to analyser."})
            continue

        # Unknown words (skip very short words and sentence-initial capitalized)
        if token.get("unknown"):
            # Check if it's sentence-initial (allow proper nouns there)
            is_sentence_start = (i == 0 or
                (i > 0 and analysed[i-1]["word"] in (".", "!", "?", ":")))
            # Flag if not a likely proper noun (mid-sentence capitalized = proper noun)
            if not (word[0].isupper() and not is_sentence_start):
                issues.append({"type": "unknown_word", "word": word,
                               "message": f"'{word}' not recognised. Possible spelling error or non-Faroese word."})
            continue

        pos = a.get("pos", "")
        gender = a.get("gender", "")
        case = a.get("case", "")

        # Preposition + noun case
        if pos == "Pr" and word.lower() in _PREP_CASE:
            required = _PREP_CASE[word.lower()]
            for j in range(i + 1, min(i + 4, len(analysed))):
                na = analysed[j]["a"]
                if na.get("pos") == "N" and na.get("case"):
                    if na["case"] != required and required in ("Acc", "Dat"):
                        issues.append({"type": "preposition_case", "word": analysed[j]["word"],
                                       "message": f"'{word}' requires {required.lower()}, but "
                                                   f"'{analysed[j]['word']}' ({na.get('lemma','?')}) is {na['case'].lower()}."})
                    break

        # Article/determiner + noun gender
        if pos in ("Det", "Num") and gender:
            for j in range(i + 1, min(i + 3, len(analysed))):
                na = analysed[j]["a"]
                if na.get("pos") == "N" and na.get("gender"):
                    if gender != na["gender"]:
                        issues.append({"type": "article_gender", "word": f"{word}...{analysed[j]['word']}",
                                       "message": f"'{word}' is {gender.lower()}, but "
                                                   f"'{analysed[j]['word']}' ({na.get('lemma','?')}) is {na['gender'].lower()}."})
                    break
                elif na.get("pos") == "A":
                    continue
                else:
                    break

        # Pronoun + var/er + adjective gender
        if pos == "Pron" and a.get("pron_type") == "Pers" and gender and case == "Nom":
            for j in range(i + 1, min(i + 3, len(analysed))):
                if analysed[j]["word"].lower() in ("var", "er"):
                    if j + 1 < len(analysed):
                        adj_a = analysed[j + 1]["a"]
                        if adj_a.get("pos") == "A" and adj_a.get("gender"):
                            if adj_a["gender"] != gender:
                                issues.append({"type": "adjective_gender", "word": analysed[j + 1]["word"],
                                               "message": f"'{word}' is {gender.lower()}, but adjective "
                                                           f"'{analysed[j+1]['word']}' is {adj_a['gender'].lower()}."})
                    break
                elif analysed[j]["a"].get("pos") not in ("Adv", ""):
                    break

        # Reflexive case
        if a.get("pron_type") == "Refl" and case == "Acc" and i > 0:
            prev_word = analysed[i - 1]["word"].lower()
            if prev_word in ("keypti", "keypi", "fekk", "fær"):
                issues.append({"type": "reflexive_case", "word": word,
                               "message": f"'{prev_word} {word}' — use dative 'sær', not accusative 'seg'."})

    return {
        "issues": issues,
        "total_issues": len(issues),
        "words_analysed": len(analysed),
        "has_analyser": _ANALYSER is not None,
    }


@mcp.tool()
def lookup_word(word: str) -> dict[str, Any]:
    """Look up a Faroese word — definition, grammar, inflections.

    Args:
        word: Faroese headword (e.g. "feilur", "keypmaður", "vakur")
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM words WHERE search_word = ? COLLATE NOCASE", (word,)
        ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT * FROM words WHERE search_word LIKE ? COLLATE NOCASE LIMIT 5",
                (f"{word}%",),
            ).fetchall()
            if not rows:
                return {"error": f"'{word}' not found"}

        results = []
        for r in rows:
            w = row_to_dict(r)
            inflections = conn.execute(
                "SELECT form_index, form FROM inflected_forms WHERE word_id = ? ORDER BY form_index",
                (w["id"],),
            ).fetchall()
            w["inflected_forms"] = [r["form"] for r in inflections]
            results.append(w)

    return {"count": len(results), "results": results}


@mcp.tool()
def translate_text(text: str, domain: str = "") -> dict[str, Any]:
    """Translate English text to Faroese with domain terminology.

    Args:
        text: English text to translate
        domain: Optional domain hint (e.g. "KT" for tech/IT terms)
    """
    # Simplified: just look up each content word in EN-FO translations
    word_re = re.compile(r"[A-Za-z]+(?:'[a-z]+)?")
    tokens = [m.group() for m in word_re.finditer(text)]
    if not tokens:
        return {"error": "No words found"}

    stop_words = {"the", "a", "an", "is", "are", "was", "were", "of", "in", "on",
                  "at", "to", "for", "with", "and", "but", "or", "not", "this", "that",
                  "it", "he", "she", "they", "we", "you", "i", "our", "your", "my"}

    domain_map = {}
    if domain:
        terms = _get_domain_terms()
        domain_map = {k: v for k, v in terms.get(domain.upper(), {}).items() if not k.startswith("_")}

    glossary = []
    seen = set()
    with get_db() as conn:
        for tok in tokens:
            lower = tok.lower()
            if lower in stop_words or lower in seen:
                continue
            seen.add(lower)

            # Domain term?
            if lower in domain_map:
                glossary.append({"source": lower, "translation": domain_map[lower], "source_type": "domain"})
                continue

            # DB lookup
            rows = conn.execute(
                "SELECT target_word FROM translations WHERE source_word = ? COLLATE NOCASE AND dictionary_id = 3 LIMIT 3",
                (tok,),
            ).fetchall()
            if rows:
                glossary.append({"source": lower, "translations": [r["target_word"][:100] for r in rows]})

    return {"glossary": glossary, "domain": domain.upper() if domain else None}


@mcp.tool()
def check_register(word: str) -> dict[str, Any]:
    """Check if a Faroese word is standard, formal, archaic, or dialectal.

    Args:
        word: Faroese word to check
    """
    _MARKERS = {
        "(sj.)": "rare", "(gl.)": "archaic", "(kvæð.)": "literary",
        "(bíbl.)": "biblical", "(stb.)": "dialectal",
    }
    with get_db() as conn:
        rows = conn.execute(
            "SELECT search_word, explanation_text FROM words WHERE search_word = ? COLLATE NOCASE",
            (word,),
        ).fetchall()
        if not rows:
            return {"word": word, "found": False}

        explanation = rows[0]["explanation_text"] or ""
        result: dict[str, Any] = {"word": word, "found": True}

        # Redirect?
        m = re.match(r"^sí\s+(.+)$", explanation.strip())
        if m:
            result["redirect_to"] = m.group(1).strip()
            return result

        # Register markers in first 80 chars
        markers = []
        for marker, label in _MARKERS.items():
            if marker in explanation[:80]:
                markers.append(label)
        if markers:
            result["register_warnings"] = markers
        else:
            result["register"] = "standard"

        # Synonyms
        syns = re.match(r"^([a-záíóúýæøð]+(?:\s*,\s*[a-záíóúýæøð]+)*)", explanation.strip(), re.I)
        if syns and not re.match(r"^\d", explanation.strip()):
            result["synonyms"] = [s.strip() for s in syns.group(1).split(",") if s.strip().lower() != word.lower()][:3]

    return result


@mcp.tool()
def grammar_reference(query: str) -> dict[str, Any]:
    """Look up Faroese grammar rules, conjugation tables, common mistakes.

    Args:
        query: Topic — "common mistakes", "verb fara", "prepositions", "imperative",
               "pronouns", "adjective", "terminology", "word order"
    """
    rules = _get_grammar_rules()
    if not rules:
        return {"error": "Grammar rules file not found"}

    q = query.lower().strip()
    result: dict[str, Any] = {"query": query}

    if "mistake" in q or "feil" in q or "common" in q:
        result["type"] = "common_mistakes"
        result["mistakes"] = rules.get("common_mistakes", {})
    elif q.startswith("verb "):
        verb = q[5:].strip()
        for key in ("auxiliary_verbs", "preterite_present_verbs"):
            if verb in rules.get(key, {}):
                result["type"] = key
                result["conjugation"] = rules[key][verb]
                return result
        result["type"] = "verb_not_in_rules"
        result["hint"] = "Use generate_form to get correct forms"
    elif "pronoun" in q:
        result["type"] = "pronouns"
        result["data"] = rules.get("personal_pronouns", {})
    elif "preposition" in q:
        result["type"] = "prepositions"
        result["data"] = rules.get("prepositions", {})
    elif "imperative" in q:
        result["type"] = "imperative"
        result["data"] = rules.get("imperative", {})
    elif "adjective" in q:
        result["type"] = "adjective"
        result["data"] = rules.get("adjective_declension", {})
    elif "terminolog" in q or "vitlíki" in q:
        result["type"] = "terminology"
        result["data"] = rules.get("faroese_terminology", {})
    elif "word order" in q:
        result["type"] = "word_order"
        result["data"] = rules.get("word_order", {})
    else:
        result["type"] = "help"
        result["topics"] = [
            "common mistakes", "verb vera/hava/fara/...", "pronouns",
            "prepositions", "imperative", "adjective", "terminology", "word order",
        ]

    return result


def main():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    transport = "stdio"
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        if idx + 1 < len(sys.argv):
            transport = sys.argv[idx + 1]

    log.info(f"Starting Faroese MCP server (transport={transport}, db={DB_PATH})")
    log.info(f"Analyser: {'✓' if _ANALYSER else '✗'}  Generator: {'✓' if _GENERATOR else '✗'}")

    if transport == "http":
        mcp.run(transport="sse", host="0.0.0.0", port=8080)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
