# AGENTS.md

## Project purpose

This repository contains a local Canvas course-material sync tool built with Python and Streamlit. It downloads and organizes authorized course materials, assignments, pages, and related files while preserving local changes.

The project is intended to run locally on both macOS and Windows. Windows is the user's primary/reference environment; macOS is also supported for mobile development and testing.

## Git workflow

Long-lived branches:

- `main`: stable, usable releases only.
- `dev`: integration branch for ongoing development.
- `feature/*`: one branch per new feature or substantial change, created from the latest `dev`.
- `fix/*`: one branch per focused bug fix, created from the latest `dev` unless the user explicitly requests a hotfix from `main`.

Rules:

1. Never develop directly on `main`.
2. Before starting a new feature, check the current branch and working tree.
3. If the working tree contains unrelated uncommitted changes, do not discard, reset, overwrite, or stash them without the user's explicit approval.
4. For a new task, start from the latest `dev` when Git/network access is available, then create a descriptive branch such as `feature/delete-files` or `fix/windows-path`.
5. Keep each branch focused on one task. Avoid unrelated refactors.
6. Never force-push.
7. Never merge into `main` unless the user explicitly asks for a release/merge.
8. Do not delete branches unless the user explicitly asks.
9. When a task is complete, summarize changed files, tests run, remaining risks, and the exact branch/commit state.
10. If pushing is requested or clearly part of the active workflow, push only the current feature/fix branch unless the user explicitly requests otherwise.

## Development priorities

- Preserve existing working behavior unless the task explicitly changes it.
- Prefer small, reviewable changes over broad rewrites.
- Reuse existing project abstractions before adding new frameworks or dependencies.
- Keep the UI understandable for a non-developer end user.
- Maintain backward compatibility with existing local databases/configuration whenever practical.
- Do not silently delete, overwrite, or migrate user data without a safe migration, backup, preview, or confirmation path.

## Security and privacy

This project handles school accounts and local course files. Treat authentication material and local data as sensitive.

Never commit or expose:

- Canvas access tokens.
- Cookies or browser sessions.
- Passwords, SSO credentials, MFA codes, or private keys.
- `.streamlit/secrets.toml`.
- `session.json`, `config.json`, browser profiles, local databases, downloaded course files, or other ignored runtime data.
- Real user/course data in tests, examples, screenshots, logs, or documentation unless the user explicitly provides sanitized material for that purpose.

Do not weaken TLS verification or implement authentication bypasses. Do not automate password/MFA entry. Authentication should use authorized Canvas APIs/tokens or a user-completed browser login flow.

Before adding a file to Git, check `.gitignore` and `git status` for accidental sensitive files.

## Platform requirements

Supported development/runtime targets include macOS and Windows.

- Python requirement: 3.11 or newer.
- Main application: Streamlit.
- Browser automation: Playwright with existing browser fallback behavior where applicable.
- Tests: pytest.

Windows is the primary reference environment for real-world use. Changes involving filesystem paths, launching files/folders, trash/recycle-bin behavior, browser discovery, subprocesses, background reminders, or startup scripts must be reviewed for Windows compatibility.

Use `pathlib` and existing cross-platform helpers instead of hard-coded `/` or `C:\\` paths where possible.

Do not assume that a feature tested only on macOS is verified on Windows. Clearly label platform-specific verification gaps.

## Testing

Before considering a code change complete:

1. Run the relevant focused tests first.
2. Run the full test suite when practical:

   `pytest`

3. If dependencies are isolated in the project virtual environment, use the environment's Python/pytest rather than changing global Python packages.
4. Add or update tests for new behavior and bug fixes when reasonably possible.
5. Do not modify tests merely to make failures disappear; fix the underlying behavior unless the specification intentionally changed.
6. Report any tests that could not be run and why.

For authentication, SSO, real Canvas API behavior, browser login, and Windows-specific integration, automated tests are not a substitute for real-device validation. Do not claim these are verified unless they were actually tested in the relevant environment.

## Real Canvas access

Do not access a real school account, perform destructive actions, delete real course files, or change remote Canvas content unless the user explicitly asks for that real-world action.

Use mock/demo data for development whenever it is sufficient. If real-account validation is necessary, tell the user exactly what must be tested locally and what data/permission is involved.

## File and data safety

The application is designed to preserve local modifications and downloaded materials.

- Never silently overwrite a locally modified managed file.
- Never recursively delete user-selected directories without the project's existing safety checks and confirmation flow.
- Prefer trash/recycle-bin behavior over permanent deletion when that is the project's established behavior.
- Preserve notebook/CSV companion-file relationships and other existing grouping logic.
- Treat path traversal, symlinks, invalid filenames, case collisions, and Windows reserved names as safety-sensitive cases.

## Dependencies

Before adding a dependency:

- Check whether the standard library or an existing dependency already solves the problem.
- Avoid large dependencies for small features.
- Update both dependency metadata and documentation when required.
- Keep macOS and Windows installation/startup paths working.

## Documentation

Update `README.md` when a user-visible feature, setup step, platform requirement, limitation, or workflow changes.

Keep instructions concrete and suitable for a user who is not expected to understand the internal architecture.

## Completion checklist

For each substantial task, verify:

- Correct branch used.
- No accidental secrets or runtime data tracked.
- Existing behavior preserved unless intentionally changed.
- Relevant tests added/updated.
- Relevant tests pass.
- Cross-platform implications reviewed.
- README updated if user-facing behavior changed.
- No merge into `main` unless explicitly requested.
- Final response states what changed, what was tested, and what still requires real Windows/Canvas validation.
