# A/B Benchmark — User Guide

This tool shows you, **on your own codebase**, what **Bito AI Architect** adds to your
coding agent: it runs the same questions three ways and compares the answers for
**quality, cost, and speed**.

- **Arm A** — your coding tool on its own
- **Arm B** — your tool **+ Bito AI Architect** (the indexed-codebase MCP)
- **Arm C** — your tool **+ Bito MCP + Bito skills**

You don't need to be technical. You run **one file**, then click through a short setup
in your browser. This guide walks through every step.

---

## Step 1 — Start the app

Open the unzipped `ab-benchmark` folder and:

| Your computer | What to do |
|---|---|
| **Mac** | Double-click **`start.command`** |
| **Windows** | Double-click **`start.bat`** |
| **Linux** | Open a terminal in the folder and run **`./scripts/start.sh`** |

A black window opens and sets things up automatically (this takes a minute or two the
first time, and needs internet). When it's ready it opens your browser at
**http://127.0.0.1:8765**. Leave the black window open while you use the tool.

> **On a Mac, if double-clicking is blocked:** right-click `start.command` → **Open** →
> **Open** (you only need to do this once). If it opens in a text editor instead of
> running, right-click → Open With → Terminal.

> The window installs anything missing (Python, Node, the Claude CLI). If it can't, it
> prints the exact thing to install — follow that, then start again.

---

## Step 2 — Sign in to Claude Code (one time)

The benchmark drives the **Claude Code** command-line tool, so it needs to be signed in.

1. Open a **new** terminal window.
2. Type `claude` and press Enter.
3. Type `/login` and press Enter, then finish signing in in your browser.

You only do this once per computer. (The start window installs the Claude tool for you;
this step just signs it in.)

---

## Step 3 — Connect Bito (in the app, one time)

In the browser, go to the **Setup** tab.

- **If you already use Bito in Claude Code:** it's detected automatically — you'll see
  **"Connected — reusing your existing Bito"** and a green **MCP reachable** badge.
  Nothing to do.
- **If not connected yet:** type your **Bito Workspace ID** in the box and click
  **Connect with Bito MCP**, then approve in the browser.
  - *Self-hosted Bito?* Click **"Self-hosted Bito instance?"** and paste your token instead.

Then click **Run health check** to confirm Bito answers. You can **Disconnect** any time
from the same card.

> Don't have a Workspace ID? Get it from your Bito account, then paste it here.

---

## Step 4 — Install Bito Skills (one time)

Arms B and C need the Bito skills. On the **Setup** tab, under **Bito Skills**:

- Click **Install Skills** (one click — it handles everything), **or**
- Run the one-line installer it shows, then restart the app:
  - **Mac/Linux:** `curl -fsSL https://mcp-setup.bito.ai/install.sh | bash`
  - **Windows:** `irm https://mcp-setup.bito.ai/install.ps1 | iex`

When it's done you'll see **"skills installed — Arms B & C are ready."**

---

## Step 5 — Set up your questions (prompts)

Go to the **Prompts** tab. It starts **empty** — you add the questions you want to test.

- **Easiest:** after you connect Bito (Step 3), the app can **auto-generate** questions
  based on your indexed repositories.
- **Manual:** click **Add** and write your own. For ideas and the right shape, open the
  included **`prompts.example.json`** file — it has ready-made templates (just replace the
  `<repo>` placeholders with your real repository names).

Aim your questions at code that your Bito workspace has **indexed** — that's where Bito
helps most (architecture, cross-repo, "where is X", impact of a change, etc.).

---

## Step 6 — Run the benchmark

Go to the **Run** tab.

1. **Arms:** keep **A, B, C** checked (you need all three for a full comparison).
2. **Workspace:**
   - **Fresh clone** (default) — each arm fetches the code it needs on its own.
   - **Local repo** — test against a copy of a folder on your computer (the path is
     pre-filled). Use this to try changes against your own checkout.
3. Click **Start**. You'll see live progress. A full run takes a while (it's making real
   model calls). You can **Stop** any time.

---

## Step 7 — See the results

- **Results / Scores** — quality, cost, time, and tokens per arm (defaults to your latest
  run). Higher quality at **lower cost and time** is the Bito win.
- **Leaderboard** — who won on cost, speed, quality, and more.
- **Reports** — a clean side-by-side summary you can **download as Markdown** to share.

---

## Everyday use

- **Stop the app:** click the black window and press **Ctrl + C**.
- **Start it again later:** double-click the same start file. It's instant after the first
  time.
- **Start fresh (wipe setup + results to re-test):** run the cleanup script, then start again:
  - **Mac/Linux:** `./scripts/clean.sh`
  - **Windows:** `powershell -ExecutionPolicy Bypass -File scripts\clean.ps1`

---

## Troubleshooting

| Problem | Fix |
|---|---|
| **"claude: command not found"** or runs fail | Open a terminal, run `npm install -g @anthropic-ai/claude-code`, then `claude` and `/login`. (Install Node.js from https://nodejs.org if needed.) |
| **Setup says "Bito MCP unavailable"** | Re-run **Run health check**; if it persists, click **Disconnect** and connect again, or reinstall with the one-line installer in Step 4. |
| **"address already in use"** | An old copy is still running — the start file now clears it automatically, so just start again. |
| **Page won't open** | Make sure the black window is still open, then visit http://127.0.0.1:8765. To use a different port: `python app.py --port 9000`. |
| **Prompts page is empty** | That's expected on a fresh start — add your own, or connect Bito and let it auto-generate (see Step 5). |

---

## What this needs (installed automatically where possible)

- **Python 3.10+** — the start file installs it if missing.
- **Claude Code CLI** (uses Node.js) — installed for you; you sign in once (Step 2).
- **A Bito AI Architect Workspace** — connected in the app (Step 3).

Maintainer note: utility scripts live in **`scripts/`** — `clean.sh` / `clean.ps1` to
reset for a fresh test, and `package.py` to build the shareable client zip.
