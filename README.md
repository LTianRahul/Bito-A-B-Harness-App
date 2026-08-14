# Bito A/B/C Benchmark Harness

Find out, **on your own codebase**, what [Bito AI Architect](https://bito.ai) actually
adds to your coding agent. This tool runs the same real engineering tasks three ways
and gives you a clear, side-by-side comparison:

| Arm | What it is |
|---|---|
| **A** | Your coding tool on its own — the baseline |
| **B** | Your tool **+ Bito AI Architect** (the indexed-codebase MCP) |
| **C** | Your tool **+ Bito MCP + Bito Skills** (full capability) |

You get quality, cost, speed, and token usage for each arm, plus a downloadable report.

---

## Prerequisites

Before you start, have these ready:

| Requirement | Why | Where to get it |
|---|---|---|
| **Docker Desktop** (Mac/Windows) or Docker Engine + Compose (Linux) | Runs the whole app in one container — no local Python/Node setup | [docker.com](https://www.docker.com/products/docker-desktop/) |
| **An Anthropic API key** | Lets the container run Claude Code headlessly, no browser login | [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) — this is a separate credential from a claude.ai subscription |
| **A Bito AI Architect workspace ID** | Arms B & C connect to your indexed repos through this | Your Bito account (self-hosted Bito? you'll use a bearer token instead — see Setup page) |
| **A GitHub or GitLab account** *(optional)* | Only needed if Arm A should clone repos itself | Skip this entirely if you'd rather point the benchmark at a repo already checked out on your machine (see "Workspace modes" below) |

That's it — no Node, no Python, no manual dependency installs. Everything else
(the `claude`/`gh`/`glab` CLIs, skills, MCP config) is either already baked into the
container or set up from the browser.

---

## Quick start

```bash
docker run -d --name ab-harness \
  -p 8765:8765 \
  -v harness-data:/data \
  -v harness-claude-home:/home/appuser/.claude \
  -v harness-gh-config:/home/appuser/.config \
  ghcr.io/ltianrahul/bito-ab-harness-app:latest
```

Then open **http://localhost:8765** — everything else happens in the browser.

*(Have this repo cloned instead? `docker compose up -d` does the same thing.)*

### Why three volumes?

| Volume | Holds |
|---|---|
| `harness-data` → `/data` | Results, Bito connection, your prompts, everything the app itself tracks |
| `harness-claude-home` → `/home/appuser/.claude` | Bito Skills (installed once, Step 4 below) |
| `harness-gh-config` → `/home/appuser/.config` | Any `gh`/`glab` sign-in (Step 5 below) |

All three survive container restarts **and** upgrades to a newer image — see
[Upgrading](#upgrading-to-a-newer-image) below.

---

## First-time setup (do these once, in order)

Everything after the `docker run` above happens on the **Setup** page in your browser
— no more terminal commands needed except where noted.

### 1. Add a Claude Code API key

Setup tab → **Claude Code — Anthropic API key** card → paste your key → **Save**.

This is the container's equivalent of `claude`'s usual `/login` step — a plain API key
works headlessly with zero browser sign-in, which is what makes this whole flow
possible inside a container.

### 2. Connect Bito AI Architect

Setup tab → **Bito AI Architect MCP** card → enter your **Workspace ID** → **Connect**
→ approve in the popup that opens. This is regular browser OAuth; it works the same
as it would outside a container.

*Self-hosted Bito?* Click "Self-hosted Bito instance?" and paste a bearer token
instead — no browser step needed.

### 3. Install Bito Skills

Arms B & C need these installed, and they live inside the container's own filesystem
— so this one step is a terminal command, run against the container you just started:

```bash
docker exec \
  -e MCP_URL="https://mcp.bito.ai/<YOUR_WORKSPACE_ID>/mcp" \
  -e USER_EMAIL="you@example.com" \
  ab-harness bash -c "curl -fsSL https://mcp-setup.bito.ai/install.sh | bash -s -- --non-interactive"
```

Reload the Setup page afterward — no restart needed, it detects the new skills live.
You should see **11 bito-\* skills installed**.

### 4. Authenticate git hosting *(only if Arm A should clone repos itself)*

```bash
docker exec -it ab-harness gh auth login
```

or, for GitLab:

```bash
docker exec -it ab-harness glab auth login --hostname gitlab.com --device --git-protocol https
```

Both print a one-time code and a URL — open the URL in **any** browser on your
machine, enter the code, done. Skip this entirely if you're using **local-repo**
workspace mode instead (see below) — no git auth needed there at all.

### 5. Add your prompts

**Prompts** tab → **Generate with AI** (uses your indexed Bito repos to draft
realistic tasks) or **+ Add prompt** to write your own.

### 6. Run your first A/B/C test

**Run** tab → keep all three arms checked → choose a workspace mode → **Start**.

### 7. See results

**Results** tab for quality/cost/speed per arm, **Leaderboard** for head-to-head
wins, **Reports** for a shareable Markdown summary.

---

## Workspace modes

| Mode | How it works | Needs git auth? |
|---|---|---|
| **fresh-clone** (default) | Each arm clones the target repo itself via `gh`/`glab` | Yes — see Step 4 above |
| **local-repo** | Points at a repo folder you mount into the container | No |

To use local-repo mode, add one more mount when starting the container:

```bash
docker run -d --name ab-harness \
  -p 8765:8765 \
  -v harness-data:/data \
  -v harness-claude-home:/home/appuser/.claude \
  -v harness-gh-config:/home/appuser/.config \
  -v /path/on/your/machine/my-repo:/workspace/my-repo:ro \
  ghcr.io/ltianrahul/bito-ab-harness-app:latest
```

then select `/workspace/my-repo` as the local repo path on the Run tab.

---

## Upgrading to a newer image

**Important:** `docker pull` only refreshes the cached image — it does **not**
touch a container that's already running. To actually pick up a new version:

```bash
docker pull ghcr.io/ltianrahul/bito-ab-harness-app:latest
docker rm -f ab-harness
docker run -d --name ab-harness \
  -p 8765:8765 \
  -v harness-data:/data \
  -v harness-claude-home:/home/appuser/.claude \
  -v harness-gh-config:/home/appuser/.config \
  ghcr.io/ltianrahul/bito-ab-harness-app:latest
```

Reusing the same volume names means nothing is lost — results, Bito connection,
skills, and git sign-in all carry over automatically.

*Using `docker compose` instead?* `docker compose pull && docker compose up -d`
does the recreate for you in one step.

---

## Good to know

- **This build supports Claude Code only** — Copilot, Cursor, Windsurf, and Cline
  are intentionally hidden from the Setup/Run pages, since their CLIs aren't
  installed in this image.
- **Everything runs as a non-root user** inside the container (`claude` itself
  refuses to run headlessly as root), and the container self-heals file
  ownership on every start — you don't need to think about this at all, but it's
  why the entrypoint does a bit of setup before the app actually starts.
- Want to run this **without Docker** instead (plain Python/Node on your machine)?
  See [docs/README.md](docs/README.md).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Ran `docker pull` but nothing changed | `pull` doesn't recreate a running container — see [Upgrading](#upgrading-to-a-newer-image) above. |
| `--dangerously-skip-permissions cannot be used with root/sudo privileges` on any run | You're on an image from before the non-root fix — pull the latest and recreate the container. |
| Bito Skills card shows 0 skills after running the installer | Same cause as above — check `docker exec ab-harness which jq` prints a path; if not, you're on an old image. |
| `glab auth login` fails / redirects to a `localhost` URL that won't connect | Use the `--device` form shown in Step 4 above, not plain `glab auth login`. |
| Container exits immediately, logs show `Permission denied` on `/data/...` | Pull the latest image — older ones didn't self-heal ownership on a volume from an even older, root-run image. |
| Port 8765 already in use | Something else is using it. Map a different host port: `-p 9000:8765` and open `http://localhost:9000` instead. |

For the full troubleshooting reference (every scenario above, plus more, with exact
symptoms and root causes) see **[docs/DOCKER.md](docs/DOCKER.md)**.
