# codexauthutil

A small CLI utility for managing multiple [OpenAI Codex](https://github.com/openai/codex) `auth.json` profiles. Switch between accounts instantly and see live quota usage for each one.

## Why

Codex stores its auth token at `~/.codex/auth.json`. If you have multiple OpenAI accounts (e.g. work and personal), swapping between them means manually copying files. This tool manages that for you and shows how much of your 5-hour and weekly quota each account has used.

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) — for direct execution without installing

## Installation

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

## Usage

### Add a profile

Save the currently active `~/.codex/auth.json` as a named profile:

```bash
./codexauth.py add work
./codexauth.py add personal
```

Or save from a specific file:

```bash
./codexauth.py add work --file /path/to/work-auth.json
```

### List profiles

Shows all profiles with live quota usage, then prompts you to activate one:

```bash
./codexauth.py list
```

```
  #  Name        Mode      5h Used        Weekly
  1  work        chatgpt   ████░ 74%      ████░ 74%      ●
  2  personal    chatgpt   █░░░░ 12%      ██░░░ 38%

Activate token (enter number, or q to quit): _
```

- Enter a number to activate that profile
- Press Enter or `q` to exit without changing anything
- The `●` marks the currently active profile

Flags:
- `--no-interactive` — print the table and exit (useful for scripting)
- `--no-usage` — skip the API call for faster output

### Activate a profile

Directly activate by name (no prompt):

```bash
./codexauth.py use work
```

This copies the profile to `~/.codex/auth.json` and backs up the previous file to `~/.codexauth/auth.json.bak`.

### Check active profile

```bash
./codexauth.py status
# Active: work
```

### Remove a profile

```bash
./codexauth.py remove personal
```

## How usage data works

Quota is fetched from `https://chatgpt.com/backend-api/wham/usage` using the `access_token` stored in each profile's `auth.json`. Two windows are displayed:

| Column | Window | Description |
|--------|--------|-------------|
| **5h Used** | 5 hours | Short-term compute quota |
| **Weekly** | 7 days | Rolling weekly quota |

- Tokens are automatically refreshed if they are older than 8 days
- `api_key` mode profiles show `N/A` (no quota limits apply)
- Expired or revoked tokens show `expired` in red

## File layout

Profiles are stored in `~/.codexauth/`:

```
~/.codexauth/
├── tokens/
│   ├── work.json       # saved auth.json profiles (chmod 600)
│   └── personal.json
├── active              # name of the currently active profile
└── auth.json.bak       # backup of the last overwritten auth.json
```

The store directory is created with `chmod 700` and individual token files with `chmod 600`.

## Running tests

```bash
uv run --with "pytest,pytest-asyncio,respx" pytest tests/ -v
```
