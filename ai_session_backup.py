#!/usr/bin/python3
"""Back up Claude and Codex session logs, plus the artifacts in your Claude account.

Everything goes to ~/Backups/ai-sessions, and a second copy to iCloud Drive/AI session backup.
Nothing is ever deleted in either place:
- files removed at the source stay in the backup;
- a session log (.jsonl) that is rewritten instead of appended keeps its old copy in _replaced/<date>/;
- copies are APFS clones, so unchanged data takes no extra disk space.

Artifacts are fetched through Claude Code's own Artifact tool in a locked-down headless run
(Haiku, no shell, no file tools, nothing saved to session history). That step needs the terminal
Claude Code to be logged in (`claude auth login`); without a login it is skipped and you get a notification.

Usage: ai_session_backup.py [--only sessions|artifacts|icloud]
Runs every 8 hours (03:30, 11:30, 19:30) from ~/Library/LaunchAgents/com.vlad.ai-session-backup.plist.
"""
import argparse
import base64
import ctypes
import datetime
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

HOME = Path.home()
ROOT = HOME / "Backups" / "ai-sessions"
APP = HOME / "Library" / "Application Support" / "Claude"

# Folders mirrored as-is: (source, destination under ROOT)
TREES = [
    (HOME / ".claude/projects", "claude/projects"),  # Claude Code sessions: terminal and desktop app
    (HOME / ".claude/file-history", "claude/file-history"),  # file snapshots behind /rewind
    (HOME / ".claude/transcripts", "claude/transcripts"),  # OpenCode logs kept in ~/.claude
    (APP / "claude-code-sessions", "claude/desktop-app/code-sessions"),  # desktop app titles and account mapping
    (APP / "local-agent-mode-sessions", "claude/desktop-app/cowork-sessions"),  # Cowork sessions
    (HOME / ".codex/sessions", "codex/sessions"),
    (HOME / ".codex/archived_sessions", "codex/archived_sessions"),
    (HOME / ".codex/memories", "codex/memories"),
    (HOME / ".codex/attachments", "codex/attachments"),
]
FILES = [
    (HOME / ".claude/history.jsonl", "claude/history.jsonl"),
    (HOME / ".codex/history.jsonl", "codex/history.jsonl"),
    (HOME / ".codex/session_index.jsonl", "codex/session_index.jsonl"),
]
# Claude config folders outside ~/.claude, e.g. one a Cursor worktree created
EXTRA_PROJECT_GLOBS = [str(HOME / ".cursor/worktrees/*/*/.claude/projects")]
# Databases are snapshotted with SQLite's backup API (safe while in use)
SQLITE_FILES = [(HOME / ".claude/__store.db", "claude/__store.db")]  # 2025 Claude Code sessions
CODEX_DB_GLOBS = ["state_*.sqlite", "memories_*.sqlite", "goals_*.sqlite"]  # thread titles and metadata
# thread_history_*.sqlite and logs_*.sqlite are left out: rebuilt from the session logs, and 10 GB.

# Second copy in iCloud Drive: a one-way mirror of ROOT that never deletes there
ICLOUD_ROOT = HOME / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
ICLOUD = ICLOUD_ROOT / "AI session backup"
STAGING = HOME / "Backups" / ".icloud-staging"  # temp copies land here, outside iCloud Drive, so half-written files never upload

MIN_FREE_BYTES = 10 * 1024 ** 3
TODAY = datetime.date.today().isoformat()  # local date: names snapshot folders and log files
CHECK_DATE = datetime.datetime.utcnow().date().isoformat()  # UTC, like the dates in the artifact list
PATH = os.pathsep.join([str(HOME / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"])

_libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
_libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]


def log(msg):
    line = "%s %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    logs = ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / ("backup-%s.log" % TODAY[:7]), "a") as f:
        f.write(line + "\n")


def notify(msg):
    subprocess.run(["/usr/bin/osascript", "-e", 'display notification "%s" with title "AI session backup"'
                    % msg.replace('"', "'")], capture_output=True, timeout=30)


def new_stats():
    return {"copied": 0, "updated": 0, "unchanged": 0, "preserved": 0, "errors": 0}


# ---------- sessions ----------

def clone(src, dst, tmp_dir=None):
    """Copy src to dst as an APFS clone (falls back to a plain copy), replacing dst atomically."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = (tmp_dir or dst.parent) / (dst.name + ".backup-tmp-%d" % os.getpid())
    if os.path.lexists(tmp):
        tmp.unlink()
    if _libc.clonefile(os.fsencode(src), os.fsencode(tmp), 0) != 0:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def sha256(path, limit=None):
    h, left = hashlib.sha256(), limit
    with open(path, "rb") as f:
        while left is None or left > 0:
            chunk = f.read(1 << 20 if left is None else min(1 << 20, left))
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
    return h.hexdigest()


def is_append(src, dst, old_size, new_size):
    """True when src still starts with everything the backup copy holds."""
    return new_size >= old_size and sha256(src, old_size) == sha256(dst)


def keep_old(dst, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    n, final = 1, target
    while os.path.lexists(final):
        n += 1
        final = target.with_name("%s.%d" % (target.name, n))
    os.replace(dst, final)


def mirror_file(src, dst, root, stats):
    st = src.stat()
    try:
        dt = dst.stat()
    except FileNotFoundError:
        dt = None
    if dt is not None and dt.st_size == st.st_size and dt.st_mtime_ns == st.st_mtime_ns:
        stats["unchanged"] += 1
        return
    if dt is not None and dst.name.endswith(".jsonl") and not is_append(src, dst, dt.st_size, st.st_size):
        keep_old(dst, root / "_replaced" / TODAY / dst.relative_to(root))
        stats["preserved"] += 1
    clone(src, dst)
    stats["copied" if dt is None else "updated"] += 1


def transient(name):
    return name.endswith(".tmp") or ".tmp." in name or ".tmp-" in name or name.endswith((".lock", "-shm"))


def mirror_tree(src, dst, root, stats):
    def onerror(err):
        stats["errors"] += 1
        log("  read error: %s" % err)

    for dirpath, dirnames, filenames in os.walk(src, onerror=onerror):
        rel = Path(dirpath).relative_to(src)
        for name in filenames:
            if transient(name):
                continue
            s, d = Path(dirpath) / name, dst / rel / name
            try:
                if s.is_symlink():
                    if not os.path.lexists(d):
                        d.parent.mkdir(parents=True, exist_ok=True)
                        os.symlink(os.readlink(s), d)
                        stats["copied"] += 1
                elif s.is_file():
                    mirror_file(s, d, root, stats)
            except FileNotFoundError:
                pass  # removed while we were copying; it will be gone from the source anyway
            except OSError as e:
                stats["errors"] += 1
                log("  copy error: %s: %s" % (s, e))
    return stats


def snapshot_sqlite(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".backup-tmp")
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        if os.path.lexists(p):
            os.unlink(p)
    try:
        source = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(str(src)), uri=True, timeout=120)
        try:
            target = sqlite3.connect(str(tmp))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except sqlite3.OperationalError:
        # An app that has closed its WAL database leaves no -shm file, and SQLite cannot open that read-only.
        # With no -wal file either, the main file holds everything, so a clone of it is a complete copy.
        if os.path.exists(str(src) + "-wal"):
            raise
        if os.path.lexists(tmp):
            os.unlink(tmp)
        clone(src, tmp)
    target = sqlite3.connect(str(tmp))
    try:
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("snapshot of %s failed its integrity check" % src)
        target.execute("PRAGMA journal_mode=DELETE")  # a standalone file, readable without -wal/-shm
    finally:
        target.close()
    os.replace(tmp, dst)


def backup_sessions():
    stats = new_stats()
    trees = list(TREES)
    for pattern in EXTRA_PROJECT_GLOBS:
        for p in sorted(glob.glob(pattern)):
            rel = Path(p).relative_to(HOME)
            trees.append((Path(p), "claude/other-config-folders/" + str(rel).replace("/.claude/projects", "/projects")))
    for src, rel in trees:
        if src.is_dir():
            before = dict(stats)
            mirror_tree(src, ROOT / rel, ROOT, stats)
            log("  %-45s +%d new, %d updated, %d old versions kept" % (
                rel, stats["copied"] - before["copied"], stats["updated"] - before["updated"],
                stats["preserved"] - before["preserved"]))
    for src, rel in FILES:
        if src.is_file():
            try:
                mirror_file(src, ROOT / rel, ROOT, stats)
            except OSError as e:
                stats["errors"] += 1
                log("  copy error: %s: %s" % (src, e))
    dbs = list(SQLITE_FILES)
    for pattern in CODEX_DB_GLOBS:
        dbs += [(Path(p), "codex/db/" + Path(p).name) for p in sorted(glob.glob(str(HOME / ".codex" / pattern)))]
    for src, rel in dbs:
        if not src.is_file():
            continue
        try:
            snapshot_sqlite(src, ROOT / rel)
            if rel.startswith("codex/db/"):  # keep one copy per month as history
                monthly = ROOT / "codex/db-monthly" / TODAY[:7] / src.name
                if not monthly.exists():
                    clone(ROOT / rel, monthly)
        except (OSError, sqlite3.Error) as e:
            stats["errors"] += 1
            log("  database snapshot error: %s: %s" % (src, e))
    return stats


# ---------- artifacts ----------
#
# claude-artifacts/<id>/<YYYY-MM-DD>/ holds one snapshot per day the artifact changed:
#   files/   the artifact's own published files (the type's viewer code under artifact-type/ is
#            skipped: it is identical for every artifact of that type and holds none of your content)
#   assets/  uploaded images and other assets
#   docs/    Docs artifacts only: each tab exported as Markdown and HTML (their text is not in files/)
#   meta.json  title, URL, type, version and a checksum for every saved file
# Files identical to the previous snapshot are hard links, so they take no extra space.

LIST_RE = re.compile(r"^- \((?P<owner>[^)]+)\) (?P<title>.+?) — (?P<url>https://claude\.ai/(?:code/)?artifact/"
                     r"(?P<id>[A-Za-z0-9_-]+)) — updated (?P<updated>\d{4}-\d{2}-\d{2})\s*$", re.M)
FILE_LINE_RE = re.compile(r'^- "(?P<path>.+?)"\s+\S+\s+\d+ bytes\s*$', re.M)
TYPE_RE = re.compile(r'an Artifact of type "([^"]+)"')
ASSET_LINE_RE = re.compile(r"^- /_blob/(?P<id>[0-9a-f]{32})\s", re.M)
SAVED_DIR_RE = re.compile(r'Files saved under "(?P<dir>[^"]+)" from version (?P<version>\S+) of')
SAVED_FILE_RE = re.compile(r'^- "(?P<path>.+?)" saved \((?P<size>\d+) bytes, "[^"]*", sha256 (?P<sha>[0-9a-f]{64})\)', re.M)
CONTENT_RE = re.compile(r"<artifact-file-content>.*?</artifact-file-content>", re.S)
ASSET_SAVED_RE = re.compile(r'Asset saved: "(?P<path>[^"]+)" \((?P<size>\d+) bytes, [^,]+, sha256 (?P<sha>[0-9a-f]{64})\)')
DOC_EXT = {"markdown": "md", "html": "html"}


def parse_artifact_list(text):
    return [m.groupdict() for m in LIST_RE.finditer(text)]


def parse_files_listing(text):
    m = re.search(r"\(version ([^)\s]+)\)", text)
    return (m.group(1) if m else None), [m.group("path") for m in FILE_LINE_RE.finditer(text)]


def parse_type(text):
    m = TYPE_RE.search(text)
    return m.group(1) if m else None


def parse_assets_listing(text):
    return [m.group("id") for m in ASSET_LINE_RE.finditer(text)]


def parse_read_result(text):
    text = CONTENT_RE.sub("", text)  # file contents are artifact data and can contain anything
    m = SAVED_DIR_RE.search(text)
    files = [(f.group("path"), int(f.group("size")), f.group("sha")) for f in SAVED_FILE_RE.finditer(text)]
    return {"dir": m.group("dir") if m else None, "version": m.group("version") if m else None, "files": files}


def parse_asset_saved(text):
    m = ASSET_SAVED_RE.search(text)
    return (m.group("path"), int(m.group("size")), m.group("sha")) if m else None


def parse_json_result(text):
    """Claude Docs connector results are JSON, sometimes after a notice line."""
    i = text.find("{")
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[i:])[0]
    except ValueError:
        return None


def parse_doc_tabs(text):
    obj = parse_json_result(text) or {}
    return [(f["id"], f.get("name") or f["id"]) for f in obj.get("files", [])
            if isinstance(f, dict) and f.get("id") and f.get("mime") == "application/vnd.claude.page"]


def parse_doc_export(text):
    obj = parse_json_result(text) or {}
    data = obj.get("data") or {}
    if obj.get("verdict") != "allow" or not data.get("bytes_b64"):
        return None
    return {"format": data.get("format"), "rev": obj.get("rev"), "bytes": base64.b64decode(data["bytes_b64"])}


PERSISTED_RE = re.compile(r"^\s*(?:<|&lt;)persisted-output(?:>|&gt;)\s*Output too large \([^)]*\)\. "
                          r"Full output saved to: (\S+)")
RUN_DIR_MARK = "ai-session-backup-"  # temp folder name of our headless runs, also in their ~/.claude/projects folder


def resolve_persisted(text):
    """Claude Code replaces a large tool result with a stub naming the file it saved; read that file."""
    m = PERSISTED_RE.match(text)
    if not m:
        return text
    path = Path(m.group(1))
    parents = path.parents
    if len(parents) < 4 or parents[3] != HOME / ".claude" / "projects" or RUN_DIR_MARK not in parents[2].name \
            or not path.is_file():
        return text  # only files our own headless runs wrote
    raw = path.read_text()
    try:
        blocks = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(blocks, list):
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    return raw


def cleanup_headless_leftovers():
    """Remove the ~/.claude/projects folders our headless runs leave (saved large results)."""
    for p in (HOME / ".claude" / "projects").glob("*%s*" % RUN_DIR_MARK):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def run_claude(claude, prompt, max_turns, workdir, allow=()):
    """One headless Claude Code run; returns the raw tool results and the tool names it had.

    Read-only by construction: only the Artifact tool (plus any connector tool named in `allow`) is
    approved, Artifact's writing actions are denied, and anything else is refused (dontAsk)."""
    cmd = [claude, "-p", prompt, "--model", "haiku", "--restricted", "--tools", "Artifact",
           "--allowedTools", "Artifact"] + list(allow) + [
           "--disallowedTools", "Artifact(action:publish)", "Artifact(action:delete)", "Artifact(action:pin)",
           "Artifact(action:unpin)", "ArtifactData", "ArtifactComments",
           "--permission-mode", "dontAsk", "--no-session-persistence",
           "--output-format", "stream-json", "--verbose", "--max-turns", str(max_turns)]
    # Same environment whether launchd or a Claude session starts us: drop inherited Claude session variables.
    # Claude Code offers the Artifact tool only when it runs as the desktop app's engine, and the
    # enableArtifact setting cannot turn it on, so the run identifies as that engine.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    env.update(PATH=PATH, MAX_MCP_OUTPUT_TOKENS="2000000", CLAUDE_CODE_ENTRYPOINT="claude-desktop")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL, cwd=workdir, env=env)
    results, tools, final = [], [], None
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "system" and d.get("subtype") == "init":
            tools = d.get("tools") or []
        elif d.get("type") == "user":
            for c in d.get("message", {}).get("content", []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    content = c.get("content")
                    if isinstance(content, list):
                        content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
                    results.append(resolve_persisted(content or ""))
        elif d.get("type") == "result":
            final = d
    if final is None or "Failed to authenticate" in (final.get("result") or "") or not results:
        raise RuntimeError("headless Claude run made no tool call (tools offered: %s). Claude said: %s %s" % (
            [t for t in tools if not t.startswith("mcp__")], ((final or {}).get("result") or "")[:300], r.stderr[-300:]))
    return results, tools


def safe_join(base, rel):
    p = os.path.realpath(os.path.join(base, rel))
    if not p.startswith(os.path.realpath(base) + os.sep):
        raise ValueError("path outside the download folder: %r" % rel)
    return p


def store(src, dst, prev_file, sha):
    """Save a downloaded file, hard-linking the previous snapshot's copy when identical."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if prev_file is not None and prev_file.is_file() and sha256(prev_file) == sha:
        os.link(prev_file, dst)
    else:
        shutil.copy2(src, dst)


def link_tree(src, dst):
    for dirpath, _, filenames in os.walk(src):
        for name in filenames:
            s = Path(dirpath) / name
            d = dst / s.relative_to(src)
            d.parent.mkdir(parents=True, exist_ok=True)
            os.link(s, d)


DOCS_TOOLS = ("mcp__claude_ai_Claude_Docs__read", "mcp__claude_ai_Claude_Docs__export")  # claude.ai Docs connector
DOCS_ATTEMPTS = 3
DOCS_RETRY_SECONDS = 30  # the Claude Docs connector sometimes is not connected yet when a run starts


def export_docs_once(claude, work, aid, docs_tools, stage):
    read_tool, export_tool = docs_tools
    res, _ = run_claude(claude, 'Call the tool named %s exactly once with {"ref": {"object": "project", '
                        '"id": "%s"}}. Do not call any other tool. Then reply with the word DONE.'
                        % (read_tool, aid), 4, work, allow=[read_tool])
    tabs = parse_doc_tabs(next((t for t in res if '"files"' in t), ""))
    if not tabs:
        raise RuntimeError("Docs artifact: no tabs found in %r" % [t[:200] for t in res])
    docs_meta, h = [], hashlib.sha256()
    for tab_id, tab_name in tabs:
        calls = ['the tool named %s with {"container": {"kind": "project", "id": "%s"}, "file": "%s", '
                 '"format": "%s", "maxBytes": 11534336}' % (export_tool, aid, tab_id, fmt) for fmt in DOC_EXT]
        res, _ = run_claude(claude, "Make exactly these tool calls, in this order, and nothing else:\n"
                            + "\n".join("%d) %s" % (n + 1, c) for n, c in enumerate(calls))
                            + "\nThen reply with the word DONE.", 5, work, allow=[export_tool])
        exports = {e["format"]: e for e in (parse_doc_export(t) for t in res) if e}
        if "markdown" not in exports:
            raise RuntimeError("Docs tab %s: Markdown export failed: %r" % (tab_id, [t[:200] for t in res]))
        stem = "%s__%s" % (re.sub(r"[^\w.-]+", "_", tab_name).strip("_")[:80] or "tab", tab_id)
        for fmt, e in sorted(exports.items()):
            name = "%s.%s" % (stem, DOC_EXT.get(fmt, fmt))
            dst = stage / "docs" / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(e["bytes"])
            sha = hashlib.sha256(e["bytes"]).hexdigest()
            h.update(("%s %s\n" % (name, sha)).encode())
            docs_meta.append({"name": name, "tab": tab_id, "format": fmt, "rev": e["rev"],
                              "size": len(e["bytes"]), "sha256": sha})
    return docs_meta, h.hexdigest()


def export_docs(claude, work, aid, docs_tools, stage):
    """Export every tab of a Docs artifact, retrying with a fresh run when the connector was not ready."""
    for attempt in range(1, DOCS_ATTEMPTS + 1):
        try:
            return export_docs_once(claude, work, aid, docs_tools, stage)
        except RuntimeError:
            if attempt == DOCS_ATTEMPTS:
                raise
            shutil.rmtree(stage / "docs", ignore_errors=True)
            time.sleep(DOCS_RETRY_SECONDS)


def save_artifact(claude, work, base, it, index, docs_tools):
    """Save a new snapshot of one artifact if it changed. Returns True when a snapshot was written."""
    aid, url = it["id"], it["url"]
    prev = index.get(aid, {})
    prev_dir = base / aid / prev["dir"] if prev.get("dir") else None
    res, _ = run_claude(claude, 'Make exactly these two Artifact tool calls, in this order, and nothing else: '
                        '1) {"action": "list", "scope": "files", "url": "%s"} 2) {"action": "list", "scope": "assets", '
                        '"url": "%s"}. Then reply with the word DONE.' % (url, url), 5, work)
    listing = next((t for t in res if "Published files of" in t), "")
    version, paths = parse_files_listing(listing)
    if not version:
        raise RuntimeError("no version in the files listing: %r" % (res[:1],))
    atype = parse_type(listing)
    paths = [p for p in paths if not p.startswith("artifact-type/")]
    assets = parse_assets_listing(next((t for t in res if "Assets of https" in t), ""))

    stage = base / aid / (".staging-%d" % os.getpid())
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    try:
        meta = {"title": it["title"], "url": url, "owner": it["owner"], "type": atype, "version": version,
                "listed_updated": it["updated"], "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "files": [], "assets": [], "docs": []}
        if version == prev.get("version") and prev_dir and prev_dir.is_dir():
            for part in ("files", "assets"):
                if (prev_dir / part).is_dir():
                    link_tree(prev_dir / part, stage / part)
            old = json.loads((prev_dir / "meta.json").read_text())
            meta["files"], meta["assets"] = old.get("files", []), old.get("assets", [])
        else:
            calls = ['{"action": "read", "url": "%s", "paths": %s}' % (url, json.dumps(paths[i:i + 200]))
                     for i in range(0, len(paths), 200)]
            calls += ['{"action": "read", "url": "%s", "path": "%s"}' % (url, a) for a in assets]
            res = []
            if calls:
                res, _ = run_claude(claude, "Make exactly these Artifact tool calls, in this order, and nothing else:\n"
                                    + "\n".join("%d) %s" % (n + 1, c) for n, c in enumerate(calls))
                                    + "\nThen reply with the word DONE.", len(calls) + 3, work)
            missing, missing_assets = set(paths), set(assets)
            for text in res:
                saved = parse_read_result(text)
                for rel, size, sha in saved["files"]:
                    if rel not in missing or not saved["dir"]:
                        continue  # only files we asked for
                    src = safe_join(saved["dir"], rel)
                    if sha256(src) != sha:
                        raise RuntimeError("checksum mismatch for %s" % rel)
                    store(src, stage / "files" / rel, prev_dir / "files" / rel if prev_dir else None, sha)
                    meta["files"].append({"path": rel, "size": size, "sha256": sha})
                    missing.discard(rel)
                asset = parse_asset_saved(text)
                if asset:
                    path, size, sha = asset
                    name = os.path.basename(path)
                    aid32 = name.split(".")[0]
                    if aid32 in missing_assets and "/artifact-files/" in path and sha256(path) == sha:
                        store(path, stage / "assets" / name, prev_dir / "assets" / name if prev_dir else None, sha)
                        meta["assets"].append({"name": name, "size": size, "sha256": sha})
                        missing_assets.discard(aid32)
            if missing or missing_assets:
                raise RuntimeError("not downloaded: %d of %d files, %d of %d assets (e.g. %s)" % (
                    len(missing), len(paths), len(missing_assets), len(assets), sorted(missing | missing_assets)[:3]))

        docs_sha = None
        if atype == "Docs":
            # Never save a Docs snapshot without its text: if the export keeps failing this raises,
            # the artifact counts as an error, and the next run tries again.
            meta["docs"], docs_sha = export_docs(claude, work, aid, docs_tools or DOCS_TOOLS, stage)

        if version == prev.get("version") and docs_sha == prev.get("docs_sha") and prev_dir and prev_dir.is_dir():
            prev["checked"] = CHECK_DATE
            return False
        (stage / "meta.json").write_text(json.dumps(meta, indent=1))
        name, n = TODAY, 1
        while (base / aid / name).exists():  # a second change today gets <date>.2, never replacing the first
            n += 1
            name = "%s.%d" % (TODAY, n)
        os.replace(stage, base / aid / name)
        index[aid] = {"title": it["title"], "url": url, "type": atype, "version": version, "docs_sha": docs_sha,
                      "dir": name, "checked": CHECK_DATE}
        return True
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def needs_check(prev, listed_updated):
    """Whether an artifact may have changed since we last checked it.

    The artifact list dates changes by day in UTC, so anything changed within a day of the last check
    is checked again. Docs text changes without a new artifact date, so Docs are checked every run."""
    if prev.get("type") == "Docs" or not prev.get("checked"):
        return True
    margin = datetime.date.fromisoformat(prev["checked"]) - datetime.timedelta(days=1)
    return listed_updated >= margin.isoformat()


def backup_artifacts():
    stats = {"listed": 0, "saved": 0, "unchanged": 0, "errors": 0}
    claude = shutil.which("claude", path=PATH)
    if not claude:
        log("  artifacts skipped: claude command not found")
        return stats, "skipped: no claude command"
    status = subprocess.run([claude, "auth", "status"], capture_output=True, text=True, timeout=60,
                            env=dict(os.environ, PATH=PATH))
    try:
        logged_in = json.loads(status.stdout).get("loggedIn") is True
    except ValueError:
        logged_in = False
    if not logged_in:
        log("  artifacts skipped: terminal Claude Code is not logged in. Run `claude auth login` once.")
        notify("Artifacts were not backed up: run `claude auth login` in Terminal once.")
        return stats, "skipped: not logged in"

    base = ROOT / "claude-artifacts"
    base.mkdir(parents=True, exist_ok=True)
    index_path = base / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    try:
        return run_artifact_backup(claude, base, index, index_path, stats)
    finally:
        cleanup_headless_leftovers()


def run_artifact_backup(claude, base, index, index_path, stats):
    with tempfile.TemporaryDirectory(prefix=RUN_DIR_MARK) as work:
        results, tools = run_claude(claude, 'Call the Artifact tool exactly once with {"action": "list", "scope": '
                                    '"all", "limit": 50}. Do not call any other tool. Then reply with the word DONE.', 4, work)
        read_tool = next((t for t in tools if re.match(r"^mcp__.*Docs.*__read$", t)), None)
        export_tool = next((t for t in tools if re.match(r"^mcp__.*Docs.*__export$", t)), None)
        docs_tools = (read_tool, export_tool) if read_tool and export_tool else None
        items = parse_artifact_list("\n".join(results))
        stats["listed"] = len(items)
        if not items:
            raise RuntimeError("artifact list came back empty: %r" % ("\n".join(results)[:300]))
        for it in items:
            prev = index.get(it["id"], {})
            if not needs_check(prev, it["updated"]):
                stats["unchanged"] += 1
                continue
            try:
                if save_artifact(claude, work, base, it, index, docs_tools):
                    stats["saved"] += 1
                    log("  artifact saved: %s" % it["title"])
                else:
                    stats["unchanged"] += 1
            except Exception as e:  # one broken artifact must not stop the others
                stats["errors"] += 1
                log("  artifact error: %s: %s" % (it["title"], e))
            index_path.write_text(json.dumps(index, indent=1, sort_keys=True))
    return stats, "ok"


# ---------- iCloud ----------

COPY_TIMES = "logs/icloud-copy-times.json"  # when each file was last copied to iCloud Drive
UPLOAD_GRACE_DAYS = 3  # a file iCloud has not uploaded after this long is reported
UPLOAD_CHECK_JS = r"""
ObjC.import('Foundation');
function run(argv) {
  var list = ObjC.unwrap($.NSString.stringWithContentsOfFileEncodingError(argv[0], $.NSUTF8StringEncoding, null));
  var pending = [];
  list.split('\n').forEach(function (p) {
    if (!p) return;
    var val = Ref(), err = Ref();
    var ok = $.NSURL.fileURLWithPath(p).getResourceValueForKeyError(val, $.NSURLUbiquitousItemIsUploadedKey, err);
    if (!(ok && val[0] && ObjC.unwrap(val[0]) === true)) pending.push(p);
  });
  return pending.join('\n');
}
"""


def not_uploaded(paths):
    """Of the given iCloud Drive files, those iCloud reports as not uploaded."""
    if not paths:
        return []
    fd, listfile = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(paths))
    try:
        r = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", UPLOAD_CHECK_JS, listfile],
                           capture_output=True, text=True, timeout=3600)
    finally:
        os.unlink(listfile)
    if r.returncode != 0:
        raise RuntimeError("iCloud upload check failed: %s" % r.stderr.strip()[-300:])
    return [line for line in r.stdout.splitlines() if line]


def mirror_to_icloud():
    """Copy new and changed backup files into iCloud Drive, then report files iCloud has not uploaded.

    Nothing there is ever deleted or rewritten from scratch: the local backup only adds files or grows
    them, and keeps old versions in _replaced/."""
    stats = {"copied": 0, "updated": 0, "unchanged": 0, "errors": 0}
    if not ICLOUD_ROOT.is_dir():
        raise RuntimeError("iCloud Drive folder not found (%s): is iCloud Drive turned on?" % ICLOUD_ROOT)
    STAGING.mkdir(parents=True, exist_ok=True)
    times_path = ROOT / COPY_TIMES
    times = json.loads(times_path.read_text()) if times_path.exists() else {}
    now = int(time.time())
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name == ".lock" or transient(name):
                continue
            src = Path(dirpath) / name
            rel = str(src.relative_to(ROOT))
            if rel == COPY_TIMES:
                continue  # rewritten every run; it records copies, it is not backup content
            dst = ICLOUD / rel
            try:
                if src.is_symlink() or not src.is_file():
                    continue
                st = src.stat()
                try:
                    dt = dst.stat()  # metadata only: works without downloading files iCloud has offloaded
                except FileNotFoundError:
                    dt = None
                if dt is not None and dt.st_size == st.st_size and dt.st_mtime_ns == st.st_mtime_ns:
                    stats["unchanged"] += 1
                    times.setdefault(rel, now)  # copied before tracking began: its grace starts now
                    continue
                clone(src, dst, tmp_dir=STAGING)
                times[rel] = now
                stats["copied" if dt is None else "updated"] += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                stats["errors"] += 1
                if stats["errors"] <= 20:
                    log("  iCloud copy error: %s: %s" % (dst, e))
    times_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = times_path.with_name(times_path.name + ".backup-tmp")
    tmp.write_text(json.dumps(times, sort_keys=True))
    os.replace(tmp, times_path)

    cutoff = now - UPLOAD_GRACE_DAYS * 86400
    stale = not_uploaded([str(ICLOUD / r) for r, t in sorted(times.items()) if t < cutoff and (ICLOUD / r).exists()])
    stats["not_uploaded_after_3_days"] = len(stale)
    for p in stale[:5]:
        log("  not in iCloud yet after %d days: %s" % (UPLOAD_GRACE_DAYS, p))
    return stats


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=["sessions", "artifacts", "icloud"])
    args = ap.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    lock = open(ROOT / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another backup is still running; exiting")
        return 0
    cleanup_headless_leftovers()  # in case an earlier run was interrupted
    started = time.time()
    state = {"started": datetime.datetime.now().isoformat(timespec="seconds")}
    log("backup started")
    if shutil.disk_usage(ROOT).free < MIN_FREE_BYTES:
        log("stopped: less than 10 GB free on the disk")
        notify("Backup stopped: less than 10 GB free on the disk.")
        return 1
    ok = True
    if args.only in (None, "sessions"):
        try:
            s = backup_sessions()
        except Exception as e:  # never die silently: record it and notify
            log("sessions failed: %r" % e)
            s = dict(new_stats(), errors=1, failure=repr(e))
        state["sessions"] = s
        log("sessions: %(copied)d new, %(updated)d updated, %(unchanged)d unchanged, "
            "%(preserved)d old versions kept, %(errors)d errors" % s)
        ok = ok and s["errors"] == 0
    if args.only in (None, "artifacts"):
        try:
            a, status = backup_artifacts()
        except Exception as e:
            a, status = {"errors": 1}, "failed: %s" % e
        state["artifacts"] = dict(a, status=status)
        log("artifacts: %s %s" % (status, json.dumps(a)))
        ok = ok and a.get("errors", 0) == 0 and not status.startswith("failed")
    if args.only in (None, "icloud"):
        try:
            i = mirror_to_icloud()
        except Exception as e:
            log("iCloud copy failed: %s" % e)
            i = {"errors": 1, "failure": str(e)}
        state["icloud"] = i
        log("iCloud: %s" % json.dumps(i))
        ok = ok and i.get("errors", 0) == 0 and i.get("not_uploaded_after_3_days", 0) == 0
    state["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    state["seconds"] = round(time.time() - started)
    state["ok"] = ok
    (ROOT / "state.json").write_text(json.dumps(state, indent=1))
    log("backup finished in %ds (%s)" % (state["seconds"], "ok" if ok else "with errors"))
    if not ok:
        notify("Backup finished with errors; see ~/Backups/ai-sessions/logs.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
