# codexauthutil

A small CLI utility for managing multiple [OpenAI Codex](https://github.com/openai/codex) `auth.json` profiles. Switch between accounts instantly and see live quota usage, reset countdowns, and earned usage-limit reset credits for each one.

## Why

Codex stores its auth token at `~/.codex/auth.json`. If you have multiple OpenAI accounts (e.g. work and personal), swapping between them means manually copying files. This tool manages that for you and shows how much of your 5-hour and weekly quota each account has used, plus how long remains until each window resets. It also identifies Plus and Pro tiers and normalizes weekly capacity into Plus-equivalent units so accounts with different subscriptions can be compared directly.

This tool also can help you sync auth tokens between different computers. 

## Requirements

- Python 3.10+; the host development environment uses Python 3.14.
- [`mise`](https://mise.jdx.dev/) installs Python and uv using `mise.toml`.

# Installation

### Option A — Run from the checkout

```bash
git clone https://github.com/jamesyc/codexauthutil
cd codexauthutil
mise trust
mise install
mise exec -- uv sync --locked
mise exec -- uv run --locked codexauth --help
```

`uv` installs the locked dependencies into `.venv` using mise's Python. The
standalone `codexauth.py` launcher also works through
`mise exec -- uv run --script codexauth.py --help`, but uses its inline dependencies
instead of the project lockfile.

### Option B — Install as a shell command

```bash
mise exec -- uv tool install --editable .
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
Use `--local-only` to keep a profile usable on this machine without importing or exporting it:

```bash
./codexauth.py add private --local-only
```

### Log into a new profile

Bootstrap a fresh ChatGPT-backed profile through the browser OAuth flow:

```bash
./codexauth.py login
```
or
```bash
./codexauth.py login work
```

OAuth profiles can also stay on the current machine only:

```bash
./codexauth.py login private --local-only
```

This uses the hard-coded Codex OAuth client and redirect URI `http://localhost:1455/auth/callback`, and sends the same extra authorize parameters Codex/OpenClaw use.

The command prints an authorization URL, waits for you to paste back the full localhost callback URL, exchanges the authorization code for tokens, asks for a profile name if you did not pass one on the command line, saves the profile, and then shows the normal profile list.

## List profiles

Run without a command to sync your configured accounts, show live quota usage, and choose a profile:

```bash
./codexauth.py
```

Use `list` to view local profiles directly:

```bash
./codexauth.py list
```
```
  #  Name        Tier      Weekly        Weekly Left   Plus-Eq Left   Credits   Reset Expires
  1  work        Pro 20x   ████░ 74%     2d 3h               5.20      2311   10d  5h  3m  ●
  2  personal    Plus      ██░░░ 38%     5d 8h               0.62         —              —

Activate token (enter number, or q to quit): _
```

- Enter a number to activate that profile
- Press Enter or `q` to exit without changing anything
- The `●` marks the currently active profile

`Plus-Eq Left` treats one complete Plus weekly allowance as `1.00`. The current
normalization is Plus = 1x, Pro 5x ($100) = 5x, and Pro 20x ($200) = 20x. It is
an allowance comparison, not an estimate of literal tokens remaining.

Flags for `list`:
- `--no-interactive` — print the table and exit (useful for scripting)
- `--no-usage` — skip the API call for faster output
- `--all` — include hidden profiles and show the detailed `Mode`, `5h Used`, `5h Left`, and Spark columns

Profiles can be hidden from the default list without being deleted or banned.
Hidden preferences are synced with `sync` when `CODEXAUTH_SYNC_DIR`
is configured:

```bash
./codexauth.py hide old-work
./codexauth.py list --all
./codexauth.py unhide old-work
```

## Watch all accounts

```bash
./codexauth.py watch
```

Syncs configured accounts and checks live usage for every stored account, including hidden
profiles, immediately and every 10 seconds. It shows the detailed usage table, refreshes stale
tokens, and reloads profiles and the active account after syncing. Slow checks finish before
another starts. The current table stays visible while sync and usage refresh in the background,
then updates once the next snapshot is ready.
Redirected output keeps each timestamped snapshot. It keeps watching
even when no profiles are stored, so newly added accounts appear on the next check.
There are no activation or sync prompts. Sync failures are shown alongside the local table,
and syncing is tried again on the next check. Press **Ctrl+C** to stop.
Use `watch --interval 30` to check every 30 seconds instead.

## Start unset weekly usage windows

Some ChatGPT-backed accounts show no weekly reset time, or return the full seven-day
`reset_after_seconds` value, until they make their first Codex request. Check every stored profile,
including hidden profiles, and start only those weekly windows with:

```bash
./codexauth.py start-weekly
```

The command shows the matching profiles and asks for confirmation because it can send several small Codex
requests per account. The default `--model auto` discovers each account’s available models through
`codex app-server`, prefers small families (nano, mini, Luna, then Terra), and tries the account
default before other models. This is a size/cost heuristic; the catalog does not expose exact
parameter counts or prices. After each successful request it polls the main weekly bucket,
continuing to the next model if the timer stays unset. Unsupported models are skipped; other
request errors or failed usage checks stop probing. It reports the model that started the timer.
Each request runs in a temporary isolated
`CODEX_HOME` with a read-only sandbox, and does not switch the active profile. Any credentials that
Codex refreshes during the request are saved back to the matching stored profile.

Pass profile names to check only those accounts, use `--model` to override the model, or use `--yes`
to skip confirmation:

```bash
./codexauth.py start-weekly work personal
./codexauth.py start-weekly --model gpt-5.6-terra --yes
```

This command requires the `codex` CLI to be installed and available on `PATH`.

The API reports both `reset_at` and second-precise `reset_after_seconds`. An unstarted seven-day
placeholder reports the full `604800` seconds on every lookup, even though the table floors that to
`6d 23h`; `start-weekly` triggers when that exact value is present or the reset time is null.
A successful model response alone does not prove that the main weekly bucket started. An earlier
Plus-account test with `gpt-5.4-mini` left it unset. Auto mode verifies a real reset countdown
before reporting success; an explicit `--model` sends only one request. GPT-5.4 and GPT-5.4 mini
retired from ChatGPT-backed Codex on August 31, 2026 ([OpenAI model documentation](https://learn.chatgpt.com/docs/models)).
A September 13, 2026 live check selected `gpt-5.6-luna` and verified that it started the main
weekly timer. Auto mode checks each account independently rather than assuming this holds for all plans.

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

The shared profile repository is configured in a repo-local `.env` file:

```bash
echo 'CODEXAUTH_SYNC_DIR=~/codex-profiles' > .env
```

The path is expanded with your home directory, so `~/...` works.

## Sync all accounts

The configured directory must be a Git checkout with an upstream branch and working Git
credentials. Running `./codexauth.py` automatically fetches shared changes, merges newer
credentials in both directions, updates the active account, and publishes the result before
loading the usage table. `watch` performs the same sync before each usage check.

If usage lookup refreshes tokens after a successful sync, those updates are synced automatically
as well. No sync confirmation is needed. Without sync configuration, both commands show local
profiles normally. If syncing fails, they report the problem and still show the local table.

To sync once without displaying the usage table:

```bash
./codexauth.py sync
```

To keep syncing and displaying live usage every 10 seconds:

```bash
./codexauth.py watch
```

This runs until **Ctrl+C**. Use `watch --interval 30` to check every 30 seconds instead.
To keep it running after the terminal closes, launch it with a log:

```bash
mkdir -p -m 700 ~/.codexauth
nohup ./codexauth.py watch > ~/.codexauth/watch.log 2>&1 &
```

Each check reports imported, exported, removed, and skipped profiles. It never prompts for
input. Ambiguous or invalid credentials are left untouched while other accounts sync.
The standalone `sync` command exits with code `0` on success, `2` when profiles were skipped,
or `1` if the sync could not finish. Automatic sync issues do not prevent the default command
from showing the table or stop `watch` from continuing.

Rejected pushes and temporary network failures trigger up to three attempts, each fetching
again and repeating credential comparisons. Git operations have a 30-second timeout
and use noninteractive authentication, so authentication must already be configured.

Sync preserves complete token sets even when Git branches diverge. It stages only profile
files, hidden preferences, and `.gitignore`. Unrelated staged work and pre-existing unfinished
Git operations require attention. An ambiguous conflict between Git versions, or a conflict
outside credential and hidden-preference files, stops that pass without guessing a winner.

A local lock prevents simultaneous sync runs, including the legacy commands. Changes to local
credentials during publication cause another comparison pass. Hidden and unhidden preferences
merge using the previous agreed state, so one machine does not repeatedly undo the other.
A profile named in the sync repository's `.gitignore` is removed from the local store. Ordinary
absence is not a shared deletion; use this blacklist when a removal must persist across machines.
Blacklisted files are excluded from credential comparisons even if they are still tracked in Git.
An unchanged historical profile does not block other accounts from syncing.
Profiles created with `--local-only` are recorded in `~/.codexauth/local-only`, omitted from every
sync path, and added to the repository-local `.git/info/exclude`. They remain available for local
usage and activation, but neither their credentials nor their hidden preference are published.

The older `pull` and `push` commands remain callable for compatibility but are hidden from
normal help. They retain their interactive conflict-resolution behavior; running the script
directly or using `watch` handles routine syncing automatically.

## How automatic credential merging works

Sync first checks account identifiers and, when available, the user identity in the ID token.
For matching ChatGPT accounts, it compares access-token and ID-token issue times (`iat`) and
`last_refresh`. Comparable timestamps must agree on which copy is newer; missing timestamps
are ignored, and equal timestamps do not break a tie. File modification times never decide
credential freshness because a Git checkout or file copy can change them.

The newer profile is copied intact so its access, refresh, and ID tokens remain together.
Identical JSON contents are skipped without rewriting either file. If identities differ or
cannot be confirmed, timestamps conflict, or different credentials have equal or unavailable
timestamps, `sync` skips the profile and reports the reason. Different API keys also require
manual resolution because they do not contain comparable refresh metadata. The legacy commands
can show the available dates and ask which copy to use.

The same comparison applies to active-account reconciliation. Noninteractive `list` and
`watch` report ambiguous active credentials without prompting. These comparisons use local
metadata; they do not refresh tokens or contact the authentication service to determine the winner.

# How usage data works

Quota is fetched from `https://chatgpt.com/backend-api/wham/usage` using the `access_token` stored in each profile's `auth.json`. Earned usage-limit reset details are fetched from `https://chatgpt.com/backend-api/wham/rate-limit-reset-credits`, matching the data shown by Codex's `/usage` flow. These are reset credits that can reset eligible usage windows; they are separate from the normal time when a quota window resets automatically.

The detailed `--all` list view shows standard 5-hour and weekly columns, while the default view keeps only the weekly pair. When the API provides `limit_window_seconds`, the CLI classifies each window by duration instead of assuming `primary_window=5h` and `secondary_window=weekly`. If the API omits `limit_window_seconds`, the parser falls back to the legacy positional mapping for backwards compatibility. If the API also returns named Spark limits under `additional_rate_limits`, `--all` renders those as extra columns using the same duration-aware logic. The UI shortens `GPT-5.3-Codex-Spark` to `Spark` so the table stays readable on narrow terminals:

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
| **Credits** | Account-level | Rounded ChatGPT credit balance, matching Codex's status display |
| **Reset Expires** | Account-level | Time remaining before each available earned usage-limit reset expires, formatted in aligned day/hour/minute fields like `10d  5h  3m` |

- Tokens are automatically refreshed if they are older than 8 days
- Unknown or duplicate API windows are preserved as extra columns rather than silently relabeled or dropped
- `api_key` mode profiles show `N/A` (no quota limits apply)
- Expired or revoked tokens show `expired` in red
- Reset countdowns render compact durations such as `53m`, `4h 12m`, or `2d 3h`
- Available usage-limit resets are ordered by soonest expiration and shown as compact days, hours, and minutes remaining; resets without an expiration show `Does not expire`
- Reset-expiration countdowns use the default text color beyond seven days, yellow at seven days or less, and red at one day or less
- If reset-detail lookup fails, the reset-expiration column shows `Unavailable`

If refresh succeeds, the local stored profile is updated with the new tokens and a fresh `last_refresh` timestamp.

This utility reports reset availability but does not redeem a reset. Use Codex's `/usage` menu when you want to consume one.

# File layout

Profiles are stored in `~/.codexauth/`:

```
~/.codexauth/
├── tokens/
│   ├── work.json       # saved auth.json profiles (chmod 600)
│   └── personal.json
├── active              # name of the currently active profile
├── hidden              # list-view preference for hidden profile names
├── local-only          # profile names that must never be synced
├── auth.json.bak       # backup of the last overwritten auth.json
├── sync.lock           # local sync process lock
└── sync-state/         # last agreed hidden-profile preferences for each repository
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

Profile JSON files are copied with metadata preserved. Modified times are shown as context for unresolved conflicts, while credential timestamps determine freshness. The `hidden` file is a newline-delimited list of profile names hidden from the default list view.
Local-only profiles have no JSON file in this directory; their would-be paths are ignored through
the checkout's `.git/info/exclude` file.

# File write semantics

The tool treats inode preservation as a compatibility property for existing auth files:

- `use` and reconcile flows overwrite an existing `~/.codex/auth.json` in place instead of swapping it with a new file
- this preserves the inode number for an already-existing active auth file, so hard links continue to point at the updated contents
- if `~/.codex/auth.json` does not exist yet, the tool creates it, and a new inode is unavoidable
- `activate` preserves the selected profile's modified time on `~/.codex/auth.json`
- `save_codex_auth` writes fresh JSON and therefore gives `~/.codex/auth.json` a fresh modified time
- `add`, `import`, and `export` preserve source modified times for provenance; credential freshness is compared separately
- token refresh writes intentionally update stored profile modified time because the stored content actually changed

# Notes

- Stored profiles and backups are local plaintext JSON files; they are permission-restricted but not encrypted.
- Hidden `import`, `export`, `pull`, and `push` commands remain available for manual resolution and compatibility; running the script directly or using `watch` syncs automatically.

## Running tests

```bash
mise exec -- uv sync --locked --extra test
mise exec -- uv run --locked --extra test pytest tests/ -v
```
