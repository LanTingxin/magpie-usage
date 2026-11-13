---
name: magpie-usage
description: Inspect, backfill, or troubleshoot Antigravity native model & subagent usage reporting to Magpie. Use when the user asks about syncing Antigravity token usage to Magpie, backfilling historical Antigravity sessions into Magpie, or checking the magpie-usage plugin status.
---

# Magpie Usage Reporter

This plugin automatically reports Antigravity native model invocations (including subagents, cache read/write tokens, reasoning tokens, thinking effort, TTFT, first visible text latency `first_text_ms` for output TPS speed, and total stream latency) to local Magpie via `POST http://127.0.0.1:3425/v1/magpie/usage/requests` on `PostInvocation` and `Stop` hooks.

## Script Location

When installed globally (the default), the sync script resides at:

- **macOS / Linux**: `~/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py`
- **Windows (PowerShell)**: `$HOME/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py`
- **Workspace-scoped install**: `.agents/plugins/magpie-usage/scripts/sync_usage.py`

Always pass `-B` to Python (`python3 -B` on macOS/Linux, `py -3 -B` on Windows) so bytecode files are not written into the plugin directory.

## Commands

### 1. Check sync cursor & Magpie gateway key status

- **macOS / Linux**:
  ```bash
  python3 -B ~/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py --status
  ```
- **Windows (PowerShell)**:
  ```powershell
  py -3 -B "$HOME/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py" --status
  ```

### 2. Dry-run pending usage records (without posting)

- **macOS / Linux**:
  ```bash
  python3 -B ~/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py --dry-run
  ```
- **Windows (PowerShell)**:
  ```powershell
  py -3 -B "$HOME/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py" --dry-run
  ```

### 3. Backfill / catch up unsynced historical conversations

Safe and idempotent (deduplicated by `rid: agy-<request_id>`):

- **macOS / Linux**:
  ```bash
  python3 -B ~/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py --backfill
  ```
- **Windows (PowerShell)**:
  ```powershell
  py -3 -B "$HOME/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py" --backfill
  ```

### 4. Full re-scan from scratch

Use if Magpie's `usage.jsonl` was restored or cleared and you want to re-ingest all local SQLite conversation databases:

- **macOS / Linux**:
  ```bash
  python3 -B ~/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py --backfill --reset-cursor
  ```
- **Windows (PowerShell)**:
  ```powershell
  py -3 -B "$HOME/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py" --backfill --reset-cursor
  ```

### 5. Run built-in self-tests

- **macOS / Linux**:
  ```bash
  python3 -B ~/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py --self-test
  ```
- **Windows (PowerShell)**:
  ```powershell
  py -3 -B "$HOME/.gemini/config/plugins/magpie-usage/scripts/sync_usage.py" --self-test
  ```

## Optional Environment Variables

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `MAGPIE_USAGE_URL` | `http://127.0.0.1:3425/v1/magpie/usage/requests` | Override target Magpie usage ingest endpoint |
| `MAGPIE_GATEWAY_KEY` | Auto-read from `~/.config/magpie/caller-keys.json` | Explicit Bearer token if Magpie gateway auth is enabled |
| `MAGPIE_CONFIG_DIR` | `~/.config/magpie` | Custom Magpie config directory containing `caller-keys.json` |
| `ANTIGRAVITY_CONVERSATIONS_DIR` | `~/.gemini/antigravity/conversations` | Custom Antigravity SQLite trajectory directory |
