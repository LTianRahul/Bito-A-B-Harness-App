# Running the A/B Benchmark in Docker

This is the fastest way to try the benchmark: no zip file, no Python/Node setup —
just Docker. Everything else (Claude Code auth, Bito connect, prompts, running) is
done in the browser, exactly like the [non-Docker guide](README.md), just launched
differently.

---

## Step 1 — Start the app

**Requires:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or
Docker Engine + Compose on Linux) installed and running.

```bash
docker run -d --name ab-harness \
  -p 8765:8765 \
  -v harness-data:/data \
  -v harness-claude-home:/root/.claude \
  -v harness-gh-config:/root/.config \
  ghcr.io/ltianrahul/bito-ab-harness-app:latest
```

Or, if you have this repo's `docker-compose.yml` (it does the same three volumes for you):

```bash
docker compose up -d
```

Open **http://localhost:8765** — everything from here on happens in the browser.

> **Why three volumes?** `/data` holds your results, Bito connection, and prompts.
> `/root/.claude` and `/root/.config` hold Bito Skills and any `gh`/`glab` sign-in.
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

Same as the non-Docker flow — **Setup** tab → **Bito AI Architect MCP** card → enter
your Workspace ID → **Connect**. The browser OAuth popup works fine through the
mapped port; nothing about this step changes for Docker.

---

## Step 4 — Install Bito Skills

The Setup page shows the same one-line installer as always, but it has to run
**inside the container** (skills install to `~/.claude/skills`, which is the
container's filesystem, not your host's):

```bash
docker exec -it ab-harness bash -c "curl -fsSL https://mcp-setup.bito.ai/install.sh | bash"
```

Then reload the Setup page — no restart needed.

---

## Step 5 — Git hosting credentials (only if you use "fresh-clone" workspace mode)

Arm A clones repos itself via `gh`/`glab`, so it needs one of them authenticated
*inside the container*:

```bash
docker exec -it ab-harness gh auth login
```

This is the device-code flow — it prints a URL and a code; open the URL in **any**
browser on your machine and enter the code. No extra port needs to be published for
this (unlike Claude Code's OAuth login).

Only `gh` (GitHub) ships in the image today — `glab`'s official installer needs
`sudo`, which the slim base image doesn't have. If you need GitLab repos, install it
yourself once: `docker exec -it ab-harness bash -c "apt-get update && apt-get install -y sudo && curl -fsSL https://gitlab.com/gitlab-org/cli/-/raw/main/scripts/install.sh | bash"`.

**Using "local-repo" workspace mode instead?** You don't need this step at all — see
[below](#testing-against-a-local-repo-instead-of-cloning).

---

## Step 6 & 7 — Prompts and Run

Identical to the non-Docker flow — see [Steps 5–7 of the main guide](README.md).

---

## Everyday use

- **Stop:** `docker compose down` (add `-v` only if you want to wipe everything — see below).
- **Start again:** `docker compose up -d`. Everything you set up is still there.
- **Upgrade to a newer image:** `docker compose pull && docker compose up -d`. Your
  volumes (results, auth, skills) carry over.
- **Full reset:** `docker compose down -v` — deletes all three volumes, back to a
  fresh machine.
- **View logs:** `docker compose logs -f`.

### Testing against a local repo instead of cloning

If you'd rather run against a checkout you already have than have Arm A clone it,
mount it into the container and point the Run tab's **local-repo** workspace mode at
the mounted path:

```bash
docker run -d --name ab-harness \
  -p 8765:8765 \
  -v harness-data:/data \
  -v harness-claude-home:/root/.claude \
  -v harness-gh-config:/root/.config \
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

---

## What's different from running it directly (not in Docker)

- **Claude Code auth** is an API key pasted in the Setup page, not `claude`/`/login`
  in a terminal (see Step 2's rationale above).
- **Bito Skills and git-hosting CLI login** happen via `docker exec`, not a plain
  terminal, since they install into the container's filesystem.
- **`glab` isn't preinstalled** (see Step 5). `gh`, `git`, `node`, and `claude` are.
- Everything else — the Setup checklist, Bito OAuth connect, Prompts, Run, Results —
  is exactly the same app, unchanged.
