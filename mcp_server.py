#!/usr/bin/env python3
"""
Faroese Dictionary MCP Server (sprotin.fo data).
Exposes 66,813 Faroese words with inflections and grammar via MCP tools.

Run via Claude Code:
  claude mcp add --transport stdio --scope user faroese-dictionary -- \
    /Users/svenbjornkviltolsen/work/sprotan/.venv/bin/python3 \
    /Users/svenbjornkviltolsen/work/sprotan/mcp_server.py
"""

import contextlib
import logging
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP

# Logging to stderr (critical for stdio transport)
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sprotin-mcp")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "sprotin.db")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TOOLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "giellalt")

# Docker path fallbacks
if not os.path.exists(DB_PATH) and os.path.exists("/app/db/sprotin.db"):
    DB_PATH = "/app/db/sprotin.db"
if not os.path.isdir(DATA_DIR) and os.path.isdir("/app/data"):
    DATA_DIR = "/app/data"
if not os.path.isdir(TOOLS_DIR) and os.path.isdir("/app/tools/giellalt"):
    TOOLS_DIR = "/app/tools/giellalt"

mcp = FastMCP(
    "faroese-dictionary",
    host="0.0.0.0",
    instructions=(
        "Faroese dictionary MCP server with 67,000+ words from sprotin.fo. "
        "\n\n"
        "RULES — When writing Faroese:\n"
        "- Do NOT guess words. If unsure, look it up or keep the original.\n"
        "- Do NOT translate word-for-word. Write natural Faroese.\n"
        "- Prefer everyday words over formal/literary ones.\n"
        "\n"
        "WORKFLOW — When writing Faroese:\n"
        "1. BEFORE writing: Use generate_form (supports batch with semicolons).\n"
        "   Single: generate_form('vænta','V','Ind+Prt+3Sg') → 'væntaði'\n"
        "   Batch: generate_form('keypmaður;vænta;hoyra','N;V;V','Msc+Pl+Nom+Indef;Ind+Prt+3Sg;Imp')\n"
        "   → ['keypmenn','væntaði','hoyr'] in ONE call.\n"
        "2. AFTER writing: Run review(text) — spelling + grammar in ONE call.\n"
        "For EVERY verb, noun case, adjective gender: generate_form first.\n"
        "\n"
        "Keep it silent — fix issues without explaining. The user wants clean Faroese, "
        "not a list of tool calls.\n"
    ),
)


# ── GiellaLT API (external morphological analysis + grammar checking) ─────

_GIELLALT_BASE = "https://api.giellalt.org"
_GIELLALT_TIMEOUT = 15


def _giellalt_spellcheck(text: str) -> dict | None:
    """Call GiellaLT speller API for Faroese. Returns None on failure."""
    try:
        url = f"{_GIELLALT_BASE}/speller/fo"
        data = _json.dumps({"text": text}).encode()
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": "sprotan-mcp/1.0",
            "Content-Type": "application/json",
        })
        resp = urllib.request.urlopen(req, timeout=_GIELLALT_TIMEOUT)
        return _json.loads(resp.read())
    except Exception as e:
        log.warning(f"GiellaLT speller API failed: {e}")
        return None


def _giellalt_gramcheck(text: str) -> dict | None:
    """Call GiellaLT grammar checker API for Faroese. Returns None on failure."""
    try:
        url = f"{_GIELLALT_BASE}/grammar/fo"
        data = _json.dumps({"text": text}).encode()
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": "sprotan-mcp/1.0",
            "Content-Type": "application/json",
        })
        resp = urllib.request.urlopen(req, timeout=_GIELLALT_TIMEOUT)
        return _json.loads(resp.read())
    except Exception as e:
        log.warning(f"GiellaLT grammar API failed: {e}")
        return None


# ── HFST transducers (GiellaLT morphological analysis + generation) ────────

_HFST_LOOKUP = None
for _p in [os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "bin", "hfst-optimized-lookup"),
           "/usr/local/bin/hfst-optimized-lookup", "/usr/bin/hfst-optimized-lookup"]:
    if os.path.exists(_p):
        _HFST_LOOKUP = _p
        break

# Discover analyser and generator transducers by scanning TOOLS_DIR
_ANALYSER = None
_GENERATOR = None
if os.path.isdir(TOOLS_DIR):
    for f in os.listdir(TOOLS_DIR):
        if "analyser" in f and "desc" in f and f.endswith(".hfstol"):
            _ANALYSER = os.path.join(TOOLS_DIR, f)
        if "generator" in f and "norm" in f and f.endswith(".hfstol"):
            _GENERATOR = os.path.join(TOOLS_DIR, f)

# Fallback: hardcoded generator path for backwards compatibility
if not _GENERATOR:
    _fallback = os.path.join(TOOLS_DIR, "generator-gramcheck-gt-norm.hfstol")
    if os.path.exists(_fallback):
        _GENERATOR = _fallback

log.info(f"HFST lookup: {_HFST_LOOKUP or 'NOT FOUND'}")
log.info(f"Analyser: {_ANALYSER or 'NOT FOUND'}")
log.info(f"Generator: {_GENERATOR or 'NOT FOUND'}")


def _hfst_generate(analysis: str) -> str | None:
    """Run HFST generator to produce a surface form from morphological tags.
    Returns the generated form, or None if generation fails."""
    if not _HFST_LOOKUP or not _GENERATOR:
        return None
    try:
        import subprocess
        result = subprocess.run(
            [_HFST_LOOKUP, "-q", _GENERATOR],
            input=analysis + "\n",
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1] != parts[0] and "+?" not in parts[1]:
                    return parts[1]
        return None
    except Exception:
        return None


def _hfst_analyse(word: str) -> list[str]:
    """Run HFST analyser on a single word. Returns list of analysis strings."""
    if not _HFST_LOOKUP or not _ANALYSER:
        return []
    try:
        result = subprocess.run(
            [_HFST_LOOKUP, "-q", _ANALYSER],
            input=word + "\n",
            capture_output=True, text=True, timeout=5,
        )
        analyses = []
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = line.split("\t")
                if len(parts) >= 2 and "+?" not in parts[1] and parts[1] != parts[0]:
                    analyses.append(parts[1])
        return analyses
    except Exception:
        return []


# ── HFST+CG3 pipeline (full morphological analysis + disambiguation) ──────

_VISLCG3 = None
_DISAMBIGUATOR = None
for _p in [os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "bin", "vislcg3"),
           "/usr/local/bin/vislcg3", "/usr/bin/vislcg3"]:
    if os.path.exists(_p):
        _VISLCG3 = _p
        break
for _p in [os.path.join(TOOLS_DIR, "disambiguator.bin"),
           os.path.join(TOOLS_DIR, "grc-disambiguator.bin")]:
    if os.path.exists(_p):
        _DISAMBIGUATOR = _p
        break

log.info(f"CG3: {_VISLCG3 or 'NOT FOUND'}  Disamb: {_DISAMBIGUATOR or 'NOT FOUND'}")


def _hfst_analyse_batch(tokens: list[str]) -> dict[str, list[str]]:
    """Analyse multiple tokens with HFST in one call."""
    if not _HFST_LOOKUP or not _ANALYSER:
        return {}
    try:
        input_str = "\n".join(tokens) + "\n"
        result = subprocess.run(
            [_HFST_LOOKUP, "-q", _ANALYSER],
            input=input_str, capture_output=True, text=True, timeout=15,
        )
        by_token: dict[str, list[str]] = {}
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) >= 2:
                    surface, analysis = parts[0], parts[1]
                    if surface not in by_token:
                        by_token[surface] = []
                    if "+?" not in analysis and analysis != surface:
                        by_token[surface].append(analysis)
        return by_token
    except Exception as e:
        log.warning(f"HFST batch analyse failed: {e}")
        return {}


def _build_cg3_input(tokens: list[str], analyses: dict[str, list[str]]) -> str:
    """Build CG3 cohort format from HFST analyses."""
    lines = []
    for tok in tokens:
        lines.append(f'"<{tok}>"')
        token_analyses = analyses.get(tok, [])
        if not token_analyses:
            lines.append(f'\t"{tok}" ?')
        for a in token_analyses:
            parts = a.split("+")
            lemma = parts[0]
            tags = " ".join(parts[1:])
            lines.append(f'\t"{lemma}" {tags}')
    return "\n".join(lines) + "\n"


def _cg3_disambiguate(cg3_input: str) -> str | None:
    """Run CG3 disambiguator. Returns disambiguated cohort text or None."""
    if not _VISLCG3 or not _DISAMBIGUATOR:
        return None
    try:
        result = subprocess.run(
            [_VISLCG3, "-g", _DISAMBIGUATOR],
            input=cg3_input, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout
        return None
    except Exception as e:
        log.warning(f"CG3 disambiguation failed: {e}")
        return None


def _parse_cg3_output(cg3_output: str) -> list[dict]:
    """Parse CG3 cohort output into structured list.
    Returns list of {surface, lemma, pos, tags: set} per token."""
    result = []
    current: dict | None = None
    for line in cg3_output.strip().split("\n"):
        line = line.strip()
        if line.startswith('"<') and line.endswith('>"'):
            if current:
                result.append(current)
            surface = line[2:-2]
            current = {"surface": surface, "readings": []}
        elif line.startswith('"') and current is not None:
            # Parse: "lemma" Tag1 Tag2 Tag3 ...
            m = re.match(r'"([^"]*)"(.*)', line)
            if m:
                lemma = m.group(1)
                tag_str = m.group(2).strip()
                tags = set(tag_str.split()) if tag_str else set()
                pos = ""
                for t in ["N", "V", "A", "Adv", "Pron", "Det", "Num", "CC", "CS", "Pr"]:
                    if t in tags:
                        pos = t
                        tags.discard(t)
                        break
                current["readings"].append({
                    "lemma": lemma, "pos": pos, "tags": tags,
                })
    if current:
        result.append(current)
    return result


def _analyse_text_full(text: str) -> list[dict]:
    """Full pipeline: tokenise → HFST analyse → CG3 disambiguate → parsed output."""
    tokens = [t for t in re.findall(r"\w+", text) if len(t) > 0]
    if not tokens:
        return []

    analyses = _hfst_analyse_batch(tokens)
    cg3_input = _build_cg3_input(tokens, analyses)
    cg3_output = _cg3_disambiguate(cg3_input)

    if cg3_output:
        parsed = _parse_cg3_output(cg3_output)
        # Attach all possible genders from raw HFST (before disambiguation)
        # so agreement checks can avoid false positives on ambiguous forms
        for i, tok in enumerate(tokens):
            if i < len(parsed):
                raw_genders = set()
                for a in analyses.get(tok, []):
                    for tag in a.split("+"):
                        if tag in _GENDER_TAG:
                            raw_genders.add(_GENDER_TAG[tag])
                parsed[i]["all_genders"] = raw_genders
        return parsed

    # Fallback: return raw HFST analyses without disambiguation
    result = []
    for tok in tokens:
        readings = []
        for a in analyses.get(tok, []):
            parts = a.split("+")
            lemma = parts[0]
            all_tags = set(parts[1:])
            pos = ""
            for t in ["N", "V", "A", "Adv", "Pron", "Det", "Num", "CC", "CS", "Pr"]:
                if t in all_tags:
                    pos = t
                    all_tags.discard(t)
                    break
            readings.append({"lemma": lemma, "pos": pos, "tags": all_tags})
        result.append({"surface": tok, "readings": readings})
    return result


_GENDER_TAG = {"Msc": "m", "Fem": "f", "Neu": "n"}
_CASE_TAG = {"Nom": "nom", "Acc": "acc", "Dat": "dat", "Gen": "gen"}
_NUMBER_TAG = {"Sg": "sg", "Pl": "pl"}


def _extract_features(reading: dict) -> dict:
    """Extract gender, case, number from a reading's tags."""
    tags = reading["tags"]
    feat: dict[str, str | None] = {"gender": None, "case": None, "number": None}
    for t in tags:
        if t in _GENDER_TAG:
            feat["gender"] = _GENDER_TAG[t]
        elif t in _CASE_TAG:
            feat["case"] = _CASE_TAG[t]
        elif t in _NUMBER_TAG:
            feat["number"] = _NUMBER_TAG[t]
    return feat


def _check_agreement_hfst(parsed: list[dict]) -> list[dict]:
    """Check gender/case/number agreement using disambiguated HFST output."""
    issues = []

    for i, token in enumerate(parsed):
        if not token["readings"]:
            continue
        r = token["readings"][0]  # disambiguated = usually 1 reading
        feat = _extract_features(r)

        # Det/Pron + Noun gender agreement
        if r["pos"] in ("Det", "Pron", "Num") and feat["gender"] and i + 1 < len(parsed):
            # Look ahead for noun (may have adjective in between)
            for j in range(i + 1, min(i + 4, len(parsed))):
                nxt = parsed[j]
                if not nxt["readings"]:
                    break
                nr = nxt["readings"][0]
                nf = _extract_features(nr)
                if nr["pos"] == "N" and nf["gender"]:
                    if feat["gender"] != nf["gender"]:
                        # Check ALL possible genders from raw HFST (before CG3)
                        # If any reading matches the noun gender, it's ambiguous
                        # (e.g. "ein" can be both Msc and Fem nom)
                        all_g = token.get("all_genders", set())
                        if nf["gender"] not in all_g:
                            gl = {"m": "masculine", "f": "feminine", "n": "neuter"}
                            issues.append({
                                "source": "hfst_cg3", "type": "determiner_noun_gender",
                                "word": f"{token['surface']} ... {nxt['surface']}",
                                "message": f"'{token['surface']}' is {gl[feat['gender']]}, but '{nr['lemma']}' is {gl[nf['gender']]}.",
                            })
                    break
                elif nr["pos"] == "A":
                    continue  # skip adjectives between det and noun
                else:
                    break

        # Adjective + Noun gender agreement
        if r["pos"] == "A" and feat["gender"] and i + 1 < len(parsed):
            nxt = parsed[i + 1]
            if nxt["readings"]:
                nr = nxt["readings"][0]
                nf = _extract_features(nr)
                if nr["pos"] == "N" and nf["gender"] and feat["gender"] != nf["gender"]:
                    gl = {"m": "masculine", "f": "feminine", "n": "neuter"}
                    issues.append({
                        "source": "hfst_cg3", "type": "adjective_noun_gender",
                        "word": f"{token['surface']} {nxt['surface']}",
                        "message": f"'{token['surface']}' is {gl[feat['gender']]}, but '{nr['lemma']}' is {gl[nf['gender']]}.",
                    })

        # Noun/Pron + var/er + Adjective gender agreement
        if r["pos"] == "V" and r["lemma"] == "vera" and feat.get("gender") is None:
            if i >= 1 and i + 1 < len(parsed):
                subj = parsed[i - 1]
                pred = parsed[i + 1]
                if subj["readings"] and pred["readings"]:
                    sr = subj["readings"][0]
                    pr = pred["readings"][0]
                    sf = _extract_features(sr)
                    pf = _extract_features(pr)
                    if sr["pos"] in ("N", "Pron") and pr["pos"] == "A":
                        if sf["gender"] and pf["gender"] and sf["gender"] != pf["gender"]:
                            gl = {"m": "masculine", "f": "feminine", "n": "neuter"}
                            issues.append({
                                "source": "hfst_cg3", "type": "predicate_adjective_gender",
                                "word": f"{subj['surface']} {token['surface']} {pred['surface']}",
                                "message": f"Subject '{subj['surface']}' is {gl[sf['gender']]}, but '{pred['surface']}' is {gl[pf['gender']]}.",
                            })

        # Adjective + Noun case agreement
        if r["pos"] == "A" and feat["case"] and i + 1 < len(parsed):
            nxt = parsed[i + 1]
            if nxt["readings"]:
                nr = nxt["readings"][0]
                nf = _extract_features(nr)
                if nr["pos"] == "N" and nf["case"] and feat["case"] != nf["case"]:
                    issues.append({
                        "source": "hfst_cg3", "type": "adjective_noun_case",
                        "word": f"{token['surface']} {nxt['surface']}",
                        "message": f"'{token['surface']}' is {feat['case']}, but '{nr['lemma']}' is {nf['case']}.",
                    })

    # "hvørt annað" with animate/people subjects → should be "hvønn annan"
    for i, token in enumerate(parsed):
        lower = token["surface"].lower()
        if lower == "hvørt" and i + 1 < len(parsed):
            nxt_lower = parsed[i + 1]["surface"].lower()
            if nxt_lower == "annað":
                # Check if context has animate subjects (pronouns like tey, vit, etc.)
                # or plural verbs before this phrase
                issues.append({
                    "source": "hfst_cg3", "type": "reciprocal_gender",
                    "word": f"{token['surface']} {parsed[i+1]['surface']}",
                    "message": "'hvørt annað' is neuter. For people use 'hvønn annan'.",
                })

    return issues


# ── Grammar rules (verb conjugation, pronouns, adjective declension) ──────

_GRAMMAR_RULES_PATH = os.path.join(DATA_DIR, "faroese_grammar_rules.json")


def _load_grammar_rules() -> dict:
    try:
        with open(_GRAMMAR_RULES_PATH, encoding="utf-8") as f:
            return _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        return {}


_GRAMMAR_RULES: dict = {}  # lazy-loaded


def _get_grammar_rules() -> dict:
    global _GRAMMAR_RULES
    if not _GRAMMAR_RULES:
        _GRAMMAR_RULES = _load_grammar_rules()
    return _GRAMMAR_RULES


# ── Paradigm labels (inflection form_index → grammar label) ───────────────
#
# Extracted from sprotin.fo fm.dictionary.jquery.js.
# Mapping is based on InflectedForm array length:
#   6 = verb, 16 = noun, 24 = adjective.

import json as _json

_PARADIGM_LABELS_PATH = os.path.join(DATA_DIR, "paradigm_labels.json")


def _load_paradigm_labels() -> dict[str, dict[str, dict]]:
    try:
        with open(_PARADIGM_LABELS_PATH, encoding="utf-8") as f:
            raw = _json.load(f)
        return {
            k: v for k, v in raw.items() if not k.startswith("_")
        }
    except (FileNotFoundError, _json.JSONDecodeError):
        return {}


_PARADIGM_LABELS = _load_paradigm_labels()


def _label_inflections(forms: list[str]) -> list[dict[str, str]]:
    """Attach grammar labels to inflected forms based on array length."""
    n = len(forms)
    if n == 6:
        key = "verb_6"
    elif n == 16:
        key = "noun_16"
    elif n == 24:
        key = "adjective_24"
    else:
        return [{"form_index": i, "form": f} for i, f in enumerate(forms)]

    paradigm = _PARADIGM_LABELS.get(key, {})
    result = []
    for i, form in enumerate(forms):
        entry: dict[str, Any] = {"form_index": i, "form": form}
        label_data = paradigm.get(str(i))
        if label_data:
            entry["label"] = label_data.get("label_en", "")
            entry["label_fo"] = label_data.get("label", "")
        result.append(entry)
    return result


# ── Translate-text engine ──────────────────────────────────────────────────

_EN_WORD_RE = re.compile(r"[A-Za-z]+(?:'[a-z]+)?")

_ENGLISH_STOP_WORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "at", "to", "for", "with", "and", "but", "or", "not",
    "this", "that", "it", "he", "she", "they", "we", "you", "i",
    "our", "your", "my", "his", "her", "its", "their",
    "do", "does", "did", "has", "have", "had", "will", "would", "can", "could",
    "shall", "should", "may", "might", "must",
    "so", "if", "than", "then", "as", "by", "from", "up", "about", "into",
    "through", "during", "before", "after", "above", "below", "between",
    "each", "every", "all", "both", "few", "more", "most", "other", "some",
    "such", "no", "only", "own", "same", "very", "us",
}

# English suffix stripping: (suffix, replacement) ordered longest-first.
_EN_SUFFIXES = [
    ("iness", "y"), ("ying", "ie"),
    ("ies", "y"), ("ied", "y"),
    ("ness", ""), ("ment", ""),
    ("ing", "e"), ("ing", ""),
    ("ed", "e"), ("ed", ""),
    ("er", "e"), ("er", ""),
    ("ly", ""), ("es", "e"), ("es", ""), ("s", ""),
]

# Common English irregular forms -> base form.
_EN_IRREGULARS: dict[str, str] = {
    "built": "build", "bought": "buy", "brought": "bring", "caught": "catch",
    "chose": "choose", "chosen": "choose", "came": "come",
    "done": "do", "drew": "draw", "drawn": "draw", "drove": "drive",
    "driven": "drive", "fell": "fall", "fallen": "fall", "felt": "feel",
    "found": "find", "flew": "fly", "flown": "fly",
    "forgotten": "forget", "gave": "give", "given": "give", "went": "go",
    "gone": "go", "grew": "grow", "grown": "grow", "held": "hold",
    "kept": "keep", "knew": "know", "known": "know", "led": "lead",
    "left": "leave", "lent": "lend", "lost": "lose", "made": "make",
    "meant": "mean", "met": "meet", "paid": "pay",
    "ran": "run", "said": "say", "saw": "see", "seen": "see",
    "sold": "sell", "sent": "send", "shook": "shake",
    "shown": "show", "sat": "sit", "slept": "sleep",
    "spoke": "speak", "spoken": "speak", "spent": "spend",
    "stood": "stand", "stole": "steal", "stolen": "steal",
    "took": "take", "taken": "take", "taught": "teach", "thought": "think",
    "threw": "throw", "thrown": "throw", "told": "tell",
    "understood": "understand", "won": "win", "wore": "wear",
    "worn": "wear", "wrote": "write", "written": "write",
    "children": "child", "men": "man", "women": "woman", "people": "person",
    "teeth": "tooth", "feet": "foot", "mice": "mouse",
    "better": "good", "best": "good", "worse": "bad", "worst": "bad",
}

# Regex to split numbered dictionary meanings: "1 ... \n2 ..."
_MEANING_SPLIT_RE = re.compile(r"(?:^|\n)\s*\d+\s+")

# Regex to strip parenthetical context labels and examples.
# Matches: (handilsm.), (t.d. ...), (løgur), (tlm.), (um ...), etc.
_PAREN_RE = re.compile(r"\([^)]*\)")

# Regex to detect "special sections" to skip.
_SPECIAL_SECTION_RE = re.compile(r"^\[")


def _load_domain_terms() -> dict[str, dict[str, str]]:
    """Load domain terms from JSON file."""
    path = os.path.join(DATA_DIR, "domain_terms.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = _json.load(f)
        result: dict[str, dict[str, str]] = {}
        for domain_key, terms in raw.items():
            result[domain_key] = {
                k: v for k, v in terms.items() if not k.startswith("_")
            }
        return result
    except (FileNotFoundError, _json.JSONDecodeError):
        return {}


_DOMAIN_TERMS = _load_domain_terms()


_FAROESE_CHARS = set("ðøæáíóúý")
# English words commonly found in dictionary example phrases.
# Used to filter out inline examples like "regular customers føst viðskiftafólk".
_ENGLISH_COMMON = {
    # Function words
    "the", "is", "are", "was", "were", "his", "her", "its", "it",
    "for", "from", "with", "that", "this", "which", "where",
    "when", "what", "who", "how", "not", "but", "can", "will",
    "has", "have", "had", "been", "being", "does", "did",
    "he", "she", "they", "we", "you", "if", "or", "and",
    "an", "by", "on", "at", "to", "of", "in", "as", "so",
    "no", "up", "out", "do", "my", "me",
    # Content words frequent in dictionary examples
    "design", "rough", "regular", "customers", "customer", "act",
    "services", "windows", "service", "good", "bad", "old", "new",
    "need", "acute", "about", "one", "way", "like", "make",
    "keep", "get", "give", "take", "come", "go", "put", "set",
    "say", "see", "know", "think", "look", "want", "use",
}


def _looks_faroese(text: str) -> bool:
    """Heuristic: does this short text look like Faroese rather than English?

    For multi-word items: rejects if ANY word is in _ENGLISH_COMMON.
    For single words: accepts (many Faroese words are pure ASCII).
    """
    lower = text.lower()
    words = lower.split()

    # If any word is clearly English, reject the whole item
    if set(words) & _ENGLISH_COMMON:
        return False

    # Single word: accept (Faroese has many ASCII-compatible words)
    # Multi-word: accept (already filtered English common words above)
    return True


def _extract_translations(raw_entry: str) -> list[str]:
    """Parse a raw EN-FO dictionary entry and extract clean Faroese translations.

    Handles numbered meanings, context labels, examples, and special sections.
    Returns a deduplicated list of core Faroese translation words/phrases.
    """
    translations: list[str] = []
    seen: set[str] = set()

    # Split into numbered meanings (or treat whole entry as one if no numbers)
    parts = _MEANING_SPLIT_RE.split(raw_entry)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Skip special sections like "[í yms. samb.]"
        if _SPECIAL_SECTION_RE.match(part):
            continue

        # Remove all parenthetical content (context labels + examples)
        cleaned = _PAREN_RE.sub(",", part)  # Replace with comma to separate adjacent words

        # Remove bracket content too
        cleaned = re.sub(r"\[[^\]]*\]", "", cleaned)

        # Normalize whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        # Split by semicolons, then commas
        fragments = cleaned.split(";")

        for frag in fragments:
            frag = frag.strip()
            if not frag:
                continue

            for item in frag.split(","):
                item = item.strip()
                if not item:
                    continue

                # Strip leading numbers like "1 " or "2 "
                item = re.sub(r"^\d+\s+", "", item).strip()

                # Strip leading word-class abbreviations like "l ", "n ", "s "
                item = re.sub(r"^[a-z]\s+", "", item).strip()

                # Skip long phrases (likely examples, not translations)
                if len(item.split()) > 4:
                    continue

                item = item.strip(" /-")
                if not item or len(item) < 2:
                    continue

                # Skip items that look English
                if not _looks_faroese(item):
                    continue

                # Skip items containing "t.d.", "el.", "e.g." remnants
                if "t.d." in item or "e.g." in item:
                    continue

                lower = item.lower()
                if lower not in seen:
                    seen.add(lower)
                    translations.append(item)

                if len(translations) >= 6:
                    return translations

    return translations


def _resolve_en_base(word_lower: str) -> list[str]:
    """Return candidate base forms for an English word."""
    bases = [word_lower]
    if word_lower in _EN_IRREGULARS:
        bases.append(_EN_IRREGULARS[word_lower])
    for suffix, replacement in _EN_SUFFIXES:
        if word_lower.endswith(suffix) and len(word_lower) > len(suffix) + 1:
            bases.append(word_lower[:-len(suffix)] + replacement)
    return bases


def _batch_translate_lookup(
    conn: sqlite3.Connection, candidates: list[str],
) -> dict[str, list[dict]]:
    """Batch-lookup translations for a list of English source words/phrases.

    Returns dict mapping lowercase source -> list of {target_word, target_word_id}.
    """
    result: dict[str, list[dict]] = {}
    unique = list({c.lower() for c in candidates})

    for batch_start in range(0, len(unique), 400):
        batch = unique[batch_start:batch_start + 400]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT id, source_word, target_word, target_word_id "
            f"FROM translations WHERE LOWER(source_word) IN ({placeholders}) "
            f"ORDER BY id",
            batch,
        ).fetchall()

        for r in rows:
            key = r["source_word"].lower()
            if key not in result:
                result[key] = []
            if len(result[key]) >= 5:
                continue
            result[key].append({
                "target_word": r["target_word"] or "",
                "target_word_id": r["target_word_id"],
            })

    return result


# ── Spell-check constants ──────────────────────────────────────────────────

_WORD_RE = re.compile(r"[A-Za-zÁáÍíÓóÚúÝýÆæØøÐð]+(?:-[A-Za-zÁáÍíÓóÚúÝýÆæØøÐð]+)*")

_ACCENT_MAP: dict[str, list[str]] = {
    "a": ["á"], "i": ["í"], "o": ["ó", "ø"], "u": ["ú"],
    "y": ["ý"], "d": ["ð"], "e": ["æ"],
}
_DEACCENT_MAP: dict[str, str] = {
    "á": "a", "í": "i", "ó": "o", "ú": "u", "ý": "y", "ð": "d", "æ": "e", "ø": "o",
}
_LINKING_ELEMENTS = ["", "a", "s", "u", "ar", "na", "ra", "ir"]


def _tokenize(text: str) -> list[tuple[str, int]]:
    """Extract word tokens with their positions from text."""
    return [(m.group(), m.start()) for m in _WORD_RE.finditer(text)]


def _should_skip(token: str) -> bool:
    """Skip single chars and all-uppercase abbreviations (KT, AI, USA)."""
    if len(token) <= 1:
        return True
    if token.isupper() and len(token) <= 6:
        return True
    return False


def _is_known_word(conn: sqlite3.Connection, word: str) -> bool:
    """Check if word exists as a headword or inflected form."""
    row = conn.execute(
        "SELECT 1 FROM words WHERE search_word = ? COLLATE NOCASE LIMIT 1", (word,)
    ).fetchone()
    if row:
        return True
    row = conn.execute(
        "SELECT 1 FROM inflected_forms WHERE form = ? COLLATE NOCASE LIMIT 1", (word,)
    ).fetchone()
    return row is not None


def _batch_check_words(conn: sqlite3.Connection, tokens: list[str]) -> set[str]:
    """Return set of tokens that are NOT in the database. Batches of 400."""
    unknown = set()
    lower_list = list({t.lower() for t in tokens})

    for batch_start in range(0, len(lower_list), 400):
        batch = lower_list[batch_start:batch_start + 400]
        placeholders = ",".join("?" for _ in batch)

        # Check headwords
        rows = conn.execute(
            f"SELECT LOWER(search_word) AS w FROM words WHERE LOWER(search_word) IN ({placeholders})",
            batch,
        ).fetchall()
        found = {r["w"] for r in rows}

        remaining = [t for t in batch if t not in found]
        if remaining:
            placeholders2 = ",".join("?" for _ in remaining)
            rows2 = conn.execute(
                f"SELECT DISTINCT LOWER(form) AS w FROM inflected_forms WHERE LOWER(form) IN ({placeholders2})",
                remaining,
            ).fetchall()
            found.update(r["w"] for r in rows2)

        unknown.update(t for t in batch if t not in found)

    # Return original-case tokens that are unknown
    return {t for t in tokens if t.lower() in unknown}


def _suggest_accent_variants(conn: sqlite3.Connection, word: str) -> list[str]:
    """Try swapping unaccented chars for accented equivalents and vice versa."""
    suggestions = []
    lower = word.lower()
    for i, ch in enumerate(lower):
        variants = []
        if ch in _ACCENT_MAP:
            variants = _ACCENT_MAP[ch]
        elif ch in _DEACCENT_MAP:
            variants = [_DEACCENT_MAP[ch]]
        for v in variants:
            candidate = lower[:i] + v + lower[i + 1:]
            if _is_known_word(conn, candidate):
                suggestions.append(candidate)
    return suggestions


def _suggest_compound_splits(conn: sqlite3.Connection, word: str) -> list[dict]:
    """Try splitting compound words and checking both parts.

    Uses a cache to avoid redundant DB lookups for the same substrings.
    """
    if len(word) < 8:
        return []
    splits = []
    lower = word.lower()
    # Cache known-word lookups within this call
    known_cache: dict[str, bool] = {}

    def is_known_cached(w: str) -> bool:
        if w not in known_cache:
            known_cache[w] = _is_known_word(conn, w)
        return known_cache[w]

    def is_headword_cached(w: str) -> bool:
        key = f"hw:{w}"
        if key not in known_cache:
            known_cache[key] = conn.execute(
                "SELECT 1 FROM words WHERE search_word = ? COLLATE NOCASE LIMIT 1", (w,)
            ).fetchone() is not None
        return known_cache[key]

    for split_pos in range(4, len(lower) - 3):
        left = lower[:split_pos]
        right = lower[split_pos:]
        for link in _LINKING_ELEMENTS:
            if link and not left.endswith(link):
                continue
            base_left = left[:-len(link)] if link else left
            left_known = is_known_cached(left) or (link and is_known_cached(base_left))
            if not left_known:
                continue
            if is_headword_cached(right):
                splits.append({"left": left, "link": link, "right": right})
                if len(splits) >= 5:
                    return splits
    return splits


def _suggest_by_prefix(conn: sqlite3.Connection, word: str) -> list[str]:
    """Find similar words by progressively shorter prefixes."""
    lower = word.lower()
    for prefix_len in range(len(lower), max(2, len(lower) - 3) - 1, -1):
        prefix = lower[:prefix_len]
        rows = conn.execute(
            "SELECT search_word FROM words WHERE search_word LIKE ? COLLATE NOCASE ORDER BY ABS(LENGTH(search_word) - ?) LIMIT 5",
            (f"{prefix}%", len(word)),
        ).fetchall()
        if rows:
            return [r["search_word"] for r in rows]
    return []


def _escape_like(s: str) -> str:
    """Escape LIKE wildcards in user input."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ── Semantic search globals (numpy + sentence-transformers lazy-loaded) ────

_EMBEDDINGS_LOADED = False
_EMBEDDING_MATRIX = None  # np.ndarray, lazy
_EMBEDDING_IDS: list[int] | None = None
_SENTENCE_MODEL = None


def _load_embeddings(conn: sqlite3.Connection) -> bool:
    """Load all embeddings into memory. Returns True if embeddings are available."""
    global _EMBEDDINGS_LOADED, _EMBEDDING_MATRIX, _EMBEDDING_IDS
    if _EMBEDDINGS_LOADED:
        return _EMBEDDING_MATRIX is not None
    import numpy as np
    _EMBEDDINGS_LOADED = True
    ids = []
    vecs = []
    for row in conn.execute("SELECT word_id, embedding FROM word_embeddings"):
        ids.append(row["word_id"])
        vecs.append(np.frombuffer(row["embedding"], dtype=np.float32).copy())
    if not ids:
        return False
    _EMBEDDING_IDS = ids
    _EMBEDDING_MATRIX = np.stack(vecs)
    norms = np.linalg.norm(_EMBEDDING_MATRIX, axis=1, keepdims=True)
    norms[norms == 0] = 1
    _EMBEDDING_MATRIX = _EMBEDDING_MATRIX / norms
    return True


def _get_sentence_model():
    """Lazy-load the sentence-transformers model."""
    global _SENTENCE_MODEL
    if _SENTENCE_MODEL is None:
        from sentence_transformers import SentenceTransformer
        _SENTENCE_MODEL = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    return _SENTENCE_MODEL


def _semantic_search(conn: sqlite3.Connection, query_embedding, limit: int = 10) -> list[tuple[int, float]]:
    """Find most similar words by cosine similarity."""
    import numpy as np
    if not _load_embeddings(conn):
        return []
    q_norm = query_embedding / (np.linalg.norm(query_embedding) or 1)
    similarities = _EMBEDDING_MATRIX @ q_norm
    top_indices = np.argsort(similarities)[-limit:][::-1]
    return [(_EMBEDDING_IDS[i], float(similarities[i])) for i in top_indices]


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


def format_word_result(conn: sqlite3.Connection, word_row: sqlite3.Row) -> dict[str, Any]:
    """Build a full word result with inflections and references."""
    w = row_to_dict(word_row)
    word_id = w["id"]

    # Get inflected forms
    inflections = conn.execute(
        "SELECT form_index, form FROM inflected_forms WHERE word_id = ? ORDER BY form_index",
        (word_id,),
    ).fetchall()
    forms = [r["form"] for r in inflections]
    w["inflected_forms"] = forms
    w["labeled_inflections"] = _label_inflections(forms)

    # Get cross-references
    refs = conn.execute(
        "SELECT referenced_word FROM word_references WHERE word_id = ?",
        (word_id,),
    ).fetchall()
    w["references"] = [r["referenced_word"] for r in refs]

    return w


# ── Tools ──────────────────────────────────────────────────────────────────


@mcp.tool()
def lookup_word(word: str) -> dict[str, Any]:
    """Look up a Faroese word by exact headword match. Returns definition, grammar class,
    all inflected forms (case/number/definiteness paradigm), and cross-references.

    Args:
        word: The Faroese headword to look up (e.g. "bilur", "genta", "fara")
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
                return {"error": f"Word '{word}' not found", "suggestion": "Try search_words() for prefix search"}

        results = [format_word_result(conn, r) for r in rows]
    return {"count": len(results), "results": results}








def _is_sentence_start(text: str, pos: int) -> bool:
    """Check if a token at `pos` is at the start of a sentence."""
    if pos == 0:
        return True
    before = text[:pos].rstrip()
    if not before:
        return True
    return before[-1] in ".!?:\n"


def _find_loanword_alternatives(
    conn: sqlite3.Connection, words: list[str],
) -> dict[str, list[str]]:
    """Check if unknown words are English loanwords by looking them up as
    source_word in the EN-FO translations table (dictionary_id=3).
    Returns dict mapping lowercase word -> list of Faroese alternatives."""
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='translations'"
    ).fetchone()
    if not table_check:
        return {}

    result: dict[str, list[str]] = {}
    for batch_start in range(0, len(words), 400):
        batch = words[batch_start : batch_start + 400]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT source_word, target_word FROM translations "
            f"WHERE dictionary_id = 3 AND LOWER(source_word) IN ({placeholders})",
            batch,
        ).fetchall()
        for r in rows:
            key = r["source_word"].lower()
            alternatives = _extract_translations(r["target_word"])
            if not alternatives:
                continue
            if key not in result:
                result[key] = alternatives
            else:
                seen = {a.lower() for a in result[key]}
                for alt in alternatives:
                    if alt.lower() not in seen:
                        result[key].append(alt)
                        seen.add(alt.lower())
    return {k: v[:6] for k, v in result.items()}


@mcp.tool()
def review(text: str) -> dict[str, Any]:
    """Complete Faroese text review — spelling + grammar in ONE call.
    Checks: spelling (local DB + GiellaLT speller), grammar (local rules +
    GiellaLT grammar), preposition+case, gender agreement, word confusion.
    Run this AFTER writing, before presenting text.

    Args:
        text: Faroese text to review
    """
    if not text or not text.strip():
        return {"spelling": [], "grammar": [], "total_issues": 0}

    spelling_issues: list[dict[str, Any]] = []
    grammar_issues: list[dict[str, Any]] = []
    seen_words: set[str] = set()

    # ── SPELLING: local DB + GiellaLT speller ──
    with get_db() as conn:
        tokens = _tokenize(text)
        all_parts = []
        for token, pos in tokens:
            if _should_skip(token):
                continue
            all_parts.append(token)

        unique = list(set(all_parts))
        unknown = _batch_check_words(conn, unique)

        # Cross-check unknown words against HFST analyser — if HFST knows
        # the word it's valid Faroese even if not in our DB (e.g. inflected forms)
        hfst_verified: set[str] = set()
        if _ANALYSER and _HFST_LOOKUP:
            unknown_list = list(unknown)
            if unknown_list:
                hfst_results = _hfst_analyse_batch(unknown_list)
                for w in unknown_list:
                    if hfst_results.get(w):
                        hfst_verified.add(w)

        for word in unknown:
            if word in hfst_verified:
                continue  # HFST recognises it — not a spelling error
            entry: dict[str, Any] = {"word": word, "source": "local_db"}
            suggestions = _suggest_by_prefix(conn, word)
            if suggestions:
                entry["suggestions"] = suggestions[:5]
            spelling_issues.append(entry)
            seen_words.add(word.lower())

    giellalt_spell = _giellalt_spellcheck(text)
    if giellalt_spell and "results" in giellalt_spell:
        for r in giellalt_spell["results"]:
            if not r.get("is_correct", True):
                word = r["word"]
                if word.lower() not in seen_words:
                    entry = {"word": word, "source": "giellalt_speller"}
                    suggestions = r.get("suggestions", [])
                    if suggestions:
                        entry["suggestions"] = [s["value"] for s in suggestions[:5]]
                    spelling_issues.append(entry)
                    seen_words.add(word.lower())

    # ── GRAMMAR: HFST+CG3 pipeline (primary) + local checks (fallback) ──
    if _ANALYSER and _VISLCG3 and _DISAMBIGUATOR:
        parsed = _analyse_text_full(text)
        hfst_issues = _check_agreement_hfst(parsed)
        for issue in hfst_issues:
            grammar_issues.append(issue)
            seen_words.add(issue.get("word", "").lower())

    _do_grammar_checks(text, grammar_issues, seen_words)

    # GiellaLT grammar API
    giellalt_gram = _giellalt_gramcheck(text)
    if giellalt_gram is not None:
        for err in giellalt_gram.get("errs", []):
            word = ""
            message = ""
            suggestions: list = []
            if isinstance(err, dict):
                word = err.get("error_text", "")
                message = err.get("description", "")
                suggestions = err.get("suggestions", [])
            elif isinstance(err, list) and len(err) >= 3:
                word = err[0]
                message = err[2] if len(err) > 2 else ""
                suggestions = err[1] if isinstance(err[1], list) else []
            if word and word.lower() not in seen_words:
                grammar_issues.append({
                    "source": "giellalt_grammar", "word": word,
                    "message": message, "suggestions": suggestions,
                })
                seen_words.add(word.lower())

    total = len(spelling_issues) + len(grammar_issues)
    return {
        "spelling": spelling_issues,
        "grammar": grammar_issues,
        "total_issues": total,
    }


_PREPOSITION_CASE: dict[str, str] = {
    # Accusative
    "til": "acc", "um": "acc", "gjøgnum": "acc", "eftir": "acc",
    "uttan": "acc", "vegna": "acc", "millum": "acc", "innan": "acc",
    "umkring": "acc",
    # Dative
    "av": "dat", "frá": "dat", "hjá": "dat", "móti": "dat",
    "undan": "dat", "úr": "dat",
    # Both (dative for location, accusative for motion)
    "á": "acc_or_dat", "í": "acc_or_dat", "við": "dat",
    "fyri": "acc_or_dat", "yvir": "acc_or_dat", "undir": "acc_or_dat",
}

# Noun form_index → case mapping (for 16-form nouns)
_NOUN_INDEX_CASE = {
    0: "nom", 1: "acc", 2: "dat", 3: "gen",
    4: "nom", 5: "acc", 6: "dat", 7: "gen",
    8: "nom", 9: "acc", 10: "dat", 11: "gen",
    12: "nom", 13: "acc", 14: "dat", 15: "gen",
}


def _identify_token(conn: sqlite3.Connection, token: str) -> list[dict]:
    """Identify what a token could be: verb, noun, adjective, preposition, etc."""
    results = []
    lower = token.lower()

    # Check if it's a preposition
    if lower in _PREPOSITION_CASE:
        results.append({"type": "preposition", "word": lower, "case": _PREPOSITION_CASE[lower]})

    # Check inflected forms
    rows = conn.execute("""
        SELECT DISTINCT w.id, w.search_word, w.word_class_code, inf.form_index
        FROM inflected_forms inf
        JOIN words w ON w.id = inf.word_id
        WHERE inf.form = ? COLLATE NOCASE
        LIMIT 10
    """, (token,)).fetchall()

    for r in rows:
        wc = r["word_class_code"] or ""
        entry = {
            "type": "verb" if wc == "s" else "noun" if wc in ("k", "kv", "h") else "adjective" if wc == "l" else wc,
            "headword": r["search_word"],
            "word_class": wc,
            "form_index": r["form_index"],
        }
        if wc in ("k", "kv", "h") and r["form_index"] < 16:
            entry["case"] = _NOUN_INDEX_CASE.get(r["form_index"], "?")
        results.append(entry)

    # Also check as headword
    rows2 = conn.execute(
        "SELECT search_word, word_class_code FROM words WHERE search_word = ? COLLATE NOCASE LIMIT 5",
        (token,),
    ).fetchall()
    for r in rows2:
        wc = r["word_class_code"] or ""
        if not any(x.get("headword") == r["search_word"] and x.get("type") == "verb" for x in results):
            results.append({
                "type": "verb" if wc == "s" else "noun" if wc in ("k", "kv", "h") else wc,
                "headword": r["search_word"],
                "word_class": wc,
                "form_index": -1,
            })

    return results


_PRONOUN_GENDER = {
    "hann": "m", "hon": "f", "tað": "n",
    "teir": "m_pl", "tær": "f_pl", "tey": "n_pl",
    "eg": None, "tú": None, "vit": "pl", "tit": "pl",
}

_SINGULAR_PRONOUNS = {"eg", "tú", "hann", "hon", "tað"}
_PLURAL_PRONOUNS = {"vit", "tit", "teir", "tær", "tey"}

# Adjective form_index → gender for 24-form adjectives
_ADJ_INDEX_GENDER = {
    0: "m", 1: "m", 2: "m", 3: "m", 4: "m", 5: "m", 6: "m", 7: "m",
    8: "f", 9: "f", 10: "f", 11: "f", 12: "f", 13: "f", 14: "f", 15: "f",
    16: "n", 17: "n", 18: "n", 19: "n", 20: "n", 21: "n", 22: "n", 23: "n",
}

_WORD_CLASS_GENDER = {"k": "m", "kv": "f", "h": "n"}
_GENDER_LABEL = {"m": "masculine", "f": "feminine", "n": "neuter"}

_DETERMINER_GENDER: dict[str, str] = {
    "hvønn": "m", "hvørja": "f", "hvørt": "n", "hvat": "n",
    "hasin": "m", "hasi": "n", "hasa": "f",
    "hesin": "m", "hesi": "n", "hesa": "f",
    "tann": "m", "ta": "f", "tað": "n",
    "nakran": "m", "nakra": "f", "nakað": "n",
    "ongan": "m", "onga": "f", "onki": "n",
    "allan": "m", "alla": "f", "alt": "n",
}

_WORD_BLACKLIST: dict[str, str] = {
    "skjá": "skíggi (k2) — 'skjá' is Icelandic, not Faroese",
}


def _do_grammar_checks(text: str, grammar_issues: list, seen_words: set) -> None:
    """Run local grammar checks on text."""
    tokens_with_pos = _tokenize(text)
    if tokens_with_pos:
        tokens = [t for t, _ in tokens_with_pos]
        lower_tokens = [t.lower() for t in tokens]

        with get_db() as conn:
            identified: list[list[dict]] = []
            for tok in tokens:
                identified.append(_identify_token(conn, tok))

            for i, (tok, ids) in enumerate(zip(tokens, identified)):
                lower = tok.lower()

                # Unknown verb forms
                if not ids and len(tok) > 3:
                    if lower.endswith(("aði", "ði", "di", "ti", "du", "tu")):
                        grammar_issues.append({
                            "source": "local", "type": "unknown_verb_form", "word": tok,
                            "message": f"'{tok}' not found. Check conjugation.",
                        })

                # Preposition + noun case
                prep_ids = [x for x in ids if x["type"] == "preposition"]
                if prep_ids and i + 1 < len(tokens):
                    prep = prep_ids[0]
                    required = prep["case"]
                    for j in range(i + 1, min(i + 4, len(tokens))):
                        if tokens[j][0].isupper() and j > i + 1:
                            break
                        nouns = [x for x in identified[j] if x["type"] == "noun" and "case" in x]
                        if nouns:
                            for noun in nouns:
                                if required == "acc" and noun["case"] == "dat":
                                    grammar_issues.append({
                                        "source": "local", "type": "preposition_case", "word": tokens[j],
                                        "message": f"'{prep['word']}' + acc, but '{tokens[j]}' is dat.",
                                    })
                                elif required == "dat" and noun["case"] == "acc":
                                    grammar_issues.append({
                                        "source": "local", "type": "preposition_case", "word": tokens[j],
                                        "message": f"'{prep['word']}' + dat, but '{tokens[j]}' is acc.",
                                    })
                            break

                # Article + noun gender
                if lower in ("ein", "eitt", "eina") and i + 1 < len(tokens):
                    next_nouns = [x for x in identified[i + 1] if x["type"] == "noun"]
                    if not next_nouns and i + 2 < len(tokens):
                        next_nouns = [x for x in identified[i + 2] if x["type"] == "noun"]
                    for nn in next_nouns:
                        wc = nn.get("word_class", "")
                        if lower == "ein" and wc == "h":
                            grammar_issues.append({
                                "source": "local", "type": "article_gender",
                                "word": f"{tok} {tokens[i+1]}",
                                "message": f"'{nn['headword']}' is neuter — use 'eitt'.",
                            })
                        elif lower == "eitt" and wc in ("k", "kv"):
                            grammar_issues.append({
                                "source": "local", "type": "article_gender",
                                "word": f"{tok} {tokens[i+1]}",
                                "message": f"'{nn['headword']}' is {wc} — use 'ein'/'eina'.",
                            })
                        break

                # Subject + var/er + adjective gender (pronoun or noun subject)
                if lower in ("var", "er") and i >= 1 and i + 1 < len(tokens):
                    prev = lower_tokens[i - 1]
                    gender = _PRONOUN_GENDER.get(prev)
                    # If not a pronoun, check if previous token is a noun
                    if gender is None:
                        prev_nouns = [x for x in identified[i - 1] if x["type"] == "noun"]
                        if prev_nouns:
                            gender = _WORD_CLASS_GENDER.get(prev_nouns[0].get("word_class", ""))
                    if gender in ("m", "f", "n"):
                        next_adjs = [x for x in identified[i + 1]
                                     if x.get("type") == "adjective" and x.get("form_index", -1) >= 0]
                        for adj in next_adjs:
                            adj_gender = _ADJ_INDEX_GENDER.get(adj["form_index"])
                            if adj_gender and adj_gender != gender:
                                grammar_issues.append({
                                    "source": "local", "type": "adjective_gender",
                                    "word": tokens[i + 1],
                                    "message": f"'{tokens[i-1]} {lower} {tokens[i+1]}' — subject is {_GENDER_LABEL[gender]}, adj is {_GENDER_LABEL.get(adj_gender, '?')}.",
                                })
                            break

                # "einki" + plural verb
                if lower == "einki":
                    for j in range(i + 1, min(i + 4, len(tokens))):
                        if lower_tokens[j] in ("vóru", "eru", "kundu", "skuldu", "vildu", "høvdu"):
                            grammar_issues.append({
                                "source": "local", "type": "number_agreement", "word": tokens[j],
                                "message": f"'einki' + singular verb. '{tokens[j]}' is plural.",
                            })
                            break

                # "tú" + 3sg verb
                if lower == "tú":
                    for j in (i - 1, i + 1):
                        if 0 <= j < len(tokens):
                            v = [x for x in identified[j] if x["type"] == "verb" and x["form_index"] == 1]
                            if v:
                                grammar_issues.append({
                                    "source": "local", "type": "verb_person", "word": tokens[j],
                                    "message": f"'{tokens[j]}' with 'tú' — 3sg of '{v[0]['headword']}'. Check 2sg.",
                                })
                                break

                # "keypti seg" → sær
                if lower in ("keypti", "keypi") and i + 1 < len(tokens) and lower_tokens[i + 1] == "seg":
                    grammar_issues.append({
                        "source": "local", "type": "reflexive_case", "word": "seg",
                        "message": "'keypti seg' → 'keypti sær'.",
                    })

                # hyggja/hugsa confusion
                if lower in ("hugdi", "hugdu") and i + 1 < len(tokens) and lower_tokens[i + 1] == "um":
                    grammar_issues.append({
                        "source": "local", "type": "word_confusion", "word": tok,
                        "message": f"'{tok} um' — 'hugdi' = looked. 'hugsaði um' = thought.",
                    })

                # Determiner/pronoun + noun gender agreement
                det_gender = _DETERMINER_GENDER.get(lower)
                if det_gender and i + 1 < len(tokens):
                    for j in range(i + 1, min(i + 4, len(tokens))):
                        nns = [x for x in identified[j] if x["type"] == "noun"]
                        if nns:
                            nn = nns[0]
                            noun_gender = _WORD_CLASS_GENDER.get(nn.get("word_class", ""))
                            if noun_gender and noun_gender != det_gender:
                                grammar_issues.append({
                                    "source": "local", "type": "determiner_gender",
                                    "word": f"{tok} {tokens[j]}",
                                    "message": f"'{tok}' is {_GENDER_LABEL[det_gender]}, but '{nn['headword']}' is {_GENDER_LABEL[noun_gender]}.",
                                })
                            break
                        # skip adjectives between determiner and noun
                        adjs = [x for x in identified[j] if x.get("type") == "adjective"]
                        if not adjs:
                            break

                # Adjective + noun gender/case agreement
                adj_ids = [x for x in ids if x.get("type") == "adjective" and x.get("form_index", -1) >= 0]
                if adj_ids and i + 1 < len(tokens):
                    for j in range(i + 1, min(i + 3, len(tokens))):
                        nns = [x for x in identified[j] if x["type"] == "noun"]
                        if nns:
                            noun = nns[0]
                            noun_gender = _WORD_CLASS_GENDER.get(noun.get("word_class", ""))
                            noun_fi = noun.get("form_index", -1)
                            if noun_gender and noun_fi >= 0:
                                # noun case_offset: position within 8-slot block (sg+pl, nom/acc/dat/gen)
                                noun_case_offset = noun_fi % 8
                                # Check if any adj form matches both gender AND case
                                matched = False
                                for a in adj_ids:
                                    ag = _ADJ_INDEX_GENDER.get(a["form_index"])
                                    a_case_offset = a["form_index"] % 8
                                    if ag == noun_gender and a_case_offset == noun_case_offset:
                                        matched = True
                                        break
                                if not matched:
                                    # Also check second noun interpretation (e.g. nom vs acc)
                                    if len(nns) > 1:
                                        n2 = nns[1]
                                        n2_fi = n2.get("form_index", -1)
                                        if n2_fi >= 0:
                                            n2_offset = n2_fi % 8
                                            for a in adj_ids:
                                                ag = _ADJ_INDEX_GENDER.get(a["form_index"])
                                                if ag == noun_gender and a["form_index"] % 8 == n2_offset:
                                                    matched = True
                                                    break
                                if not matched:
                                    grammar_issues.append({
                                        "source": "local", "type": "adjective_noun_gender",
                                        "word": f"{tok} {tokens[j]}",
                                        "message": f"'{tok}' doesn't agree with '{noun['headword']}' ({_GENDER_LABEL[noun_gender]}).",
                                    })
                            break
                        break  # only look at immediate next token

                # Blacklisted words
                if lower in _WORD_BLACKLIST:
                    grammar_issues.append({
                        "source": "local", "type": "wrong_word", "word": tok,
                        "message": f"'{tok}' — {_WORD_BLACKLIST[lower]}",
                    })


@mcp.tool()
def translate_text(text: str, domain: str = "") -> dict[str, Any]:
    """Translate English text to Faroese. Returns a clean deduplicated glossary
    with parsed translations, grammar info, and domain terminology.
    Handles text of any length. Use this for translating sentences or paragraphs.

    Args:
        text: English text to translate (no length limit)
        domain: Optional domain for terminology (e.g. "KT" for tech/IT terms)
    """
    tokens = [m.group() for m in _EN_WORD_RE.finditer(text)]
    if not tokens:
        return {"error": "No words found in text."}

    lower_tokens = [t.lower() for t in tokens]

    # Build domain lookup
    domain_map: dict[str, str] = {}
    if domain and domain.upper() in _DOMAIN_TERMS:
        domain_map = _DOMAIN_TERMS[domain.upper()]

    # Collect unique content words (skip stop words) + their base forms
    unique_words: dict[str, str] = {}  # lower -> original
    for tok, lower in zip(tokens, lower_tokens):
        if lower in _ENGLISH_STOP_WORDS or lower in unique_words:
            continue
        if lower in domain_map:
            continue
        unique_words[lower] = tok

    # Build candidate set: each unique word + its base forms
    candidates: set[str] = set()
    base_map: dict[str, str] = {}  # maps base form -> original lower word
    for lower in unique_words:
        bases = _resolve_en_base(lower)
        for b in bases:
            candidates.add(b)
            if b != lower:
                base_map[b] = lower

    # Also collect n-gram phrases (2-4 words) for phrase matching
    phrase_candidates: set[str] = set()
    for i in range(len(lower_tokens)):
        for n in range(2, min(5, len(lower_tokens) - i + 1)):
            phrase = " ".join(lower_tokens[i:i + n])
            if phrase in domain_map:
                continue
            phrase_candidates.add(phrase)

    all_candidates = list(candidates | phrase_candidates)

    with get_db() as conn:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='translations'"
        ).fetchone()
        if not table_check:
            return {"error": "Translations table not found. Run import_db.py with EN-FO data first."}

        trans_map = _batch_translate_lookup(conn, all_candidates)

        # Build glossary: deduplicated, parsed, with grammar
        glossary: list[dict[str, Any]] = []
        matched_words: set[str] = set()
        unmatched: list[str] = []

        # 1) Multi-word phrases first
        for phrase in sorted(phrase_candidates, key=lambda p: -len(p.split())):
            if phrase in trans_map:
                entries = trans_map[phrase]
                all_translations: list[str] = []
                for e in entries:
                    all_translations.extend(_extract_translations(e["target_word"]))
                if all_translations:
                    glossary.append({"source": phrase, "translations": all_translations[:6], "phrase": True})
                    for w in phrase.split():
                        matched_words.add(w)

        # 2) Single content words
        word_ids_to_fetch: list[tuple[int, int]] = []  # (glossary_index, word_id)

        for lower, original in unique_words.items():
            if lower in matched_words:
                continue

            # Collect hits from exact match + all base forms, merge
            hits: list[dict] = []
            if lower in trans_map:
                hits.extend(trans_map[lower])
            for base in _resolve_en_base(lower):
                if base != lower and base in trans_map:
                    hits.extend(trans_map[base])

            if hits:
                all_translations: list[str] = []
                first_word_id = None
                for e in hits:
                    all_translations.extend(_extract_translations(e["target_word"]))
                    if not first_word_id and e.get("target_word_id"):
                        first_word_id = e["target_word_id"]

                if all_translations:
                    entry: dict[str, Any] = {
                        "source": lower,
                        "translations": all_translations[:6],
                    }
                    if first_word_id:
                        idx = len(glossary)
                        word_ids_to_fetch.append((idx, first_word_id))
                    glossary.append(entry)
                    matched_words.add(lower)
                else:
                    unmatched.append(original)
            else:
                unmatched.append(original)

        # Batch-fetch grammar info for all word_ids
        if word_ids_to_fetch:
            ids = [wid for _, wid in word_ids_to_fetch]
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id, word_class_code, short_inflected_form "
                f"FROM words WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
            grammar_by_id = {r["id"]: r for r in rows}

            for idx, wid in word_ids_to_fetch:
                g = grammar_by_id.get(wid)
                if g:
                    if g["word_class_code"]:
                        glossary[idx]["word_class"] = g["word_class_code"]
                    if g["short_inflected_form"]:
                        glossary[idx]["inflection"] = g["short_inflected_form"]

    # Build domain overrides (only terms actually present in the text as whole words)
    domain_applied: dict[str, str] = {}
    if domain_map:
        text_lower = text.lower()
        for en_term, fo_term in domain_map.items():
            # Use word-boundary matching to avoid false positives
            # ("ai" matching in "tailored", etc.)
            if re.search(r"\b" + re.escape(en_term) + r"\b", text_lower):
                domain_applied[en_term] = fo_term

    result: dict[str, Any] = {
        "glossary": glossary,
        "unmatched": unmatched,
    }
    if domain_applied:
        result["domain"] = domain.upper()
        result["domain_terms"] = domain_applied
    result["note"] = (
        "Glossary of Faroese translations extracted from the EN-FO dictionary. "
        "Compose natural Faroese using these as reference — adapt grammar, "
        "word order, and register. Domain terms override dictionary entries."
    )
    return result




_REGISTER_MARKERS = {
    "(sj.)": "rare/uncommon",
    "(gl.)": "archaic/old",
    "(kvæð.)": "poetic/literary",
    "(bíbl.)": "biblical",
    "(stb.)": "dialectal",
    "(Suð.)": "Southern dialect",
}


def _try_tag_variations(lemma: str, pos: str, tags: str) -> str | None:
    """Try common tag format variations for HFST generation."""
    if pos == "N":
        alt = f"{lemma}+{pos}+{tags.replace('+Indef', '').replace('+Def', '')}"
        r = _hfst_generate(alt)
        if r:
            return r
    elif pos == "V":
        if "Ind" not in tags and "Imp" not in tags:
            r = _hfst_generate(f"{lemma}+V+Ind+{tags}")
            if r:
                return r
        if "Imp" in tags and "Sg" not in tags:
            r = _hfst_generate(f"{lemma}+V+Imp+Sg")
            if r:
                return r
    elif pos == "A":
        if "Comp" not in tags and "Superl" not in tags:
            r = _hfst_generate(f"{lemma}+A+Pos+{tags}")
            if r:
                return r
    return None


@mcp.tool()
def generate_form(lemma: str, pos: str, tags: str) -> dict[str, Any]:
    """Generate correct inflected forms of Faroese words using GiellaLT HFST.
    Use BEFORE writing to get correct forms. Supports batch mode.

    Args:
        lemma: Base form OR multiple forms separated by semicolons.
            Single: "keypmaður"
            Batch: "keypmaður;vænta;vakur"
        pos: Part of speech (matched 1:1 with lemmas).
            Single: "N"
            Batch: "N;V;A"
        tags: Morphological tags (matched 1:1 with lemmas).
            Single: "Msc+Pl+Nom+Indef"
            Batch: "Msc+Pl+Nom+Indef;Ind+Prt+3Sg;Fem+Sg+Nom"
            Tags: Nouns: Msc/Fem/Neu+Sg/Pl+Nom/Acc/Dat/Gen+Def/Indef
                  Verbs: Ind+Prs/Prt+1Sg/2Sg/3Sg, Imp, PrfPtc
                  Adj: Msc/Fem/Neu+Sg/Pl+Nom/Acc/Dat/Gen
    """
    # Handle batch mode
    lemmas = lemma.split(";")
    poses = pos.split(";")
    taglist = tags.split(";")

    if len(lemmas) > 1:
        # Pad pos/tags if shorter
        while len(poses) < len(lemmas):
            poses.append(poses[-1])
        while len(taglist) < len(lemmas):
            taglist.append(taglist[-1])

        results = []
        for l, p, t in zip(lemmas, poses, taglist):
            l, p, t = l.strip(), p.strip(), t.strip()
            analysis = f"{l}+{p}+{t}"
            form = _hfst_generate(analysis)
            if not form:
                form = _try_tag_variations(l, p, t)
            results.append({"lemma": l, "form": form, "analysis": analysis})
        return {"batch": True, "results": results}

    # Single mode
    analysis = f"{lemma}+{pos}+{tags}"
    result = _hfst_generate(analysis)
    if not result:
        result = _try_tag_variations(lemma, pos, tags)

    if result:
        return {"lemma": lemma, "analysis": analysis, "generated_form": result}

    return {
        "lemma": lemma, "analysis": analysis, "generated_form": None,
        "hint": "Nouns: N+Msc/Fem/Neu+Sg/Pl+Nom/Acc/Dat/Gen+Def/Indef. Verbs: V+Ind+Prs/Prt+1Sg/2Sg/3Sg. Adj: A+Msc/Fem/Neu+Sg/Pl+Nom/Acc/Dat/Gen",
    }


@mcp.tool()
def check_register(word: str) -> dict[str, Any]:
    """Check if a Faroese word is formal, rare, archaic, or dialectal.
    Uses dictionary logic — no manual word lists needed:
    1. Register markers: (sj.)=rare, (gl.)=archaic, (kvæð.)=poetic, (stb.)=dialectal
    2. Redirect entries: "sí X" = X is the preferred/standard form
    3. Single-synonym definitions: short explanation pointing to another word
    4. FO-EN presence + usage frequency in other definitions

    Args:
        word: Faroese word to check
    """
    result: dict[str, Any] = {"word": word}

    with get_db() as conn:
        rows = conn.execute(
            "SELECT search_word, explanation_text FROM words WHERE search_word = ? COLLATE NOCASE",
            (word,),
        ).fetchall()

        if not rows:
            result["found"] = False
            return result

        result["found"] = True
        explanation = rows[0]["explanation_text"] or ""

        # 1. Check "sí X" redirects — X is the preferred form
        redirect_match = re.match(r"^sí\s+(.+)$", explanation.strip())
        if redirect_match:
            target = redirect_match.group(1).strip()
            result["redirect_to"] = target
            result["recommendation"] = f"'{word}' redirects to '{target}'. Use '{target}' instead."
            return result

        # 2. Check register markers — only flag if marker appears near the START
        #    of the explanation (first 80 chars). Markers deep in the text are
        #    about specific sub-meanings or examples, not the whole word.
        start_text = explanation[:80] if explanation else ""
        markers_found = []
        for marker, label in _REGISTER_MARKERS.items():
            if marker in start_text:
                markers_found.append({"marker": marker, "register": label})
        if markers_found:
            result["register_warnings"] = markers_found

        # 3. Check single-synonym definitions (implicit redirect to preferred form)
        stripped = explanation.strip()
        # Pattern: explanation is just one word (= this is a variant, that word is standard)
        if (len(stripped) < 30
                and "," not in stripped
                and "(" not in stripped
                and " " not in stripped.strip()
                and len(stripped) > 1
                and not stripped.startswith("sí")):
            result["preferred_form"] = stripped

        # 4. Extract leading synonyms from explanation
        #    Pattern: "synonym1, synonym2, definition text..."
        synonym_match = re.match(
            r"^([a-záíóúýæøð]+(?:\s*,\s*[a-záíóúýæøð]+)*)",
            stripped, re.IGNORECASE,
        )
        if synonym_match and not re.match(r"^\d", stripped):
            raw_synonyms = synonym_match.group(1)
            synonyms = [s.strip() for s in raw_synonyms.split(",") if s.strip().lower() != word.lower()]
            if synonyms and len(synonyms[0]) > 1:
                result["synonyms"] = synonyms[:5]

        # 5. FO-EN dictionary presence (common words are in FO-EN)
        fo_en = conn.execute(
            "SELECT COUNT(*) as cnt FROM translations WHERE source_word = ? COLLATE NOCASE AND dictionary_id = 2",
            (word,),
        ).fetchone()
        result["in_fo_en_dictionary"] = fo_en["cnt"] > 0

        # 6. Usage frequency: how often this word appears in other definitions
        usage = conn.execute(
            "SELECT COUNT(*) as cnt FROM words WHERE explanation_text LIKE ? AND search_word != ? COLLATE NOCASE",
            (f"% {word} %", word),
        ).fetchone()
        result["used_in_definitions"] = usage["cnt"]

        # Build recommendation
        if markers_found:
            labels = ", ".join(m["register"] for m in markers_found)
            rec = f"Word is marked as: {labels}."
            if result.get("synonyms"):
                rec += f" Consider: {', '.join(result['synonyms'][:3])}"
            result["recommendation"] = rec
        elif result.get("preferred_form"):
            result["recommendation"] = f"'{word}' is a variant. Preferred form: '{result['preferred_form']}'"
        else:
            result["register"] = "standard"

    return result


@mcp.tool()
def grammar_reference(query: str) -> dict[str, Any]:
    """Look up Faroese grammar rules. Query by topic to get conjugation tables,
    declension patterns, pronoun forms, etc.

    Query topics:
    - "verb fara" → full conjugation (all persons, present + past)
    - "pronouns" or "pronoun eg" → personal pronouns in all cases
    - "auxiliary vera" → conjugation of vera/hava/verða/blíva
    - "adjective strong/weak" → adjective endings
    - "prepositions" → all prepositions with case government
    - "preposition til" → which case "til" governs + examples
    - "weak verbs" / "strong verbs" → verb class overviews
    - "preterite-present" → kunna, skula, mega, vita, vilja, munna
    - "word order" → V2 rule, questions, subordinate clauses
    - "articles" → definite articles (tann, ta, tað...)

    Args:
        query: Grammar topic to look up
    """
    rules = _get_grammar_rules()
    if not rules:
        return {"error": "Grammar rules file not found."}

    q = query.lower().strip()
    result: dict[str, Any] = {"query": query}

    # Verb conjugation lookup
    if q.startswith("verb "):
        verb = q[5:].strip()
        # Check auxiliaries
        if verb in rules.get("auxiliary_verbs", {}):
            result["type"] = "auxiliary_verb"
            result["conjugation"] = rules["auxiliary_verbs"][verb]
            return result
        # Check preterite-present
        if verb in rules.get("preterite_present_verbs", {}):
            result["type"] = "preterite_present_verb"
            result["conjugation"] = rules["preterite_present_verbs"][verb]
            return result
        # Look up in DB to determine verb class, then show pattern
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM words WHERE search_word = ? COLLATE NOCASE AND word_class_code = 's'",
                (verb,),
            ).fetchall()
            if rows:
                w = row_to_dict(rows[0])
                inflections = conn.execute(
                    "SELECT form_index, form FROM inflected_forms WHERE word_id = ? ORDER BY form_index",
                    (w["id"],),
                ).fetchall()
                forms = {r["form_index"]: r["form"] for r in inflections}
                result["type"] = "verb"
                result["word"] = w["search_word"]
                result["inflection_class"] = w["inflection_class"]
                result["grammar_raw"] = w["grammar_raw"]
                result["db_forms"] = {
                    "infinitive": forms.get(0, "?"),
                    "3sg_present": forms.get(1, "?"),
                    "sg_past": forms.get(2, "?"),
                    "pl_past": forms.get(3, "?"),
                    "supine": forms.get(4, "?"),
                    "past_participle_m_nom": forms.get(5, "?"),
                }
                # Add person ending rules
                result["person_endings"] = {
                    "present": {
                        "1sg": "stem + -i (e.g. far-i, bít-i, kall-i)",
                        "2sg": "varies by class (see 3sg or add -t/-st/-ur)",
                        "3sg": forms.get(1, "?"),
                        "plural": forms.get(0, "?") + " (= infinitive form)",
                    },
                    "past": {
                        "1sg": forms.get(2, "?") + " (= sg past)",
                        "2sg": forms.get(2, "?") + "st/t (add -st or -t)",
                        "3sg": forms.get(2, "?") + " (= sg past)",
                        "plural": forms.get(3, "?") + " (= pl past)",
                    },
                }
                result["weak_verb_classes"] = rules.get("weak_verb_classes", {})
                result["strong_verb_classes_overview"] = {
                    k: v.get("example", "") for k, v in rules.get("strong_verb_classes", {}).items()
                    if not k.startswith("_") and "example" in v
                }
                return result
        result["error"] = f"Verb '{verb}' not found in database"
        return result

    # Pronoun lookup
    if "pronoun" in q:
        result["type"] = "pronouns"
        result["personal_pronouns"] = rules.get("personal_pronouns", {})
        if any(p in q for p in ["eg", "1st", "fyrst"]):
            result["focus"] = {
                "singular": rules["personal_pronouns"]["singular"]["1st"],
                "plural": rules["personal_pronouns"]["plural"]["1st"],
            }
        elif any(p in q for p in ["tú", "2nd", "annar"]):
            result["focus"] = {
                "singular": rules["personal_pronouns"]["singular"]["2nd"],
                "plural": rules["personal_pronouns"]["plural"]["2nd"],
            }
        return result

    # Auxiliary verbs
    if "auxiliary" in q or "hjálpar" in q:
        result["type"] = "auxiliary_verbs"
        result["verbs"] = rules.get("auxiliary_verbs", {})
        return result

    # Preterite-present verbs
    if "preterite" in q or "tátíðar" in q:
        result["type"] = "preterite_present_verbs"
        result["verbs"] = rules.get("preterite_present_verbs", {})
        return result

    # Weak verbs overview
    if "weak verb" in q or "veik" in q:
        result["type"] = "weak_verb_classes"
        result["classes"] = rules.get("weak_verb_classes", {})
        return result

    # Strong verbs overview
    if "strong verb" in q or "sterk" in q:
        result["type"] = "strong_verb_classes"
        result["classes"] = rules.get("strong_verb_classes", {})
        return result

    # Adjective declension
    if "adjective" in q or "lýsing" in q:
        result["type"] = "adjective_declension"
        result["declension"] = rules.get("adjective_declension", {})
        return result

    # Articles
    if "article" in q or "kenniorð" in q:
        result["type"] = "definite_articles"
        result["articles"] = rules.get("definite_articles", {})
        return result

    # Interrogative pronouns
    if "interrogat" in q or "spurnar" in q:
        result["type"] = "interrogative_pronouns"
        result["pronouns"] = rules.get("interrogative_pronouns", {})
        return result

    # Prepositions
    if "preposition" in q or "fyriseting" in q:
        result["type"] = "prepositions"
        preps = rules.get("prepositions", {})
        # Check if asking about a specific preposition
        for case_group in ["accusative", "dative", "accusative_or_dative", "genitive"]:
            group = preps.get(case_group, {})
            for prep, data in group.items():
                if prep.startswith("_"):
                    continue
                if prep in q:
                    result["preposition"] = prep
                    result["case"] = case_group
                    result["details"] = data
                    return result
        result["all_prepositions"] = preps
        return result

    # Specific preposition lookup (without "preposition" keyword)
    preps = rules.get("prepositions", {})
    for case_group in ["accusative", "dative", "accusative_or_dative", "genitive"]:
        group = preps.get(case_group, {})
        for prep, data in group.items():
            if prep.startswith("_"):
                continue
            if q == prep or q == f"prep {prep}":
                result["type"] = "preposition"
                result["preposition"] = prep
                result["case"] = case_group
                result["details"] = data
                return result

    # Word order
    if "word order" in q or "orðaröð" in q or "v2" in q:
        result["type"] = "word_order"
        result["rules"] = rules.get("word_order", {})
        return result

    # Imperative
    if "imperative" in q or "boðsháttur" in q:
        result["type"] = "imperative"
        result["rules"] = rules.get("imperative", {})
        return result

    # Common mistakes
    if "mistake" in q or "feil" in q or "common" in q:
        result["type"] = "common_mistakes"
        result["mistakes"] = rules.get("common_mistakes", {})
        return result

    # Faroese terminology for modern concepts
    if "terminolog" in q or "vitlíki" in q or "ai" in q.split() or "kt" in q.split():
        result["type"] = "faroese_terminology"
        result["terms"] = rules.get("faroese_terminology", {})
        return result

    # Default: return all available topics
    result["type"] = "help"
    result["available_topics"] = [
        "verb <word> — conjugation of a specific verb",
        "pronouns — all personal pronouns in all cases",
        "auxiliary vera/hava/verða/blíva — auxiliary verb conjugation",
        "preterite-present — kunna, skula, mega, vita, vilja, munna",
        "weak verbs — 4 classes of weak verb endings",
        "strong verbs — 7 classes of strong verb patterns",
        "adjective strong — strong (indefinite) adjective endings",
        "adjective weak — weak (definite) adjective endings",
        "articles — definite articles (tann, ta, tað...)",
        "interrogative — question pronouns (hvør, hvat...)",
        "prepositions — all prepositions with case government",
        "preposition til/á/í/við/... — specific preposition case + examples",
        "word order — V2 rule, questions, subordinate clauses",
        "imperative — how to form imperative (hoyr! far! kom!)",
        "common mistakes — gender agreement, weak forms, preposition case, etc.",
        "terminology — vitlíki (AI), KT (IT), snildur (smart for things)",
    ]
    return result




def main():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}. Run import_db.py first.", file=sys.stderr)
        sys.exit(1)

    transport = "stdio"
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        if idx + 1 < len(sys.argv):
            transport = sys.argv[idx + 1]

    log.info(f"Starting Faroese Dictionary MCP server (transport={transport}, db={DB_PATH})")
    log.info(f"Analyser: {'found' if _ANALYSER else 'NOT FOUND'}  Generator: {'found' if _GENERATOR else 'NOT FOUND'}")

    if transport == "http":
        import uvicorn
        mcp_app = mcp.streamable_http_app()
        uvicorn.run(mcp_app, host="0.0.0.0", port=8080)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
