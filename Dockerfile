# Faroese MCP Server — complete language toolkit
# Includes: HFST morphological analyser + generator, CG3 grammar checker,
# SQLite dictionary (67k words), and MCP server.

FROM ubuntu:24.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg git autoconf automake libtool \
    pkg-config build-essential bc gawk python3 lsb-release icu-devtools \
    && rm -rf /var/lib/apt/lists/*

# Install HFST + CG3 via apertium nightly repo
RUN curl -sS http://apertium.projectjj.com/apt/install-nightly.sh | bash
RUN apt-get update && apt-get install -y --no-install-recommends \
    hfst vislcg3 \
    && rm -rf /var/lib/apt/lists/*

# Install GiellaLT tools needed for build
RUN apt-get update && apt-get install -y --no-install-recommends pipx python3-venv && rm -rf /var/lib/apt/lists/*
RUN pipx install git+https://github.com/divvun/GiellaLTGramTools/ && \
    pipx install git+https://github.com/divvun/GiellaLTLexTools/ && \
    pipx install git+https://github.com/divvun/morph-test && \
    pipx ensurepath
ENV PATH="/root/.local/bin:$PATH"

# Clone and build lang-fao (Faroese morphological toolkit)
RUN git clone --depth 1 https://github.com/giellalt/lang-fao.git /opt/lang-fao
WORKDIR /opt/lang-fao
RUN TERM=dumb ./autogen.sh
RUN ./configure --enable-analysers --enable-generators
RUN make -j$(nproc)

# Collect built transducers
RUN mkdir -p /opt/transducers && \
    find /opt/lang-fao -name "*.hfstol" -exec cp {} /opt/transducers/ \; && \
    find /opt/lang-fao -name "*.pmhfst" -exec cp {} /opt/transducers/ \; && \
    ls -la /opt/transducers/

# ── Runtime image ─────────────────────────────────────────────────────────
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg lsb-release python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*
RUN curl -sS http://apertium.projectjj.com/apt/install-nightly.sh | bash
RUN apt-get update && apt-get install -y --no-install-recommends \
    hfst vislcg3 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir --break-system-packages mcp>=1.0.0

# Copy transducers from builder
COPY --from=builder /opt/transducers/ /app/tools/giellalt/

# Copy application files
COPY mcp_server.py /app/
COPY db/sprotin.db /app/db/
COPY data/domain_terms.json /app/data/
COPY data/faroese_grammar_rules.json /app/data/
COPY data/paradigm_labels.json /app/data/

WORKDIR /app

# Verify the build
RUN echo "keypmaður+N+Msc+Pl+Nom+Indef" | hfst-optimized-lookup -q /app/tools/giellalt/generator-*-norm.hfstol || true
RUN ls -la /app/tools/giellalt/

EXPOSE 8080

# Default: stdio transport (for Claude Code)
# Override with --transport http for remote access
CMD ["python3", "mcp_server.py"]
