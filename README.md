# ai-session-backup

Keeps every Claude Code and Codex session log on your Mac, and every artifact in your claude.ai
account, in a local backup folder and in iCloud Drive. It runs in the background every 8 hours and
never deletes anything it has saved.

## Why this exists

Your sessions with coding agents record how the work got done: the reasoning, the research, the dead
ends and the commands that worked. That record is easier to lose than it looks:

- **Claude Code deletes session logs after 30 days of inactivity** by default (the
  `cleanupPeriodDays` setting). Recent versions spare sessions started from the Claude desktop app;
  earlier versions deleted those too, and the rule can change again.
- **Deleting a session in the Claude desktop app deletes its log at once.**
- **Artifacts live only in your claude.ai account.** If you lose access to the account, they are gone.
- **Codex keeps its sessions, but in one folder on one disk,** with no second copy.

ai-session-backup copies all of it into `~/Backups/ai-sessions` every 8 hours, keeps every version it
has seen, and copies that folder into iCloud Drive.

## What it backs up

- **Claude Code sessions** from `~/.claude/projects` (terminal and desktop app, with subagents), your
  prompt history, file snapshots, sessions in Claude Code's older 2025 database, desktop-app session
  metadata, Cowork sessions, and Claude folders it finds in Cursor worktrees.
- **Codex sessions**, active and archived, with thread names, memories, attachments and a snapshot of
  the thread database (plus one copy kept per month).
- **Claude artifacts**: each artifact's own published files and uploaded images. Docs artifacts also
  get every tab's text as Markdown and HTML.

It does not cover anything that never reaches your disk: claude.ai chats, cloud Claude Code
sessions, and the chat, memory and files of Claude Code projects.

## How it works

1. **Sessions.** Files are copied as APFS clones, so unchanged data takes no extra disk space. Files
   deleted at the source stay in the backup. A session log that is rewritten instead of appended
   keeps its old copy in `_replaced/<date>/`. Databases are copied with SQLite's backup API, or cloned
   whole when the app that owns them has closed them.
2. **Artifacts.** Short headless Claude Code runs on Haiku call the Artifact tool and the Claude Docs
   connector. Those runs can only read: publishing and deleting are blocked, and every other tool is
   refused. Downloaded files and images are checked against the SHA-256 the tool reports. An artifact gets a new
   dated snapshot only when it changes, and files that did not change are hard links to the
   previous snapshot.
3. **iCloud.** New and changed files are cloned into `iCloud Drive/AI session backup`. Each run asks
   iCloud which files it has uploaded, and notifies you about any file still not uploaded 3 days
   after it was copied.

Any failure shows a macOS notification. `~/Backups/ai-sessions/state.json` records the last run, and
`~/Backups/ai-sessions/logs/` holds the details.

## Before you install

- **Unofficial.** This project is not affiliated with Anthropic or OpenAI.
- **The artifact step relies on undocumented Claude Code behavior.** Claude Code offers its Artifact
  tool only when it runs as the Claude desktop app's engine, so the headless runs set
  `CLAUDE_CODE_ENTRYPOINT=claude-desktop`. The step also reads the text the tool returns. A Claude Code
  update can break either; the artifact step then fails with a notification, and the session backup
  and iCloud copy keep working.
- **The artifact step uses your own Claude usage**: a few short Haiku runs per artifact that changed,
  plus a re-export of each Docs artifact on every run.
- **Backups hold everything your sessions saw**, including any secret that appeared in a command's
  output. If you keep the iCloud copy, turn on Advanced Data Protection (System Settings, your Apple
  Account, iCloud) so Apple cannot read it.
- **Provided as is.** I use it daily and fix what breaks for me, but I promise no support.

## Requirements

- macOS on APFS, with the Xcode Command Line Tools (for `/usr/bin/python3`). Standard library only.
- For artifacts: Claude Code in Terminal, logged in once with `claude auth login`.
- For the iCloud copy: iCloud Drive turned on, with room for your sessions.

## Install

```bash
git clone https://github.com/bloodcarter/ai-session-backup ~/repos/ai-session-backup
~/repos/ai-session-backup/install.sh
```

Leave steps out with `--skip artifacts`, `--skip icloud` or `--skip artifacts,icloud`. To see the
launchd job file before installing it, run `install.sh --print-plist`.

The installer links `~/Backups/ai-sessions/bin/ai_session_backup.py` to your clone, so a `git pull`
takes effect on the next run.

Also tell Claude Code to keep its own logs longer, by adding this to `~/.claude/settings.json`:

```json
"cleanupPeriodDays": 3650
```

## Use

```bash
launchctl kickstart gui/$(id -u)/com.$USER.ai-session-backup   # run now
launchctl print gui/$(id -u)/com.$USER.ai-session-backup       # schedule, runs, last exit code
python3 ai_session_backup.py --only sessions                   # one step by hand: sessions, artifacts or icloud
```

## Where things go

| In `~/Backups/ai-sessions/` | What it holds |
|---|---|
| `claude/` | Claude Code, desktop app and Cowork sessions, history and file snapshots |
| `codex/` | Codex sessions, thread index, memories, attachments and database snapshots |
| `claude-artifacts/<artifact id>/<date>/` | one snapshot per day an artifact changed (`<date>.2` for a second change that day) |
| `_replaced/<date>/` | old copies of session logs that were rewritten |
| `state.json`, `logs/` | last run result and details |

Every session is a plain `.jsonl` file you can open, search or copy back to its original folder.

## Things that can change under it

- The artifact list dates changes by day in UTC. The tool records its checks in UTC and checks
  anything listed within a day of its last check again.
- The Claude Docs connector is sometimes not connected yet when a headless run starts. Docs exports
  retry three times, and a Docs artifact is never saved without its text.
- Artifacts shared with you from another account (for example after you switch Claude accounts) do not
  appear in your artifact list, though their links still work. The tool therefore also checks every
  artifact it has already saved, by its link. If one stops opening, or the Claude Docs connector refuses
  a Docs artifact's text, you get one notification, its last snapshot is kept, and it is retried daily.
- Uploaded assets are listed 50 per page and downloaded one at a time. The tool reads every page and
  downloads only assets it does not already have, matched by checksum.
- Claude Code saves a large tool result to a file and returns a stub instead. The tool reads that file,
  then removes the folders its headless runs leave in `~/.claude/projects`.

## Uninstall

```bash
~/repos/ai-session-backup/install.sh --uninstall
```

Your backups in `~/Backups/ai-sessions` and in iCloud Drive stay where they are.

## License

MIT. See [LICENSE](LICENSE).
