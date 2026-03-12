# codexauthutil Design Document

## Overview

`codexauthutil` is a small command-line utility for managing multiple OpenAI Codex authentication profiles. It is designed for people who switch between different `auth.json` identities, such as separate work and personal accounts, and want a fast way to:

- store named snapshots of Codex authentication data
- activate one profile into `~/.codex/auth.json`
- inspect which profile is currently active
- view quota usage for ChatGPT-backed profiles

The project is intentionally lightweight. It is a local tool, not a service, and it uses the filesystem as its primary storage layer.

## Goals

- Provide a simple CLI for saving and switching between named auth profiles.
- Preserve the currently installed `~/.codex/auth.json` before replacing it.
- Surface usage information in a human-friendly terminal view.
- Refresh ChatGPT OAuth tokens automatically when they are stale.
- Support importing from and exporting to a shared profile folder defined in a `.env` file.
- Keep the implementation easy to understand and maintain.

## Non-Goals

- Managing remote state or syncing profiles across machines.
- Supporting a database-backed storage system.
- Providing deep account management beyond auth file switching and usage lookup.
- Replacing the Codex authentication flow itself.

## Primary Use Cases

1. A user saves their current `~/.codex/auth.json` as `work`.
2. The user later saves a different account as `personal`.
3. The user runs `codexauth list` to see available profiles and current usage.
4. The user runs `codexauth use personal` to switch the active profile.
5. The tool copies the selected profile into `~/.codex/auth.json` and marks it active.
6. The user imports selected profiles from an external folder configured in `.env`.
7. The user exports selected local profiles to that same external folder for backup or sharing across environments.

## High-Level Architecture

The system is organized into a few focused modules:

- `codexauth/cli.py`: Click-based command definitions and command orchestration.
- `codexauth/store.py`: Filesystem storage, active profile tracking, and activation logic.
- `codexauth/usage.py`: Usage retrieval and concurrent usage fetching across profiles.
- `codexauth/refresh.py`: Refresh-token handling for ChatGPT OAuth credentials.
- `codexauth/display.py`: Rich-based table rendering and interactive prompt behavior.

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

## Command Design

### `codexauth list`

- Lists all stored profiles.
- Optionally fetches usage data unless `--no-usage` is passed.
- Shows the current active profile in a Rich table.
- Can prompt the user to activate a profile interactively unless `--no-interactive` is passed.

### `codexauth add <name>`

- Reads an auth file from `--file` or defaults to `~/.codex/auth.json`.
- Performs a lightweight validity check by requiring `auth_mode` or `tokens`.
- Saves the profile under the given name.

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

- Reads the external profile directory path from `.env`.
- Lists available external profiles and lets the user select which ones to import.
- Detects name collisions with locally stored profiles.
- When a selected import would overwrite an existing local profile, shows both sides' last modified timestamps before confirmation.
- Supports importing only a subset of available profiles rather than forcing a bulk sync.

### `codexauth export`

- Reads the external profile directory path from `.env`.
- Lists available local profiles and lets the user select which ones to export.
- Detects name collisions with profiles already present in the external directory.
- When a selected export would overwrite an existing external profile, shows both sides' last modified timestamps before confirmation.
- Supports exporting only a subset of local profiles.

## Activation Flow

Profile activation is the core operation:

1. Resolve `~/.codexauth/tokens/<name>.json`.
2. Fail if the profile does not exist.
3. If `~/.codex/auth.json` already exists, copy it to `~/.codexauth/auth.json.bak`.
4. Copy the selected profile into `~/.codex/auth.json`.
5. Restrict file permissions to `0600`.
6. Write the active profile name to `~/.codexauth/active`.

This keeps the switch operation explicit and reversible at the file level.

## Usage Retrieval Design

Usage lookup is only attempted for ChatGPT-backed profiles. For each eligible profile:

1. Read the access token and optional account ID.
2. Check whether the profile appears stale using `last_refresh`.
3. Refresh tokens first if needed.
4. Send a request to the usage endpoint.
5. Extract primary and secondary usage percentages from the response.

The CLI fetches usage concurrently for all profiles with `asyncio.gather`, which keeps the list command responsive even when several profiles are stored.

## Import and Export Design

The import/export feature extends the existing filesystem-first design by introducing an optional shared folder configured through a `.env` file.

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
4. Present the user with a selection UI so they can choose which profiles to import.
5. For profiles that do not exist locally, import directly.
6. For profiles that already exist locally, show local and external modified times and ask whether to overwrite.
7. Copy selected profiles into `~/.codexauth/tokens`.

Key UX requirement:

- overwrites should be explicit, not silent
- the user should be able to skip individual conflicting profiles
- modified timestamps should help the user decide which copy is newer

### Export Flow

1. Read the external profile directory from `.env`.
2. Enumerate local stored profiles.
3. Compare them against files already present in the external directory.
4. Present the user with a selection UI so they can choose which profiles to export.
5. For profiles that do not exist externally, export directly.
6. For profiles that already exist externally, show local and external modified times and ask whether to overwrite.
7. Copy selected profiles into the external directory.

The export flow mirrors the import flow so users only need to learn one mental model.

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

### Selection UX

The current project already uses a simple interactive prompt for activation. Import/export can follow the same lightweight philosophy:

- show a numbered list of candidate profiles
- allow selecting one, many, or all profiles
- identify which entries would overwrite an existing destination
- annotate overwrite candidates with last modified information

For non-interactive workflows, a future extension could support explicit names or flags, but the initial design should prioritize clarity for interactive use.

## Token Refresh Design

The refresh subsystem exists to avoid usage lookups failing due to expired access tokens. The current policy is simple:

- if `last_refresh` is missing, refresh
- if `last_refresh` is invalid, refresh
- if the last refresh is 8 or more days old, refresh

On a successful refresh:

- `access_token`, `refresh_token`, and `id_token` are updated if present in the response
- `last_refresh` is set to the current UTC timestamp
- the updated profile is saved back to local storage

On any failure, the original profile is preserved. This favors resilience over strict error propagation.

## Terminal UX

The user interface is optimized for quick local use:

- Click provides a small, familiar CLI surface.
- Rich renders a readable table with color and compact usage bars.
- The default `list` flow doubles as a launcher by offering an interactive activation prompt.

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
- token refresh decision logic
- usage fetch success and failure cases

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
