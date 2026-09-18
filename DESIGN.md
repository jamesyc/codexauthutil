# codexauthutil Design Document

## Overview

`codexauthutil` is a small command-line utility for managing multiple OpenAI Codex authentication profiles. It is designed for people who switch between different `auth.json` identities, such as separate work and personal accounts, and want a fast way to:

- store named snapshots of Codex authentication data
- activate one profile into `~/.codex/auth.json`
- inspect which profile is currently active
- view quota usage for ChatGPT-backed profiles
- view quota usage and reset countdowns for ChatGPT-backed profiles

The project is intentionally lightweight. It is a local tool, not a service, and it uses the filesystem as its primary storage layer.

## Goals

- Provide a simple CLI for saving and switching between named auth profiles.
- Preserve the currently installed `~/.codex/auth.json` before replacing it.
- Surface usage information and reset countdowns in a human-friendly terminal view.
- Refresh ChatGPT OAuth tokens automatically when they are stale.
- Support bootstrapping a new ChatGPT-backed profile through a manual browser OAuth flow.
- Automatically sync credentials against a shared Git profile folder defined in a `.env` file.
- Keep the implementation easy to understand and maintain.

## Non-Goals

- Running a separate background service or daemon.
- Supporting a database-backed storage system.
- Providing deep account management beyond auth file switching and usage lookup.
- Automating browser login or embedding a browser UI inside the CLI.

## Primary Use Cases

1. A user saves their current `~/.codex/auth.json` as `work`.
2. The user later saves a different account as `personal`.
3. The user runs `codexauth` to sync accounts and see available profiles and current usage.
4. The user runs `codexauth use personal` to switch the active profile.
5. The tool copies the selected profile into `~/.codex/auth.json` and marks it active.
6. The user runs `codexauth watch` to keep syncing accounts and updating the usage table every 10 seconds.
7. The user runs `codexauth sync` for a single sync pass without a usage table.
8. The user runs `codexauth login` to generate a login URL for a new profile. The user pastes that URL into a browser, logs in, and reaches a localhost redirect that is expected to fail. The user copies the full callback URL from the browser address bar back into the CLI. The tool exchanges the authorization code for tokens and saves the result as a normal profile.

## High-Level Architecture

The system is organized into a few focused modules:

- `codexauth/cli.py`: Click-based command definitions and command orchestration.
- `codexauth/config.py`: `.env` loading and sync-directory resolution.
- `codexauth/store.py`: Filesystem storage, active profile tracking, and activation logic.
- `codexauth/usage.py`: Usage retrieval and concurrent usage fetching across profiles.
- `codexauth/weekly.py`: Isolated minimal Codex execution for starting unset weekly windows.
- `codexauth/refresh.py`: Refresh-token handling for ChatGPT OAuth credentials.
- `codexauth/oauth.py`: manual OAuth bootstrap helpers, callback validation, and code exchange.
- `codexauth/display.py`: Rich-based table rendering and interactive prompt behavior.
- `codexauth/sync.py`: import/export candidate discovery and metadata-preserving file copies.
- `codexauth/credentials.py`: shared account-identity and credential-timestamp comparison for sync and active reconciliation.
- `codexauth/autosync.py`: unattended bidirectional sync, semantic Git integration, retries, and hidden-preference merging.
- `codexauth/locking.py`: a reentrant local process lock shared by sync commands.

This separation keeps side effects contained:

- CLI code handles user interaction.
- Store code owns file layout and permissions.
- Usage code owns network calls and response interpretation.
- Refresh code owns token lifecycle decisions.
- Display code owns terminal formatting.

## Data Model

Profiles are stored as raw JSON documents, mirroring the structure of Codex `auth.json` files. The tool treats profile content mostly as opaque data, but it relies on a few known fields:

- `auth_mode`: determines whether the profile is a ChatGPT-backed auth profile.
- `tokens.access_token`: used to query usage APIs.
- `tokens.refresh_token`: used to refresh stale credentials.
- `tokens.account_id`: included in usage requests when present.
- `last_refresh`: local timestamp used to decide whether a refresh is needed.

### Filesystem Layout

The project stores state under `~/.codexauth`:

- `~/.codexauth/tokens/<name>.json`: saved named profiles
- `~/.codexauth/active`: name of the active profile
- `~/.codexauth/local-only`: names of profiles that must not participate in sync
- `~/.codexauth/auth.json.bak`: backup of the previous `~/.codex/auth.json`

The active Codex auth file remains:

- `~/.codex/auth.json`

An additional external profile directory may be configured through a `.env` file. This directory acts as an import/export source of truth outside the local store.

Example configuration:

- `.env`: contains a path-like variable such as `CODEXAUTH_SYNC_DIR=/path/to/shared/profiles`

Within that external directory, profile files are expected to be stored as:

- `<CODEXAUTH_SYNC_DIR>/<name>.json`

If a profile JSON path is listed in the sync repository's `.gitignore`, that
entry should be treated as an explicit blacklist or ban for that profile. In
other words, an ignored `*.json` file is not just omitted from sync; it means
the matching local stored profile should be considered disallowed.

Local-only profiles are a separate machine-local concept. Their names are stored in
`~/.codexauth/local-only`, excluded from import, export, and hidden-preference sync, and written to
a Codex-owned block in the checkout's `.git/info/exclude`. The tracked `.gitignore` keeps its
existing shared-ban meaning.

## Command Design

### `codexauth` (no command)

- Syncs automatically when `CODEXAUTH_SYNC_DIR` is configured, then loads profiles, fetches usage, and shows the normal table with activation options.
- Without sync configuration, shows local profiles normally. Sync failures or skipped profiles are reported without preventing display or activation.
- If usage lookup refreshes tokens or preflight reconciliation catches another local update after a successful sync, publishes those changes automatically while the table stays visible, before offering activation.
- Never prompts to sync. When the initial sync fails, avoids another immediate attempt after usage lookup; the next invocation or watch cycle retries.

### `codexauth sync`

- Standalone sync command for a pass without a usage table; `pull` and `push` remain hidden compatibility commands.
- Runs one noninteractive pass. Continuous operation belongs to `watch`; `sync` has no watch or interval options.
- Fetches the configured upstream branch, compares complete credential versions, merges local and external profiles, updates the active account, and publishes managed changes.
- Skips ambiguous or invalid local/external pairs and reports each profile without prompting. Other profiles can still sync.
- Returns exit code 0 for a complete pass, 2 for skipped profiles, and 1 for a failed pass.
- Holds a cross-process local lock for each pass; legacy sync commands share it. The lock releases on normal exit, interruption, or process termination.
- Git uses noninteractive authentication and bounded execution time. Rejected pushes and transient fetch/push errors retry up to three times with fresh comparisons and short backoff.
- Stages only profile JSON, `hidden`, and `.gitignore` in the configured directory, respecting literal filenames and preserving unrelated user changes.
- Refuses pre-existing merge/rebase state or unrelated staged changes. Credential Git conflicts are resolved by the shared comparison, replacing entire files even if Git could text-merge individual fields.
- Ambiguous Git versions stop the pass before a merge is started. Other unresolved Git conflicts abort only the merge started by this pass. Automatic display flows report the condition alongside the local table.
- Rechecks local profile and active-auth content after publication and retries if they changed.
- Merges hidden preferences against the last agreed state; stores this local state atomically under `~/.codexauth/sync-state/`. With no prior state, hidden preferences from either side are retained.
- Applies explicit `.gitignore` profile bans to the local store. File absence alone is not a deletion instruction.
- Checks bans from both Git branches before comparing credential blobs. Blacklisted tracked files use normal Git integration, with unresolved conflicts still stopping the pass. Identical Git blobs need no credential comparison, so an unchanged unsupported historical profile cannot block other accounts.
- Never imports, exports, removes, or publishes profiles named in `~/.codexauth/local-only`; refreshes and active reconciliation may still update their local stored credentials.

### `codexauth list`

- Lists local profiles without an automatic sync first.
- Optionally fetches usage data unless `--no-usage` is passed.
- Shows the current active profile in a width-aware Rich view.
- For ChatGPT-backed profiles with live usage data, the default view shows weekly usage and time left.
- `--all` adds the 5-hour usage and time-left columns, plus all Spark usage and time-left columns returned by the API.
- Shows the compact time remaining before each available earned usage-limit reset expires (for example, `10d  5h  3m`) in a right-aligned column with fixed day/hour/minute positions, without displaying the available count.
- Shows ChatGPT credits in a separate right-aligned column, rounding finite balances to the nearest whole credit to match Codex's status display and preserving `Unlimited`, `Available`, unavailable, and lookup-error states.
- Uses the default text color for reset expirations beyond seven days, yellow for seven days or less, and red for one day or less.
- Uses the full multi-column table on wide terminals, a compact table on medium widths, and a stacked per-profile layout on narrow screens so phone-sized terminals remain readable.
- Can prompt the user to activate a profile interactively unless `--no-interactive` is passed.
- Excludes hidden profiles by default. `--all` includes hidden profiles,
  labels them as hidden, and shows the detailed Mode, 5-hour, and Spark columns. Hidden profiles remain stored, activatable, and
  exported/imported; hiding is a list-view preference stored in
  `~/.codexauth/hidden` and synced as `<CODEXAUTH_SYNC_DIR>/hidden`.

### `codexauth watch`

- Syncs and checks usage for all profiles, including hidden profiles, immediately and every 10 seconds. `--interval` accepts a positive number of seconds to override the cadence.
- Uses the same automatic sync and token-publication flow as the default command, with no activation or sync prompts.
- Includes sync time and usage lookup time in each cycle; slow cycles finish before another starts.
- Keeps the previous table visible while sync and usage work completes, then sends the screen clear and complete replacement snapshot in one buffered write. Sync messages are captured with the snapshot; there is no fetching spinner.
- Reports sync failures alongside local usage and attempts sync again on the next cycle. Missing sync configuration simply skips syncing.
- Keeps polling an empty store, reloads profiles after sync on every cycle, and stops cleanly on Ctrl+C during sync, lookup, or waiting.
- Redirected output retains each timestamped snapshot.

### `codexauth add <name>`

- Reads an auth file from `--file` or defaults to `~/.codex/auth.json`.
- Performs a lightweight validity check by requiring `auth_mode` or `tokens`.
- Copies the source auth file into local storage under the given name.
- Preserves the source file's modified timestamp so imported profile age stays meaningful.
- `--local-only` records the profile as machine-local and excludes its sync-repository path through `.git/info/exclude`.

### `codexauth start-weekly [name ...]`

- Fetches live usage for the selected profiles, or every stored profile when no names are passed.
- Includes hidden profiles when checking all stored profiles.
- Selects only ChatGPT-backed profiles whose weekly window is missing, has no reset timestamp, or
  reports `reset_after_seconds == 604800`. This exact API value identifies the full seven-day
  placeholder without relying on the rounded table countdown.
- Confirms once before sending requests unless `--yes` is passed.
- Sends one minimal `hi` request per selected account with `gpt-5.4` by default; `--model` provides
  an override. `gpt-5.4-mini` is not used by default because it can complete without charging the
  main weekly bucket, leaving the placeholder unchanged.
- Creates a private temporary `CODEX_HOME` and workspace for each request, writes only that
  profile's auth file there, ignores user configuration and rules, uses an ephemeral session and a
  read-only sandbox, and never changes the active profile.
- Removes API-key environment variables from the child process so the temporary ChatGPT auth file
  determines the identity.
- Saves a temporary auth file back to the named profile if Codex refreshed its credentials, even
  when the request itself fails or times out.
- Fetches usage again after successful requests and reports whether the weekly window is visible;
  the API may take a short time to reflect a successful request.

### `codexauth login [name]`

- Begins a manual OAuth bootstrap for a new profile.
- Uses the fixed Codex/OpenClaw OAuth client ID and redirect URI, while still allowing scope and originator overrides from `.env` or process environment.
- Generates a random `state`, PKCE `code_verifier`, and derived `code_challenge`.
- Builds an authorization URL for the fixed Codex/OpenClaw OAuth client.
- Saves pending OAuth state locally so the callback can be validated later.
- Prints the URL and concise instructions telling the user to open it in a browser
- Displays an input box for the user to paste the full localhost callback URL back into the CLI.
- Accepts the pasted callback URL, and reads the pending OAuth state
- Parses the callback URL query parameters.
- Validates the returned `state`.
- Extracts the authorization `code`.
- Exchanges the code for tokens at the configured token endpoint using PKCE.
- Maps the response into the local `auth.json`-style profile structure.
- Prompts for a profile name if the user did not pass one on the command line.
- Saves the new profile under `~/.codexauth/tokens/<name>.json`.
- `--local-only` applies the same machine-local behavior as `add --local-only`.
- Removes the pending OAuth state after success.

### `codexauth use <name>`

- Verifies the named profile exists.
- Backs up the existing `~/.codex/auth.json` when present.
- Copies the selected profile into place.
- Records the selected name as active.

### `codexauth remove <name>`

- Deletes the named stored profile.
- Clears the active marker if that profile was active.

### `codexauth status`

- Prints the currently active profile name, if any.

### `codexauth import`

- Intended primarily as a lower-level testing and debugging command rather than the main user workflow.
- Reads the external profile directory path from `.env`.
- Imports all available external profiles by default.
- Detects name collisions with locally stored profiles.
- Automatically imports newer credentials for matching accounts, skips identical or older incoming copies, and prompts for ambiguous comparisons.

### `codexauth export`

- Intended primarily as a lower-level testing and debugging command rather than the main user workflow.
- Reads the external profile directory path from `.env`.
- Exports all available local profiles by default.
- Detects name collisions with profiles already present in the external directory.
- Automatically exports newer credentials for matching accounts, skips identical or older local copies, and prompts for ambiguous comparisons.

### `codexauth pull` (legacy)

- Reads the external profile directory path from `.env`.
- Treats that directory as a Git working tree used to fetch remote profile changes.
- Changes into the sync directory and runs `git pull`.
- Imports all profiles from the sync directory after a successful pull.
- Prompts only when account identity or credential freshness is ambiguous during import.

### `codexauth push` (legacy)

- Reads the external profile directory path from `.env`.
- Reconciles the active account, pulls the sync repo, imports newer external credentials, then exports newer local credentials.
- Treats that directory as a Git working tree used to publish exported profiles.
- Changes into the sync directory and runs `git add .`.
- Creates a commit with a default message describing the exported-profile update.
- Runs `git push` to publish the commit to the configured remote.
- Stages, commits, and pushes the exported changes in one command.

## Activation Flow

Profile activation is the core operation:

1. Resolve `~/.codexauth/tokens/<name>.json`.
2. Fail if the profile does not exist.
3. If `~/.codex/auth.json` already exists, copy it to `~/.codexauth/auth.json.bak`.
4. Copy the selected profile into `~/.codex/auth.json`.
5. Restrict file permissions to `0600`.
6. Write the active profile name to `~/.codexauth/active`.

This keeps the switch operation explicit and reversible at the file level.

### External Refresh Reconciliation

Because activation is a one-way copy from `~/.codexauth/tokens/<name>.json` to
`~/.codex/auth.json`, another app can refresh tokens in the active `auth.json`
without updating the stored named profile. That can lead to stale stored tokens
being reactivated later.

To support external refreshes safely, the design should add an explicit
reconciliation step before any command that may write refreshed token state or
replace `~/.codex/auth.json`. In practice, that means running reconciliation at
the start of `list` (including `list --no-usage` and the default no-args
invocation) and at the start of `use`. For `list`, the order should be:
reconciliation first, refresh second, then render the list.

1. Read the active marker (`~/.codexauth/active`) if present.
2. If the active marker points to an existing stored profile, compare
   `~/.codex/auth.json` with that stored profile.
3. If they differ and both still look like the same auth identity,
   update `~/.codexauth/tokens/<active>.json` from `~/.codex/auth.json`
   before any new activation proceeds.
4. When reconciliation writes local `~/.codex/auth.json` back into store and a
   sync directory is configured, interactive commands should offer to run
   `push` so that refreshed credentials can be propagated to other machines.
   `list --no-interactive` should skip this prompt.
5. If `list` refreshes any previously stale stored ChatGPT-backed profiles
   during usage lookup and a sync directory is configured, interactive `list`
   should also offer to run `push` so those newly refreshed stored tokens can
   be propagated to other machines. `list --no-interactive` should skip this
   prompt as well.

Identity matching should be conservative to avoid accidentally writing one
account over another. A reasonable baseline is:

- account IDs from `tokens.account_id` and embedded ChatGPT account claims must agree wherever present
- `(iss, sub)` from `id_token` must also agree when available on both sides
- matching account IDs or matching ID-token identity may confirm identity when the other signal is missing

If any available identity signals disagree, the tool asks before replacing the saved profile. Matching identities permit automatic comparison of credential timestamps. When identity cannot be confirmed, the tool should not auto-overwrite.
Instead it should surface a clear warning and require an explicit command to
reconcile.

We also have to handle external refreshes for the `pull` command:

1. Before running `git pull`, first run the normal active-profile
   reconciliation step so any local external refresh already present in
   `~/.codex/auth.json` is written back to `~/.codexauth/tokens/<active>.json`
   before sync starts.

2. After `git pull`, import profiles from the sync directory into local store as
   usual.

3. If an imported profile is not the currently active local profile, stop after
   updating the stored profile.

4. If an imported profile is the currently active local profile, compare the
   imported `~/.codexauth/tokens/<active>.json` with local `~/.codex/auth.json`.
   If identity matches, automatically update the older copy to match the newer
   one:
   - require matching account identity with no conflicting user or account identifiers
   - compare token issue times and `last_refresh`, ignoring filesystem timestamps
   - require confirmation when comparable credential timestamps disagree, or when they are equal or missing despite different contents

5. If identity does not match, or cannot be confirmed from the available
   fields, do not auto-overwrite. Surface a warning and require the user to
   confirm which copy should win before replacing the saved profile or
   `~/.codex/auth.json`.

### Proposed Commands

To keep behavior clear and testable, add explicit commands alongside the
automatic pre-activation safeguard:

- `codexauth reconcile-active`
  - compares `~/.codex/auth.json` with the currently active stored profile
  - writes back to the store only when identity checks pass
  - prints whether an update occurred or no differences were found
  - do not put this in --help or README, this is for testing
  - have correct errors if identity checks fail

### Failure and Safety Rules

The reconciliation path should prioritize credential safety:

- missing `~/.codex/auth.json`: no-op with clear message
- missing active marker: no-op with clear message
- active marker points to missing profile file: warning and no write
- invalid JSON on either side: fail with concise parse error
- missing or undecodable `id_token`: do not prompt the user to delete tokens;
  instead treat identity as unconfirmed unless `tokens.account_id` is sufficient
  on its own
- identity mismatch: no write unless user explicitly forces an overwrite

All writes should preserve existing file permissions (`0600`) and update mtime,
because the stored profile contents genuinely changed.

Reconciliation should also be a strict no-op when the two copies are already
equal as parsed JSON. In that case, the tool should not rewrite either file and
should not change mtime. This avoids churn after a `pull` updates the active
local state and a later `list` or `use` checks the same profile again.

### Observability

When reconciliation changes a stored profile, the CLI output should explicitly
state:

- which profile was updated
- which fields changed at a high level (for example, tokens refreshed). Print token class changes. 
- that the update source was `~/.codex/auth.json`

This makes externally-triggered refresh sync visible instead of implicit.
If the update came from local `~/.codex/auth.json` and sync is configured, the
CLI should also offer a follow-up `push`. The CLI should not accept a "q" or 
empty newline as a most-likely accidental response.

## Manual OAuth Bootstrap Design

The project can support a browser-assisted OAuth bootstrap without attempting to control the
browser. The intended flow is:

1. The user runs `codexauth login`, optionally with a profile name.
2. The CLI generates an authorization URL using the fixed Codex/OpenClaw OAuth client ID and the fixed
   redirect URI `http://localhost:1455/auth/callback`, random `state`, and PKCE challenge.
3. The user pastes that URL into a browser and completes the provider's normal login flow.
4. The provider redirects to a localhost callback URL such as
   `http://localhost:1455/auth/callback?code=...&state=...`.
5. No local web server is required. The browser may display a connection failure, but the full callback URL remains visible in the address bar.
6. The user copies that callback URL and pastes it to this app.
7. The CLI validates the callback against the locally stored pending OAuth state.
8. The CLI exchanges the authorization code for tokens and saves the resulting profile, prompting for a profile name if needed.

This design preserves the lightweight CLI character of the project and avoids hidden browser
automation.
Using a localhost redirect URI without a listening callback server is an intentional tradeoff.

### Auth File Mapping

The goal is to convert a successful token exchange into the same local profile shape already used by
the rest of the application:

- `auth_mode`: `chatgpt`
- `OPENAI_API_KEY`: `null`
- `tokens.access_token`: from the token response
- `tokens.refresh_token`: from the token response when present
- `tokens.id_token`: from the token response when present
- `last_refresh`: set to the current UTC timestamp

If the token exchange does not provide an account identifier, the profile may initially be saved
without `tokens.account_id`. The usage subsystem can continue to treat that field as optional.

### Failure Handling

The OAuth bootstrap flow should fail clearly and safely:

- missing or expired pending login state should stop the flow before any token request
- callback URLs missing `code` or `state` should be rejected with a clear error
- token exchange failures should surface a concise provider-facing error without dumping secrets

### UX Notes

The command output should optimize for copy/paste reliability:

- `login` should print the authorization URL on its own line
- it should remind the user that a localhost browser error is expected
- it should accept a full callback URL directly
- it should ask the user what to name the profile
- successful completion should end with a saved-profile message and display `list`

## Usage Retrieval Design

Usage lookup is only attempted for ChatGPT-backed profiles. For each eligible profile:

1. Read the access token and optional account ID.
2. Check whether the profile appears stale using `last_refresh`.
3. Refresh tokens first if needed.
4. Concurrently request the usage endpoint and the reset-credit detail endpoint.
5. Extract the standard primary and secondary usage windows from the top-level `rate_limit` object.
6. Extract any named additional limits from `additional_rate_limits[]`, preserving each entry's `limit_name`.
7. Read `rate_limit_reset_credits.available_count` from the usage response as a fallback, then prefer the detailed reset-credit response when available so each available credit's expiration can be shown.
8. With `--all`, apply presentation-friendly labels to named limits in the renderer; for example, shorten `GPT-5.3-Codex-Spark` to `Spark` and `GPT-5.3-Codex-Spark Weekly` to `Spark Weekly`. Hide all Spark columns in the default view.

Reset-credit details are read-only in this utility. Available credits are sorted by expiration, credits with no expiration are shown last, and a detail-request failure does not discard a count successfully returned by the usage endpoint.

The CLI fetches usage concurrently for all profiles with `asyncio.gather`, which keeps the list command responsive even when several profiles are stored.

To keep network overhead predictable as the number of stored profiles grows,
the implementation should bound usage-fetch concurrency with a semaphore rather
than launching an unbounded number of requests. A small default such as 8
concurrent profiles is a reasonable baseline.

The usage layer should also reuse shared async HTTP clients across the full
batch:

- one client for usage GET requests
- one client for refresh POST requests

This avoids opening a fresh connection pool per profile while still allowing
different timeout policies for usage and refresh calls.

Usage retrieval should also report which stored profiles were actually updated
by a successful refresh. `list` uses that metadata to decide whether it should
offer a follow-up `push` for refreshed stored tokens when sync is configured
and the command is running interactively.

## Import and Export Design

The import/export feature extends the existing filesystem-first design by introducing an optional shared folder configured through a `.env` file.
These commands are retained primarily as lower-level testing and debugging helpers; the intended end-user sync workflow is `pull` and `push`.

### Configuration

The project should load a configured external directory from `.env` at startup or on demand for import/export commands. The expected behavior is:

- if the configured path is missing, `import` and `export` should fail with a clear setup message
- if the directory does not exist, the command should either create it on export or report the issue explicitly, depending on the chosen implementation
- the path should be treated as the only supported external source/destination for these commands

This keeps configuration simple and avoids adding another persistent config system.

### Import Flow

1. Read the external profile directory from `.env`.
2. Enumerate candidate `*.json` files in that directory.
3. Compare them against local profiles by profile name.
4. For profiles that do not exist locally, import directly.
5. For existing profiles, compare account identity and credential timestamps. Import a clearly newer source, skip an identical or older source, and prompt only for ambiguity.
6. Copy accepted profiles into `~/.codexauth/tokens`.
7. Preserve the source file's modified timestamp on the imported local copy.
8. If the sync directory's `.gitignore` blacklists a profile JSON file, treat
   that as a removal instruction and run the equivalent of `codexauth remove
   <name>` against the local store for that profile.
9. If `<CODEXAUTH_SYNC_DIR>/hidden` exists, import it as the local hidden
   profile preference after profile imports and blacklist removals.

Key UX requirement:

- report automatic updates and skipped older or identical credentials
- allow the user to skip individual ambiguous profiles
- show credential dates and file timestamps as context for unresolved comparisons

### Export Flow

1. Read the external profile directory from `.env`.
2. Enumerate local stored profiles.
3. Compare them against files already present in the external directory.
4. For profiles that do not exist externally, export directly.
5. For existing external profiles, use the same credential comparison as import. Export newer local credentials, skip identical or older copies, and prompt for ambiguity.
6. Copy accepted profiles into the external directory.
7. Preserve the source file's modified timestamp on the exported copy.

The export flow mirrors the import flow so users only need to learn one mental model.

## Git Sync Design

The Git sync feature extends the sync-directory workflow by assuming that the external profile directory may also be a Git repository. The goal is to support two lightweight workflows:

1. inbound: run `codexauth pull`
2. outbound: run `codexauth push`

Standalone `import` and `export` remain available, but `pull` and `push` now include those file-copy steps by default for the common sync path.

### Pull Flow

The pull command should behave as follows:

1. Read the external profile directory from `.env`.
2. Fail with a clear setup message if the directory is not configured.
3. Fail with a clear error if the directory does not exist.
4. Fail with a clear error if the directory is not inside a Git working tree.
5. Reconcile the currently active local profile first, if possible, so local
   external refreshes are captured in store before sync begins.
6. Run `git pull`.
7. Import all profiles from the sync directory.
8. Parse the sync repository's `.gitignore` for blacklisted `*.json` profile
   paths. For each matching local stored profile, run the equivalent of
   `codexauth remove <name>` so the local store mirrors the ban after pull.
9. For any imported profile that is currently active locally, reconcile the
   imported stored copy against `~/.codex/auth.json` before deciding whether one
   copy should replace the other.
10. Prompt only when identity cannot be confirmed or credential freshness is ambiguous, during import or active reconciliation.
11. Print a success message summarizing what happened.

### Push Flow

The push command should behave as follows:

1. Read the external profile directory from `.env`.
2. Fail with a clear error if the directory does not exist.
3. Fail with a clear error if the directory is not inside a Git working tree.
4. Reconcile the active account, then run `git pull` before writing profile files in the sync repo. This makes unfinished merges and conflicting working-tree changes fail before export can overwrite or stage them.
5. Import new external profiles and clearly newer external credentials, reconcile any imported active account, then export newer local credentials. Preserve local hidden preferences during this merge and skip blacklisted profiles. Ambiguous comparisons prompt once during export.
6. Export the local hidden profile preference to `<CODEXAUTH_SYNC_DIR>/hidden`.
7. Run `git add .` from that directory.
8. Check whether staging produced any changes.
9. If there are staged changes, run `git commit -m <message>`, followed by another `git pull` to integrate an update published during the export/commit window.
10. If there are no staged changes, skip the commit and report the no-op.
11. Run `git push` in either case so an existing ahead commit is not stranded.
12. Print a success message summarizing what happened.

### Commit Message Strategy

The default commit message should be deterministic and specific enough to explain the origin of the change set. A good baseline is something like:

- `Update exported codexauth profiles`

This keeps history readable without overfitting the message to a specific export selection. A future extension could allow a custom message flag, but the initial design does not require one.

### Why Keep `import`/`export` Alongside `pull`/`push`?

Keeping the lower-level copy commands still has benefits:

- users can review or manipulate sync-directory files without Git
- users can export locally even when Git remotes are unavailable
- users can import from the sync directory even when Git remotes are unavailable
- users can still choose a two-step manual flow when they want more control

### Edge Cases and Failure Modes

The Git sync flow introduces several important edge cases that should be handled explicitly.

#### Missing `.env` configuration

If `CODEXAUTH_SYNC_DIR` is not present, the command should fail with the same clear setup guidance used by import/export.

#### Sync directory does not exist

If the configured sync directory path does not exist yet, `push` should fail rather than creating it. Unlike `export`, this command is specifically about publishing an existing Git working tree.

#### Sync directory is not a Git repository

The configured directory may exist but not contain a `.git` directory, or it may not be part of any working tree. In that case, the command should stop before staging files and explain that the sync directory must be a Git repository.

#### Pull failure

If `git pull` fails, the command should surface the Git error output and exit with failure.

This may happen because of:

- authentication failures
- network failures
- merge conflicts
- local working tree changes preventing pull
- remote branch or tracking configuration problems

Legacy pull/push commands surface Git conflicts. The primary sync command resolves clearly ordered credential versions in full and three-way merges hidden preferences. Ambiguous Git versions or other unresolved conflicts stop the pass; only a merge started by that pass may be aborted automatically.

#### Pull succeeds but import is partially declined

After a successful `git pull`, ambiguous credential comparisons may still need a choice. Users may accept some replacements and decline others, producing a partial local update.

Unresolved comparisons remain untouched by default; older credentials are never automatically copied over newer ones.

#### Pull sees concurrent drift on both sides

It is possible for local `~/.codex/auth.json` to have been refreshed externally
while a newer version of the same profile also arrives from another machine via
`pull`.

The design should handle this in two stages:

- reconcile local active state into store before `git pull`
- then import remote changes and reconcile the active imported profile against
  local `~/.codex/auth.json`

This ordering avoids losing a local external refresh before sync begins, while
still allowing an imported newer active profile to update the local active auth
file afterward when identity checks pass.

#### No exported changes

If `git add .` is successful but there is nothing to commit, the command should not treat that as an error. It should print a message such as "No changes to commit", skip `git commit`, and still call `git push` in case a previous publication attempt left the local branch ahead of its remote.

This case matters because users may routinely run:

1. `codexauth export`
2. `codexauth push`

even when the export did not materially change any files.

#### Export succeeds but Git publication fails

Because `push` now exports before running Git commands, it can partially succeed: the sync directory may be updated on disk even if Git validation, commit, or push later fails.

This is acceptable because the export step is still useful on its own, and the design should avoid destructive rollback behavior.

#### Untracked or unrelated files in the sync directory

Because the requested workflow explicitly stages with `git add .`, the command will stage all changes in the sync directory, not only `*.json` profile files. That includes:

- new profile files
- modified profile files
- deleted tracked files
- any unrelated untracked or modified files already present in the sync directory

This is an intentional tradeoff in favor of matching the user's stated Git workflow exactly. The design should document this clearly so the behavior is not surprising.

If a narrower scope is needed later, a future version could stage only profile files or only files changed by the most recent export.

#### Pre-existing staged changes

If the sync repository already has files staged before `codexauth push` runs, `git add .` will preserve and potentially expand that staged set. The resulting commit may therefore include changes not created by `codexauth export`.

The initial design accepts this because the command is acting as a thin wrapper around standard Git commands, not as a full repository state manager. This should be documented as an operator responsibility.

#### Commit failure

If `git commit` fails after staging changes, the command should surface the Git error output and stop without attempting `git push`.

Examples include:

- missing user.name or user.email configuration
- commit hooks rejecting the change
- repository policy checks failing

The staged changes should be left intact so the user can inspect or retry manually.

#### Push failure

If `git push` fails, the command should surface the Git error output and exit with failure. The local commit will likely still exist, and the design should not attempt rollback.

This may happen because of:

- authentication failures
- network failures
- non-fast-forward rejections
- branch protection or remote hook failures

Avoiding rollback keeps the implementation simple and avoids destructive Git behavior.

#### Secrets and publication risk

The sync directory contains credential-bearing JSON files. A Git publish command makes it easier to propagate those files, so the design must explicitly acknowledge that:

- the tool does not redact or encrypt profile files before commit
- pushing the repository may expose credentials to anyone with access to the remote
- safe use depends on the user intentionally choosing a private, trusted repository

This risk already exists with export, but `push` makes publication one step easier, so the documentation should call it out directly.

### Error Handling Strategy for Git Commands

The Git pull/push commands should use a straightforward subprocess wrapper:

- capture stdout/stderr for each Git invocation
- treat non-zero exit status as a user-facing command failure
- include the failing Git subcommand in the error message
- avoid shell invocation when possible so arguments are passed directly

This keeps the implementation understandable while still producing useful diagnostics.

### Testing Strategy for Git Sync

Tests for the Git sync features should cover:

- missing sync directory configuration
- configured path that does not exist
- configured path that is not a Git repository
- successful `git pull`
- failed `git pull`
- successful no-op publish when there are no changes
- preflight pull failure stopping before export
- successful `add` -> `commit` -> `push` flow
- commit failure stopping before push
- push failure surfacing an error after a successful commit
- default commit message formatting

These tests can mock subprocess execution rather than requiring a real remote repository.

### Modified Time Semantics

File modified times are preserved for provenance and shown as context in unresolved conflicts. They never choose the newer credential set, because Git checkouts and file copies can change them. Operations intentionally do not all behave the same way:

- `add` preserves the source file's modified time, whether the source is `--file` or the default `~/.codex/auth.json`
- `import` preserves the external source file's modified time
- `export` preserves the local source file's modified time
- token refresh writes update modified time because the local stored profile contents genuinely changed
- `activate` does not rewrite the stored profile in `~/.codexauth/tokens`, so that stored file keeps its existing modified time
- `activate` preserves the selected profile's modified time when writing `~/.codex/auth.json`
- `save_codex_auth` writes generated local JSON and therefore uses a fresh modified time on `~/.codex/auth.json`

Credential freshness is determined separately using the shared credential comparison described below.

### Inode Preservation Semantics

For existing files, auth/profile updates should be expressed as in-place overwrites rather than path replacement:

- when `~/.codex/auth.json` already exists, `activate` and `save_codex_auth` should overwrite that file in place
- this preserves the existing inode so hard links to `~/.codex/auth.json` continue to observe updated contents
- the same in-place overwrite model should be used for stored profile JSON files and sync import/export targets when the destination already exists
- if a destination file does not exist yet, creating it necessarily produces a new inode

This is primarily a semantics guarantee and documentation point, not a requirement for temp-file swap logic.

### Credential Merge Decision Model

Sync and active-account reconciliation share one comparison:

1. Identical parsed JSON is a no-op; neither file is rewritten.
2. Different API-key profiles or incomplete credentials require a choice.
3. Account identifiers must agree, including available embedded ChatGPT account claims.
   ID-token issuer and subject must also agree when comparable. Matching account IDs or
   matching ID-token identity can confirm identity when the other signal is missing.
4. Compare access-token `iat`, ID-token `iat`, and timezone-aware `last_refresh` values
   where the same field is available on both sides. Equal fields do not break ties.
5. If all unequal comparable fields point to the same winner, copy its complete profile.
   Access, refresh, and ID tokens must remain together; never merge individual token fields.
6. Missing or equal timestamps with different contents, conflicting freshness signals,
   or unconfirmed identities require a choice. Filesystem timestamps never break the tie.

JWT claims are local comparison metadata, not signature or validity checks. Comparing
profiles does not contact the authentication service or rotate credentials. Prompts show
only credential timestamps and file dates, never raw token values.

`pull` imports newer external credentials and keeps newer local copies. `push` imports
new external profiles and newer external credentials before exporting newer local ones,
so both stores converge when comparisons are clear. Local hidden-profile preferences are
preserved during the push merge. Both flows reconcile the active account using the same
credential policy; noninteractive reconciliation reports ambiguity without prompting.

### Bulk Default UX

Import and export now follow a bulk-by-default workflow:

- all discovered candidates are processed automatically
- unattended sync reports and skips ambiguous comparisons; legacy commands can prompt for a manual choice
- non-conflicting profiles copy immediately
- conflicting profiles can still be skipped individually

This keeps common sync operations fast while preserving explicit confirmation for destructive cases.

## Token Refresh Design

The refresh subsystem exists to avoid usage lookups failing due to expired access tokens. The current policy is simple:

- if `last_refresh` is missing, refresh
- if `last_refresh` is invalid, refresh
- if the last refresh is 8 or more days old, refresh

On a successful refresh:

- `access_token` must be replaced from the refresh response
- `refresh_token` and `id_token` are replaced only when present in the refresh
  response; if either field is omitted, the previously stored value is retained
- `tokens.account_id` is preserved unless the implementation explicitly gains a
  trusted way to refresh it from provider data
- `last_refresh` is set to the current UTC timestamp
- the updated profile is saved back to local storage with a fresh modified timestamp
- the usage layer should surface that this profile was refreshed so higher-level
  CLI flows can treat it as a local update for sync prompting

On any failure, the original profile is preserved. This favors resilience over strict error propagation.

The refresh helper should also accept an optional caller-supplied async HTTP
client. That keeps standalone refresh behavior simple while letting batch usage
fetches reuse a shared client and letting tests inject a lightweight fake
client without patching network internals more broadly.

## Terminal UX

The user interface is optimized for quick local use:

- Click provides a small, familiar CLI surface.
- Rich renders a readable profile view with color and compact usage bars.
- `list` prints the current datetime immediately above the rendered profile view.
- The default `list` flow doubles as a launcher by offering an interactive activation prompt.
- Running without a command syncs before showing the table. `watch` combines repeated sync and usage checks, keeping the last table visible during refreshes.
- Both automatic flows publish tokens refreshed by usage lookup after a successful initial sync, without a confirmation prompt.
- When `list` updates local store state, either by reconciling the active
  `auth.json` back into store or by refreshing stale stored tokens during usage
  lookup, it should offer a follow-up `sync` when sync is configured and the
  command is interactive.
- The list display is responsive to terminal width: wide terminals keep the full table, medium terminals collapse to a compact table, and narrow terminals switch to a stacked format that remains usable on phone-width screens.

The display logic also distinguishes between:

- valid usage percentages
- expired credentials
- unavailable or non-applicable usage data

## Security and Privacy Considerations

This tool handles credential material, so the design leans on filesystem controls:

- stored profile files are written with `0600` permissions
- the active marker is also written with `0600` permissions
- profile data remains local to the machine

Known limitations:

- profile contents are stored unencrypted
- backups are also stored locally and unencrypted
- exported profiles may be committed and pushed to a Git remote without additional protection
- the tool trusts the structure of imported auth JSON beyond a minimal validation check

For a local developer utility, this is a pragmatic tradeoff, but it should be documented clearly for users.

## Error Handling Strategy

The project uses a forgiving model:

- missing profiles raise a custom `ProfileNotFoundError`
- CLI commands translate failures into user-facing `ClickException`s where appropriate
- refresh and usage networking failures fall back to unchanged profiles or `N/A` usage states
- import/export configuration errors should produce clear setup guidance rather than stack traces
- clearly newer credentials for the same account merge automatically; unattended sync skips ambiguity, while legacy commands can request confirmation

This keeps the tool useful even when network calls fail or remote APIs return unexpected errors.

## Testing Strategy

The existing test suite covers the main functional layers:

- CLI behavior and command output
- profile persistence and activation behavior
- modified-time preservation for `add`, `import`, and `export`
- modified-time updates for token refresh saves
- credential ordering independent of file times, conflicting or missing dates, and account-identity mismatches
- merging newer credentials in both directions before publication and updating active auth
- real temporary Git repositories covering rejected pushes, divergent token histories, hidden preferences, and unrelated user changes
- cross-process sync exclusion, noninteractive Git calls, bounded retries, and background-loop recovery
- automatic sync before default/watch usage lookups, publication of refreshed tokens, and local display after sync failures
- polling cadence including sync duration, newly synced accounts in empty stores, and buffered display during sync and lookup interruptions
- token refresh decision logic
- usage fetch success and failure cases
- usage batch concurrency limits and shared-client reuse
- refresh helper behavior with injected HTTP clients

Tests use:

- `pytest` for structure
- `CliRunner` for command tests
- `respx` and `httpx` mocking for network behavior

This is a good fit for the project because most important behavior is either filesystem-driven or request/response oriented.

## Tradeoffs

### Why filesystem storage instead of a database?

The tool only needs named blobs plus a small amount of metadata. Flat files make the implementation transparent, portable, and easy to inspect manually.

### Why store raw auth JSON?

Preserving the original structure reduces schema maintenance and lowers the risk of dropping fields needed by Codex or related APIs.

### Why best-effort networking?

Usage display is helpful, but profile switching is the primary job. The design avoids making usage failures block the main workflow.

## Future Improvements

- Add profile rename support.
- Add explicit backup restore support.
- Add stronger validation for imported auth files.
- Add optional encryption for stored profiles.
- Add clearer reporting when token refresh fails.
- Support exporting or importing profiles across machines.
- Add a non-interactive machine-readable output mode, such as JSON.
- Add diff or fingerprint views to compare conflicting profiles beyond modified time.
- Add batch flags such as `--all`, `--force`, or `--skip-existing` for scripting.

## Summary

`codexauthutil` is a compact local utility built around one core idea: treating Codex auth files as named profiles that can be saved, inspected, and switched safely. Its design prioritizes simplicity, low operational overhead, and a pleasant terminal experience, while adding just enough token and usage awareness to make multi-profile workflows practical.
