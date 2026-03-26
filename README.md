# Sprotan — Faroese Language MCP Server

A Model Context Protocol (MCP) server for Faroese language tools. Provides morphological analysis, form generation, grammar verification, dictionary lookup, and translation — powered by GiellaLT HFST and a 67,000-word dictionary from sprotin.fo.

## Tools

| Tool | What it does |
|---|---|
| `generate_form` | Generate correct inflected forms (batch supported) |
| `verify_text` | Verify grammar using HFST morphological analyser |
| `lookup_word` | Dictionary lookup with definitions and inflections |
| `translate_text` | English → Faroese translation with domain terms |
| `check_register` | Check if a word is standard, formal, or archaic |
| `grammar_reference` | Grammar rules, conjugation tables, common mistakes |

## Quick Start (Docker)

```bash
docker build -t sprotan-mcp .
docker run -it sprotan-mcp python3 mcp_server.py
```

For remote HTTP access:
```bash
docker run -p 8080:8080 sprotan-mcp python3 mcp_server.py --transport http
```

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│   LLM       │────▶│  MCP Server      │────▶│  GiellaLT   │
│ (Claude,    │     │  (6 tools)       │     │  HFST       │
│  OpenAI,    │◀────│                  │◀────│  Analyser + │
│  Gemini)    │     │  SQLite DB       │     │  Generator  │
└─────────────┘     │  (67k words)     │     └─────────────┘
                    └──────────────────┘
```

- **HFST Analyser**: `keypmenn` → `keypmaður+N+Msc+Pl+Nom+Indef`
- **HFST Generator**: `keypmaður+N+Msc+Pl+Nom+Indef` → `keypmenn`
- **verify_text** uses the analyser to check every word for gender/case/number agreement

## Building the Database

The SQLite database is not included in the repo (too large). To build it:

```bash
# Scrape dictionaries from sprotin.fo
python scrape_sprotin.py          # FO-FO (67k words)
python scrape_sprotin.py en-fo    # EN-FO (78k translations)
python scrape_sprotin.py fo-en    # FO-EN (79k translations)

# Import into SQLite
python import_db.py
```

## Data Sources

- **sprotin.fo** — Faroese-Faroese dictionary (67,487 words)
- **sprotin.fo EN-FO** — English-Faroese translations (78,897 entries)
- **sprotin.fo FO-EN** — Faroese-English translations (79,308 entries)
- **GiellaLT lang-fao** — Morphological analyser and generator built by UiT Tromsø and Fróðskaparsetur Føroya

## License

Dictionary data from sprotin.fo. GiellaLT tools under GPL. MCP server code under MIT.
