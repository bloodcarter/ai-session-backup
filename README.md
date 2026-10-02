# ai-session-backup

Backs up every Claude and Codex session on this Mac, plus the artifacts in the Claude account, every
8 hours. Copies go to `~/Backups/ai-sessions` and then to `iCloud Drive/AI session backup`. The tool
never deletes anything in either place.

## What it backs up

- **Claude Code sessions** from `~/.claude/projects` (terminal and desktop app, with subagents), plus
  prompt history, file snapshots, the 2025 session database, desktop-app session metadata, Cowork
  sessions and any Claude folders it finds in Cursor worktrees.
- **Codex sessions**, active and archived, with thread names, memories, attachments and a snapshot of
  the thread database (one more copy kept per month).
- **Claude artifacts**: each artifact's own published files and uploaded assets. Docs artifacts also
  get each tab's text as Markdown and HTML. A new dated snapshot is written only when an artifact
  changes; unchanged files are hard links to the previous snapshot.

## How it works

1. **Sessions.** Files are copied as APFS clones, so unchanged data costs no disk space. A session
   log that is rewritten instead of appended keeps its old copy in `_replaced/<date>/`.
   Databases are copied with SQLite's backup API, or cloned whole when the owning app has closed them.
2. **Artifacts.** Short headless Claude Code runs (Haiku) call the Artifact tool and the Claude Docs
   connector. Each run can only read: publishing and deleting are blocked, and all other tools are
   refused. Every download is checked against the SHA-256 the tool reports.
3. **iCloud.** New and changed files are cloned into iCloud Drive. Each run asks iCloud which files
   it has uploaded and raises a notification for any file still not uploaded 3 days after copying.

Any failure raises a macOS notification. `state.json` records the last run, and `logs/` holds
the details.

## Requirements

- macOS on APFS, with `/usr/bin/python3` (Xcode Command Line Tools). Standard library only.
- Claude Code in Terminal, logged in once with `claude auth login` (artifacts step only).
- iCloud Drive turned on (iCloud step only).

## Install

```bash
mkdir -p ~/Backups/ai-sessions/bin
ln -sf ~/repos/ai-session-backup/ai_session_backup.py ~/Backups/ai-sessions/bin/ai_session_backup.py
cp ~/repos/ai-session-backup/com.vlad.ai-session-backup.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vlad.ai-session-backup.plist
```

The plist is copied rather than linked because launchd may refuse a symlinked job file. After
editing it, copy it again and reload the job with `launchctl bootout` followed by `launchctl bootstrap`.

## Use

```bash
launchctl kickstart gui/$(id -u)/com.vlad.ai-session-backup   # run now
launchctl print gui/$(id -u)/com.vlad.ai-session-backup       # schedule, run count, last exit code
python3 ai_session_backup.py --only sessions                  # or: artifacts, icloud
```

## Things that can change under it

- Claude Code offers the Artifact tool only when it runs as the desktop app's engine, and the
  `enableArtifact` setting cannot turn it on. The headless runs therefore set
  `CLAUDE_CODE_ENTRYPOINT=claude-desktop`. A future Claude Code release may change this; the
  artifacts step would then fail with a notification.
- The artifact list dates changes by day in UTC. The tool records its own checks in UTC and
  re-checks anything listed within a day of the last check.
- The Claude Docs connector is sometimes not yet connected when a headless run starts. Docs exports
  retry three times, and a Docs artifact is never saved without its text.
- Claude Code saves a large tool result to a file and returns a stub. The tool reads the file, then
  removes the run folders its headless runs leave in `~/.claude/projects`.

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.vlad.ai-session-backup
rm ~/Library/LaunchAgents/com.vlad.ai-session-backup.plist
```

The backups in `~/Backups/ai-sessions` and iCloud Drive stay where they are.
