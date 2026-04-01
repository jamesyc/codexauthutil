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
- Support pull/push sync against a shared profile folder defined in a `.env` file.
- Keep the implementation easy to understand and maintain.

## Non-Goals

- Managing remote state or syncing profiles across machines.
- Supporting a database-backed storage system.
- Providing deep account management beyond auth file switching and usage lookup.
- Automating browser login or embedding a browser UI inside the CLI.

## Primary Use Cases

1. A user saves their current `~/.codex/auth.json` as `work`.
2. The user later saves a different account as `personal`.
3. The user runs `codexauth list` to see available profiles and current usage.
4. The user runs `codexauth use personal` to switch the active profile.
5. The tool copies the selected profile into `~/.codex/auth.json` and marks it active.
6. The user runs `codexauth pull` to import profiles from a shared Git-backed folder configured in `.env`.
7. The user runs `codexauth push` to export local profiles and publish them from that same folder.
8. The user runs `codexauth login` to generate a login URL for a new profile. The user pastes that URL into a browser, logs in, and reaches a localhost redirect that is expected to fail. The user copies the full callback URL from the browser address bar back into the CLI. The tool exchanges the authorization code for tokens and saves the result as a normal profile.

## High-Level Architecture

The system is organized into a few focused modules:

- `codexauth/cli.py`: Click-based command definitions and command orchestration.
- `codexauth/config.py`: `.env` loading and sync-directory resolution.
- `codexauth/store.py`: Filesystem storage, active profile tracking, and activation logic.
- `codexauth/usage.py`: Usage retrieval and concurrent usage fetching across profiles.
- `codexauth/refresh.py`: Refresh-token handling for ChatGPT OAuth credentials.
- `codexauth/oauth.py`: manual OAuth bootstrap helpers, callback validation, and code exchange.
- `codexauth/display.py`: Rich-based table rendering and interactive prompt behavior.
- `codexauth/sync.py`: import/export candidate discovery, modified-time formatting, and metadata-preserving file copies.

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

## Command Design

### `codexauth list`

- Lists all stored profiles.
- Optionally fetches usage data unless `--no-usage` is passed.
- Shows the current active profile in a width-aware Rich view.
- For ChatGPT-backed profiles with live usage data, shows four usage-related columns:
  - 5-hour usage percentage
  - time left until the 5-hour window resets
  - weekly usage percentage
  - time left until the weekly window resets
- Uses the full multi-column table on wide terminals, a compact table on medium widths, and a stacked per-profile layout on narrow screens so phone-sized terminals remain readable.
- Can prompt the user to activate a profile interactively unless `--no-interactive` is passed.

### `codexauth add <name>`

- Reads an auth file from `--file` or defaults to `~/.codex/auth.json`.
- Performs a lightweight validity check by requiring `auth_mode` or `tokens`.
- Copies the source auth file into local storage under the given name.
- Preserves the source file's modified timestamp so imported profile age stays meaningful.

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
- When an import would overwrite an existing local profile, shows both sides' last modified timestamps before confirmation.

### `codexauth export`

- Intended primarily as a lower-level testing and debugging command rather than the main user workflow.
- Reads the external profile directory path from `.env`.
- Exports all available local profiles by default.
- Detects name collisions with profiles already present in the external directory.
- When an export would overwrite an existing external profile, shows both sides' last modified timestamps before confirmation.

### `codexauth pull`

- Reads the external profile directory path from `.env`.
- Treats that directory as a Git working tree used to fetch remote profile changes.
- Changes into the sync directory and runs `git pull`.
- Imports all profiles from the sync directory after a successful pull.
- Prompts only for overwrite cases during the import step.

### `codexauth push`

- Reads the external profile directory path from `.env`.
- Exports all local profiles into the sync directory first.
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

- if present on both sides, `tokens.account_id` is authoritative and must match
- if `tokens.account_id` is missing on either side, `(iss, sub)` from
  `id_token` may be used as a backup identity check, and both fields must match

If an overwrite sees that both `tokens.account_id` and `(iss, sub)` from
`id_token` disagree, the tool should surface the update and ask the user to
confirm before replacing the saved profile; otherwise it can be an automatic
update. When identity cannot be confirmed, the tool should not auto-overwrite.
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
   - if `tokens.account_id` is present on both sides and matches, treat that as
     authoritative
   - otherwise, if `(iss, sub)` from `id_token` is present on both sides and
     matches, treat that as a match
   - if modified times are equal but contents differ, do not guess which side is
     newer; require confirmation instead

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
byte-identical. In that case, the tool should not rewrite either file and
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
4. Send a request to the usage endpoint.
5. Extract the standard primary and secondary usage windows from the top-level `rate_limit` object.
6. Extract any named additional limits from `additional_rate_limits[]`, preserving each entry's `limit_name`.
7. Apply presentation-friendly labels in the renderer where needed; for example, the UI shortens `GPT-5.3-Codex-Spark` to `Spark` and `GPT-5.3-Codex-Spark Weekly` to `Spark Weekly`.

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
5. For profiles that already exist locally, show local and external modified times and ask whether to overwrite.
6. Copy accepted profiles into `~/.codexauth/tokens`.
7. Preserve the source file's modified timestamp on the imported local copy.
8. If the sync directory's `.gitignore` blacklists a profile JSON file, treat
   that as a removal instruction and run the equivalent of `codexauth remove
   <name>` against the local store for that profile.

Key UX requirement:

- overwrites should be explicit, not silent
- the user should be able to skip individual conflicting profiles
- modified timestamps should help the user decide which copy is newer

### Export Flow

1. Read the external profile directory from `.env`.
2. Enumerate local stored profiles.
3. Compare them against files already present in the external directory.
4. For profiles that do not exist externally, export directly.
5. For profiles that already exist externally, show local and external modified times and ask whether to overwrite.
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
10. Prompt only for overwrite cases during import or for reconciliation cases
   where identity cannot be safely confirmed or recency is ambiguous.
11. Print a success message summarizing what happened.

### Push Flow

The push command should behave as follows:

1. Read the external profile directory from `.env`.
2. Export all local profiles into the sync directory.
3. Fail with a clear error if the directory does not exist.
4. Fail with a clear error if the directory is not inside a Git working tree.
5. Run `git add .` from that directory.
6. Check whether staging produced any changes.
7. If there are no staged changes, print a no-op message and stop without committing or pushing.
8. If there are staged changes, run `git commit -m <message>`.
9. Run `git push`.
10. Print a success message summarizing what happened.

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

The design should not attempt to resolve conflicts or modify the repository state automatically.

#### Pull succeeds but import is partially declined

After a successful `git pull`, the subsequent import step may still hit overwrite prompts. Users may accept some overwrites and decline others, producing a partial local update.

This is acceptable because overwrite confirmation is more important than forcing the local store to mirror the sync directory exactly.

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

If `git add .` is successful but there is nothing to commit, the command should not treat that as an error. It should print a message such as "No changes to commit" and exit successfully without calling `git push`.

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
- successful `add` -> `commit` -> `push` flow
- commit failure stopping before push
- push failure surfacing an error after a successful commit
- default commit message formatting

These tests can mock subprocess execution rather than requiring a real remote repository.

### Modified Time Semantics

The implementation uses file modified time as a lightweight signal for profile provenance during sync decisions, so operations intentionally do not all behave the same way:

- `add` preserves the source file's modified time, whether the source is `--file` or the default `~/.codex/auth.json`
- `import` preserves the external source file's modified time
- `export` preserves the local source file's modified time
- token refresh writes update modified time because the local stored profile contents genuinely changed
- `activate` does not rewrite the stored profile in `~/.codexauth/tokens`, so that stored file keeps its existing modified time
- `activate` preserves the selected profile's modified time when writing `~/.codex/auth.json`
- `save_codex_auth` writes generated local JSON and therefore uses a fresh modified time on `~/.codex/auth.json`

This gives import/export flows stable timestamps while still allowing local token refreshes to indicate a real local update.

### Inode Preservation Semantics

For existing files, auth/profile updates should be expressed as in-place overwrites rather than path replacement:

- when `~/.codex/auth.json` already exists, `activate` and `save_codex_auth` should overwrite that file in place
- this preserves the existing inode so hard links to `~/.codex/auth.json` continue to observe updated contents
- the same in-place overwrite model should be used for stored profile JSON files and sync import/export targets when the destination already exists
- if a destination file does not exist yet, creating it necessarily produces a new inode

This is primarily a semantics guarantee and documentation point, not a requirement for temp-file swap logic.

### Overwrite Decision Model

For both import and export, the tool should surface enough metadata to support a safe overwrite choice:

- profile name
- source modified timestamp
- destination modified timestamp
- whether the destination already exists

This allows prompts such as:

- import `work` from external store modified at `2026-03-10 09:15` over local copy modified at `2026-03-08 18:42`?
- export `personal` from local store modified at `2026-03-12 07:30` over external copy modified at `2026-03-01 14:05`?

The exact prompt format can vary, but the timestamps should be shown whenever overwriting is possible.

### Bulk Default UX

Import and export now follow a bulk-by-default workflow:

- all discovered candidates are processed automatically
- only overwrite cases prompt the user
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
- When `list` updates local store state, either by reconciling the active
  `auth.json` back into store or by refreshing stale stored tokens during usage
  lookup, it should offer a follow-up `push` when sync is configured and the
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
- overwrite situations should be handled through confirmation instead of implicit replacement

This keeps the tool useful even when network calls fail or remote APIs return unexpected errors.

## Testing Strategy

The existing test suite covers the main functional layers:

- CLI behavior and command output
- profile persistence and activation behavior
- modified-time preservation for `add`, `import`, and `export`
- modified-time updates for token refresh saves
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
