# Running the A/B Benchmark in Docker

This is the only supported way to run the benchmark: no zip file, no Python/Node
setup — just Docker. Everything else (Claude Code auth, Bito connect, prompts,
running) happens in the browser. See the repo's [top-level README](../README.md)
for the quick version of this same walkthrough.

> **Upgrading from before 2026-08-13?** The container used to run as root, which
> made `claude` hard-refuse every headless run with *"--dangerously-skip-permissions
> cannot be used with root/sudo privileges"* — nothing actually worked, not just one
> feature. It now runs as a non-root user, whose home moved from `/root` to
> `/home/appuser`. Recreate your container per "Upgrade to a newer image" below
> **and** update the two volume mount paths to `/home/appuser/.claude` and
> `/home/appuser/.config` (instead of `/root/...`) — reuse the **same** volume
> names you already have; the entrypoint fixes ownership on every start
> regardless of who wrote to them before, so your skills, `gh`/`glab` sign-in,
> results, and Bito connection all carry over with nothing to redo.

---

## Step 1 — Start the app

**Requires:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or
Docker Engine + Compose on Linux) installed and running.

```bash
docker run -d --name ab-harness \
  -p 8765:8765 \
  -v harness-data:/data \
  -v harness-claude-home:/home/appuser/.claude \
  -v harness-gh-config:/home/appuser/.config \
  ghcr.io/ltianrahul/bito-ab-harness-app:latest
```

Or, if you have this repo's `docker-compose.yml` (it does the same three volumes for you):

```bash
docker compose up -d
```

Open **http://localhost:8765** — everything from here on happens in the browser.

> **Why three volumes?** `/data` holds your results, Bito connection, and prompts.
> `/home/appuser/.claude` and `/home/appuser/.config` hold Bito Skills and any `gh`/`glab` sign-in.
> All three need to survive a `docker compose pull && docker compose up -d` (image
> upgrade), not just a restart — that's why they're separate named volumes instead
> of being thrown away with the container.

---

## Step 2 — Add a Claude Code API key

Go to the **Setup** tab → **Claude Code — Anthropic API key** card, paste a key from
[console.anthropic.com](https://console.anthropic.com/settings/keys), click **Save**.

This replaces the usual `claude` → `/login` terminal step. It has to — `/login`'s
browser OAuth needs a locally reachable callback port, which doesn't work cleanly
through a container, and on a Mac host the resulting token lives in the **Keychain**,
which a Linux container can't read anyway. The API key does the same job with zero
terminal steps.

*(Already have `ANTHROPIC_API_KEY` set some other way? Pass `-e ANTHROPIC_API_KEY=...`
on `docker run`, or set it in a `.env` file next to `docker-compose.yml` — the Setup
card will show "Configured" from the environment and skip asking.)*

---

## Step 3 — Connect Bito

**Setup** tab → **Bito AI Architect MCP** card → enter your Workspace ID →
**Connect**. The browser OAuth popup works fine through the mapped port — this
step is no different running in a container than anywhere else.

---

## Step 4 — Install Bito Skills

Skills install to `~/.claude/skills`, which is the **container's** filesystem, not
your host's — so this has to run inside the container, and needs `--non-interactive`
mode with the workspace/email passed as env vars (the plain interactive installer
the Setup page shows depends on a real TTY reading prompts from `/dev/tty`, which is
one more thing that can go wrong through `docker exec`; the non-interactive form is
deterministic and doesn't need `-it` at all):

```bash
docker exec \
  -e MCP_URL="https://mcp.bito.ai/<YOUR_WORKSPACE_ID>/mcp" \
  -e USER_EMAIL="you@example.com" \
  ab-harness bash -c "curl -fsSL https://mcp-setup.bito.ai/install.sh | bash -s -- --non-interactive"
```

Reload the Setup page — no restart needed, it detects the new skills live.

> **Self-hosted Bito?** Add `-e BEARER_TOKEN="..."` (omit it for hosted Bito's
> OAuth, which the installer sets up for you).

If you pulled the image before this was fixed, skills silently failed to install —
the Setup page would show the MCP connected but 0 skills, and `docker exec` output
would literally show `sudo: command not found`. That was Bito's installer script
trying `sudo apt-get install jq` to get a JSON parser it needs, and this image has
no `sudo` (it runs as root already). Current images ship `jq` and `glab` (see Step 5)
preinstalled, so this no longer happens — see "Upgrade to a newer image" under
Everyday Use below to actually get the fix (a plain `pull` isn't enough on its own).

---

## Step 5 — Git hosting credentials (only if you use "fresh-clone" workspace mode)

Arm A clones repos itself via `gh`/`glab` (both are preinstalled), so it needs one
of them authenticated *inside the container* — using each CLI's device-code flow,
which prints a one-time code and a static URL you open on **any** browser on your
machine, then polls for you to authorize. No extra port needs to be published for
this (unlike Claude Code's OAuth login):

```bash
docker exec -it ab-harness gh auth login
```

```bash
# glab's PLAIN `auth login` defaults to a browser-OAuth flow that needs a local
# callback port (http://localhost:7171/...) — that fails through docker exec, since
# nothing publishes that port. Use --device instead, which is the equivalent of
# gh's default: a one-time code + a URL with no local listener at all.
docker exec -it ab-harness glab auth login --hostname gitlab.com --device --git-protocol https
```

**Using "local-repo" workspace mode instead?** You don't need this step at all — see
[below](#testing-against-a-local-repo-instead-of-cloning).

---

## Step 6 — Set up your questions (prompts)

Go to the **Prompts** tab. It starts **empty** — you add the questions you want to test.

- **Easiest:** after you connect Bito (Step 3), click **Generate with AI** — it drafts
  questions based on your indexed repositories.
- **Manual:** click **+ Add prompt** and write your own. For ideas and the right shape,
  open the included `prompts.example.json` file in this repo — it has ready-made
  templates (replace the `<repo>` placeholders with your real repository names).

Aim your questions at code your Bito workspace has **indexed** — that's where Bito
helps most (architecture, cross-repo, "where is X", impact of a change, etc.).

---

## Step 7 — Run the benchmark

Go to the **Run** tab.

1. **Arms:** keep **A, B, C** checked (you need all three for a full comparison).
2. **Workspace mode:** **fresh-clone** (default, each arm clones the repo itself) or
   **local-repo** (test against a folder mounted into the container — see
   [Testing against a local repo](#testing-against-a-local-repo-instead-of-cloning)
   below).
3. Click **Start**. You'll see live progress — a full run takes a while, since it's
   making real model calls. You can **Stop** any time.

---

## Step 8 — See the results

- **Results / Scores** — quality, cost, time, and tokens per arm (defaults to your
  latest run). Higher quality at **lower cost and time** is the Bito win.
- **Leaderboard** — who won on cost, speed, quality, and more.
- **Reports** — a clean side-by-side summary you can **download as Markdown** to share.

---

## Everyday use

**If you started with `docker compose up -d`:**

- **Stop:** `docker compose down` (add `-v` only if you want to wipe everything — see below).
- **Start again:** `docker compose up -d`. Everything you set up is still there.
- **Upgrade to a newer image:** `docker compose pull && docker compose up -d`. Your
  volumes (results, auth, skills) carry over.
- **Full reset:** `docker compose down -v` — deletes all three volumes, back to a
  fresh machine.
- **View logs:** `docker compose logs -f`.

**If you started with the plain `docker run` command from Step 1** (no
`docker-compose.yml`): `docker pull` on its own does **not** touch the running
container — it only refreshes the local image cache. You have to remove the old
container and start a new one from the freshly pulled image; as long as you reuse
the same volume names, nothing is lost:

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

- **Stop:** `docker stop ab-harness`. **Start again:** `docker start ab-harness`.
- **Full reset:** add `docker volume rm harness-data harness-claude-home harness-gh-config`
  after removing the container.
- **View logs:** `docker logs -f ab-harness`.
- **Check which image you're actually running:** `docker inspect ab-harness --format '{{.Image}}'`
  vs. `docker images --digests ghcr.io/ltianrahul/bito-ab-harness-app` — if they don't
  match, you're still on the old container; do the pull-and-recreate above.

### Testing against a local repo instead of cloning

If you'd rather run against a checkout you already have than have Arm A clone it,
mount it into the container and point the Run tab's **local-repo** workspace mode at
the mounted path:

```bash
docker run -d --name ab-harness \
  -p 8765:8765 \
  -v harness-data:/data \
  -v harness-claude-home:/home/appuser/.claude \
  -v harness-gh-config:/home/appuser/.config \
  -v /path/on/your/machine/my-repo:/workspace/my-repo:ro \
  ghcr.io/ltianrahul/bito-ab-harness-app:latest
```

Then use `/workspace/my-repo` as the local repo path in the Run tab. This needs no
git credentials in the container at all.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| **"port is already allocated"** | Something else on your machine (or another container) is using 8765. Either stop it, or change the mapping: `-p 9000:8765` and open `http://localhost:9000`. |
| **Setup page shows the Claude Code step incomplete after saving a key** | Reload the page — the Setup checklist polls every ~12s but a manual reload is instant. |
| **Skills/git-login disappeared after an upgrade** | You're missing the `harness-claude-home` / `harness-gh-config` volumes — see the `docker run` command in Step 1. Re-run Steps 4/5 once; from then on they'll persist across upgrades. |
| **`docker exec -it ab-harness ...` says "no such container"** | You used `docker compose` — the container name is `<project>-ab-harness-1`. Run `docker compose exec ab-harness ...` instead (no container name needed). |
| **Page won't load** | `docker compose ps` to confirm it's running; `docker compose logs` for errors. |
| **Bito Skills card shows 0 skills / "no skills found" even after running the installer** | Same underlying cause as the row below — you're most likely still on the old container. If `docker exec` output literally shows `sudo: command not found`, that confirms it. |
| **Ran `docker pull` (or `docker compose pull`) but nothing changed — still see old bugs / Copilot still showing** | `pull` only refreshes the cached image; it doesn't touch the container that's already running. See "Upgrade to a newer image" under Everyday Use above for the exact remove-and-recreate steps for your setup (compose vs. plain `docker run`). |
| **`glab auth login` fails with `xdg-open: executable file not found`, then the pasted authorize URL redirects to `localhost:7171/...` and the browser can't connect** | You ran plain `glab auth login`, whose default flow needs a local callback port that isn't published. Use `glab auth login --hostname gitlab.com --device --git-protocol https` instead (Step 5) — no port needed. |
| **Everything (Generate prompts with AI, any Run) fails with `--dangerously-skip-permissions cannot be used with root/sudo privileges`** | You're on an image from before the container ran as non-root — see the upgrading note at the top of this doc. Recreate the container from the latest image, updating your two `.claude`/`.config` volume paths to `/home/appuser/...`. |
| **Container exits immediately; `docker logs ab-harness` shows `Permission denied` on `/data/results.db` or similar** | You're on an image from before the entrypoint self-healed volume ownership on every start (fixed same day as the non-root change above). Pull the latest image and recreate — no need to touch the volume itself, the fix chowns it automatically on next start. |

---

## What's different from running it directly (not in Docker)

- **Claude Code auth** is an API key pasted in the Setup page, not `claude`/`/login`
  in a terminal (see Step 2's rationale above).
- **Bito Skills and git-hosting CLI login** happen via `docker exec`, not a plain
  terminal, since they install into the container's filesystem.
- **This build only offers Claude Code** — Copilot/Cursor/Windsurf/Cline are hidden
  from the Setup/Run pages since their CLIs aren't installed in the image.
- Everything else — the Setup checklist, Bito OAuth connect, Prompts, Run, Results —
  is exactly the same app, unchanged.
