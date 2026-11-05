# Realify DebugOps sandbox: runs untrusted repository code, scanners and tests.
# Contains NO credentials. Runs as non-root with a read-only root filesystem (enforced at run time).
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm
ARG TARGETARCH
ARG GITLEAKS_VERSION=8.30.1
ARG OSV_SCANNER_VERSION=2.7.0
ARG SEMGREP_VERSION=1.183.0
ARG CLAUDE_CODE_VERSION=2.1.318

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/tools/bin:$PATH SEMGREP_SEND_METRICS=off SEMGREP_ENABLE_VERSION_CHECK=0

RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates coreutils \
    && rm -rf /var/lib/apt/lists/*

# Node 22 + npm/npx
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Scanner binaries, verified against PINNED sha256 values (from each project's release checksum file at pin time).
# To upgrade: bump the version ARG and replace the matching sha256 ARGs.
ARG GITLEAKS_SHA256_ARM64=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080
ARG GITLEAKS_SHA256_AMD64=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
ARG OSV_SHA256_ARM64=4061ef4c021ed3c47f203b818f337e351336cc5cd67c0274fc36bd1dfc728dd7
ARG OSV_SHA256_AMD64=160c6253e2922e9e0a6a9771c5c4c40a4b3c6090b34a3cdd5e80c96cc2e4218b
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) GL=x64; OSV=amd64; GL_SHA=$GITLEAKS_SHA256_AMD64; OSV_SHA=$OSV_SHA256_AMD64 ;; \
      arm64) GL=arm64; OSV=arm64; GL_SHA=$GITLEAKS_SHA256_ARM64; OSV_SHA=$OSV_SHA256_ARM64 ;; \
      *) echo "unsupported arch"; exit 1 ;; esac; \
    cd /tmp; \
    curl -fsSLo gitleaks.tgz "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${GL}.tar.gz"; \
    echo "${GL_SHA}  gitleaks.tgz" | sha256sum -c -; \
    tar -xzf gitleaks.tgz gitleaks; install -m 0755 gitleaks /usr/local/bin/gitleaks; \
    curl -fsSLo osv-scanner "https://github.com/google/osv-scanner/releases/download/v${OSV_SCANNER_VERSION}/osv-scanner_linux_${OSV}"; \
    echo "${OSV_SHA}  osv-scanner" | sha256sum -c -; \
    install -m 0755 osv-scanner /usr/local/bin/osv-scanner; \
    rm -rf /tmp/*

# Semgrep in its own venv so it never collides with a project's dependencies; rules bundled for offline scans.
RUN python -m venv /opt/tools && /opt/tools/bin/pip install "semgrep==${SEMGREP_VERSION}" \
    && mkdir -p /opt/rules && curl -fsSL https://semgrep.dev/c/p/default -o /opt/rules/semgrep-default.yml \
    && head -c 200 /opt/rules/semgrep-default.yml | grep -q "rules"

# Claude Code CLI (agents). It reaches the Claude API only through the DebugOps llm-gateway with a per-run token.
RUN npm install -g --no-audit --no-fund "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && claude --version

RUN useradd --uid 10001 --create-home --home-dir /home/sandbox sandbox && mkdir -p /workspace && chown 10001:10001 /workspace
USER 10001
WORKDIR /workspace
CMD ["sleep", "infinity"]
