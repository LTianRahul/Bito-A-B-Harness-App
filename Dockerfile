# A/B Testing Benchmark Harness — container image.
#
# Ships the FastAPI backend + prebuilt React UI (frontend/dist is committed, so no
# Node build step is needed for the UI itself) plus everything the harness shells
# out to at run time: git, the GitHub/GitLab CLIs (Arm A repo access), and the
# Claude Code CLI (Node-based) that the benchmark actually drives.
#
# Auth is done entirely through the app's Setup page in the browser — see
# docs/DOCKER.md — so nothing secret is baked into this image.
FROM python:3.11-slim

# ---- OS packages: git + Node.js (for the claude CLI) + GitHub/GitLab CLIs + jq ----
# jq is required for a non-obvious reason: Bito's own Skills installer
# (mcp-setup.bito.ai/install.sh) needs jq to parse its skills manifest, and its
# fallback auto-install shells out to `sudo apt-get install jq` — this image has
# no `sudo` (it's already root). Without jq preinstalled, the installer silently
# skips copying any skill files while still reporting the MCP server as configured.
#
# glab isn't in Debian's apt repos and its official install script has the SAME
# missing-sudo problem as above, so it's installed from GitLab's own prebuilt
# .deb release instead (dpkg -i needs no sudo). Bump GLAB_VERSION to update.
ARG GLAB_VERSION=1.113.0
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg jq \
    && mkdir -p /etc/apt/keyrings \
    # Node.js 20.x (NodeSource) — claude CLI requires Node >= 18.
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    # GitHub CLI (gh) — official apt repo.
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs gh \
    && rm -rf /var/lib/apt/lists/* \
    && GLAB_ARCH="$(dpkg --print-architecture)" \
    && curl -fsSL "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/packages/generic/glab/${GLAB_VERSION}/glab_${GLAB_VERSION}_linux_${GLAB_ARCH}.deb" \
        -o /tmp/glab.deb \
    && dpkg -i /tmp/glab.deb && rm -f /tmp/glab.deb \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

# This build only supports Claude Code — hides Copilot/Cursor/Windsurf/Cline
# from the Setup/Run pages entirely (their CLIs aren't installed in this image
# anyway). Unset this to restore every tool; unaffected outside Docker.
ENV HARNESS_TOOLS=claude

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py harness.py prompts.example.json ./
COPY backend/ ./backend/
COPY frontend/dist/ ./frontend/dist/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Per-machine state (configs/, runs/, judgments/, reports/, results.db, prompt_sets/)
# is symlinked into /data by the entrypoint — never baked into the image. Customers
# mount ONE volume at /data (see docker-compose.yml).
VOLUME ["/data"]
EXPOSE 8765

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
