# codexauthutil

A small CLI utility for managing multiple [OpenAI Codex](https://github.com/openai/codex) `auth.json` profiles. Switch between accounts instantly and see live quota usage and reset countdowns for each one.

## Why

Codex stores its auth token at `~/.codex/auth.json`. If you have multiple OpenAI accounts (e.g. work and personal), swapping between them means manually copying files. This tool manages that for you and shows how much of your 5-hour and weekly quota each account has used, plus how long remains until each window resets.

This tool also can help you sync auth tokens between different computers. 

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) — for direct execution without installing

# Installation

### Option A — Run directly (no install)

```bash
git clone https://github.com/jamesyc/codexauthutil
cd codexauthutil
chmod +x codexauth.py
./codexauth.py --help
```

`uv` automatically installs dependencies (`click`, `rich`, `httpx`) into an isolated cache on first run. Nothing is installed system-wide.

### Option B — Install as a shell command

```bash
pip install -e .
codexauth --help
```

# Usage

## Add a profile

Save the currently active `~/.codex/auth.json` as a named profile:

```bash
./codexauth.py add work
./codexauth.py add personal
```

Or save from a specific file:

```bash
./codexauth.py add work --file /path/to/work-auth.json
```

`add` preserves the source file's modified timestamp, which is useful when comparing local and synced copies later.

### Log into a new profile

Bootstrap a fresh ChatGPT-backed profile through the browser OAuth flow:

```bash
./codexauth.py login
```
or
```bash
./codexauth.py login work
```

This uses the hard-coded Codex OAuth client and redirect URI `http://localhost:1455/auth/callback`, and sends the same extra authorize parameters Codex/OpenClaw use.

The command prints an authorization URL, waits for you to paste back the full localhost callback URL, exchanges the authorization code for tokens, asks for a profile name if you did not pass one on the command line, saves the profile, and then shows the normal profile list.

## List profiles

Shows all profiles with live quota usage, then prompts you to activate one:

```bash
./codexauth.py list
```
Or just
```bash
./codexauth.py
```
```
  #  Name        Mode      5h Used        5h Left   Weekly        Weekly Left
  1  work        chatgpt   ████░ 74%      4h 12m    ████░ 74%     2d 3h        ●
  2  personal    chatgpt   █░░░░ 12%      53m       ██░░░ 38%     5d 8h

Activate token (enter number, or q to quit): _
```

- Enter a number to activate that profile
- Press Enter or `q` to exit without changing anything
- The `●` marks the currently active profile

Flags:
- `--no-interactive` — print the table and exit (useful for scripting)
- `--no-usage` — skip the API call for faster output
- `--all` — include profiles hidden from the default list

Profiles can be hidden from the default list without being deleted or banned.
Hidden preferences are synced with `push` and `pull` when `CODEXAUTH_SYNC_DIR`
is configured:

```bash
./codexauth.py hide old-work
./codexauth.py list --all
./codexauth.py unhide old-work
```

## Activate a profile

Directly activate by name (no prompt):

```bash
./codexauth.py use work
```

This copies the profile to `~/.codex/auth.json` and backs up the previous file to `~/.codexauth/auth.json.bak`.

If `~/.codex/auth.json` already exists, activation overwrites that file in place, so the inode is preserved and existing hard links keep working. If the file does not exist yet, the tool must create it, which necessarily creates a new inode.

## Check active profile

```bash
./codexauth.py status
# Active: work
```

## Remove a profile

```bash
./codexauth.py remove personal
```

# Configure sync

`import` and `export` read a sync directory from a repo-local `.env` file:

```bash
echo 'CODEXAUTH_SYNC_DIR=~/codex-profiles' > .env
```

The path is expanded with your home directory, so `~/...` works.

## Pull shared changes

If the configured sync directory is a Git repo, fetch remote changes and then import every profile with:

```bash
./codexauth.py pull
```

`pull` reads `CODEXAUTH_SYNC_DIR` from `.env`, runs `git pull --no-rebase --no-edit`, and then imports all `*.json` profiles from that directory. If a profile would overwrite an existing local copy, it shows both modified timestamps before prompting.

## Publish local changes

To export every local profile and publish the result from the sync repo:

```bash
./codexauth.py push
```

`push` now exports every local profile first, then runs Git publication in `CODEXAUTH_SYNC_DIR`.

It runs the equivalent of:

```bash
export local profiles into CODEXAUTH_SYNC_DIR
git add .
git commit -m "Update exported codexauth profiles"
git pull --no-rebase --no-edit
git push
```

If there is nothing staged after `git add .`, `push` exits successfully without creating a commit.

# How usage data works

Quota is fetched from `https://chatgpt.com/backend-api/wham/usage` using the `access_token` stored in each profile's `auth.json`. The list view still shows standard 5-hour and weekly columns, but when the API provides `limit_window_seconds` the CLI classifies each window by duration instead of assuming `primary_window=5h` and `secondary_window=weekly`. If the API omits `limit_window_seconds`, the parser falls back to the legacy positional mapping for backwards compatibility. If the API also returns named limits under `additional_rate_limits`, the CLI renders those as extra columns using the same duration-aware logic. The current UI shortens `GPT-5.3-Codex-Spark` to `Spark` so the table stays readable on narrow terminals:

| Column | Window | Description |
|--------|--------|-------------|
| **5h Used** | 5 hours | Short-term compute quota |
| **5h Left** | 5 hours | Time remaining until the short-term quota window resets |
| **Weekly** | 7 days | Rolling weekly quota |
| **Weekly Left** | 7 days | Time remaining until the weekly quota window resets |
| **Spark** | API-defined | Additional named usage limit when `additional_rate_limits` includes `GPT-5.3-Codex-Spark` |
| **Spark Left** | API-defined | Time remaining until that named limit's primary window resets |
| **Spark Weekly** | API-defined | Weekly usage for that named limit when available |
| **Spark Weekly Left** | API-defined | Time remaining until that named limit's weekly window resets |

- Tokens are automatically refreshed if they are older than 8 days
- Unknown or duplicate API windows are preserved as extra columns rather than silently relabeled or dropped
- `api_key` mode profiles show `N/A` (no quota limits apply)
- Expired or revoked tokens show `expired` in red
- Reset countdowns render compact durations such as `53m`, `4h 12m`, or `2d 3h`

If refresh succeeds, the local stored profile is updated with the new tokens and a fresh `last_refresh` timestamp.

# File layout

Profiles are stored in `~/.codexauth/`:

```
~/.codexauth/
├── tokens/
│   ├── work.json       # saved auth.json profiles (chmod 600)
│   └── personal.json
├── active              # name of the currently active profile
├── hidden              # list-view preference for hidden profile names
└── auth.json.bak       # backup of the last overwritten auth.json
```

The store directory is created with `chmod 700` and individual token files with `chmod 600`.

# Sync directory layout

When `CODEXAUTH_SYNC_DIR` is configured, imported and exported profiles are stored as plain JSON files:

```text
~/codex-profiles/
├── work.json
├── personal.json
└── hidden
```

Profile JSON files are copied with metadata preserved so modified times stay meaningful during overwrite prompts. The `hidden` file is a newline-delimited list of profile names hidden from the default list view.

# File write semantics

The tool treats inode preservation as a compatibility property for existing auth files:

- `use` and reconcile flows overwrite an existing `~/.codex/auth.json` in place instead of swapping it with a new file
- this preserves the inode number for an already-existing active auth file, so hard links continue to point at the updated contents
- if `~/.codex/auth.json` does not exist yet, the tool creates it, and a new inode is unavoidable
- `activate` preserves the selected profile's modified time on `~/.codex/auth.json`
- `save_codex_auth` writes fresh JSON and therefore gives `~/.codex/auth.json` a fresh modified time
- `add`, `import`, and `export` preserve source modified times because sync conflict prompts depend on those timestamps
- token refresh writes intentionally update stored profile modified time because the stored content actually changed

# Notes

- Stored profiles and backups are local plaintext JSON files; they are permission-restricted but not encrypted.
- Hidden `import` and `export` commands still exist for testing lower-level sync behavior, but the normal user workflow is `pull` and `push`.

## Running tests

```bash
uv run --with "pytest,pytest-asyncio,respx" pytest tests/ -v
```
