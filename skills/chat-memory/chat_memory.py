#!/usr/bin/env python3
"""chat-memory: search your past Claude Code conversations locally.

Reads the transcripts Claude Code already keeps in ~/.claude/projects and
builds a full-text index of the conversation text only (your messages and
Claude's replies). Tool output, file contents, thinking and internal notices
are left out, and common secrets are masked before anything is stored.

No network, no hooks, no dependencies beyond the Python standard library.
The index updates itself on each command; it never changes Claude Code's files.

Usage:
  chat_memory.py search WORDS... [--project P] [--since YYYY-MM-DD] [--limit N] [--any]
  chat_memory.py show SESSION [--match WORD] [--context N] [--max-chars N]
  chat_memory.py sessions [--project P] [--limit N]
  chat_memory.py forget SESSION     # remove a session from the index for good
  chat_memory.py stats
  chat_memory.py reindex            # rebuild from scratch

Environment:
  CLAUDE_CONFIG_DIR       Claude Code's config folder (default ~/.claude)
  CHAT_MEMORY_DATA_DIR    where the index is kept (default: see _default_data_dir)
"""

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys

__version__ = "0.2.0"


def _default_data_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/chat-memory")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
        return os.path.join(base, "chat-memory")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "chat-memory")


PROJECTS_DIR = os.path.join(
    os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"), "projects")
DATA_DIR = os.path.expanduser(os.environ.get("CHAT_MEMORY_DATA_DIR") or _default_data_dir())
DB_PATH = os.path.join(DATA_DIR, "index.db")
SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# Secret masking
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    # A key without its END line (cut off or pasted in part) is masked to the end.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.*?-----END [A-Z ]*PRIVATE KEY-----|.*)", re.S),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{20,}"),
    # "token <hex>" (Gitea/Forgejo) and "Basic <base64>"; needs a digit so long words stay
    re.compile(r"(?i)\b(?:token|basic)\s+(?=[A-Za-z._~+/=\-]*\d)[A-Za-z0-9._~+/=\-]{20,}"),
]
# key=value / key: value  -> keep the key, mask the value
_KEY_VALUE = re.compile(
    r"(?i)\b((?:[A-Z0-9_]*_)?(?:password|passwd|passwort|kennwort|pwd|passphrase|secret|token|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|client[_-]?secret|app[_-]?password)[A-Z0-9_]*)"
    r"(\s*[:=]\s*)(\"[^\"\n]+\"|'[^'\n]+'|[^\s,;]+)"
)
# https://user:password@host
_URL_CREDS = re.compile(r"(\b[a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@", re.I)

MASK = "[REDACTED]"


def mask_secrets(text):
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(MASK, text)
    text = _KEY_VALUE.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    text = _URL_CREDS.sub(lambda m: m.group(1) + MASK + "@", text)
    return text


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
# User-side entries that Claude Code writes itself, not things you typed.
_SKIP_PREFIXES = (
    "<task-notification", "<command-name", "<command-message", "<command-args",
    "<local-command", "<bash-input", "<bash-stdout", "<bash-stderr",
    "Caveat: The messages below", "[Request interrupted",
)


def _clean(text):
    text = _SYSTEM_REMINDER.sub("", text).strip()
    if not text or text.startswith(_SKIP_PREFIXES):
        return ""
    return mask_secrets(text)


def _message_text(entry):
    """Plain conversation text of one transcript entry, or ''."""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return _clean(content)
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return _clean("\n".join(p for p in parts if p))
    return ""


def parse_transcript(path):
    """Returns (session_info, [(uuid, timestamp, role, text), ...])."""
    info = {"session_id": None, "cwd": None, "title": None, "first_prompt": None}
    messages = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            if kind == "ai-title" and entry.get("aiTitle"):
                info["title"] = entry["aiTitle"]
                continue
            if kind not in ("user", "assistant"):
                continue
            if entry.get("isSidechain") or entry.get("isMeta") or entry.get("isCompactSummary"):
                continue
            info["session_id"] = info["session_id"] or entry.get("sessionId")
            info["cwd"] = info["cwd"] or entry.get("cwd")
            text = _message_text(entry)
            if not text:
                continue
            if kind == "user" and not info["first_prompt"]:
                info["first_prompt"] = " ".join(text.split())[:80]
            messages.append((entry.get("uuid") or "", entry.get("timestamp") or "", kind, text))
    if not info["session_id"]:
        info["session_id"] = os.path.splitext(os.path.basename(path))[0]
    return info, messages


def transcript_files():
    """Main session transcripts only (no subagent files, no memory folders)."""
    if not os.path.isdir(PROJECTS_DIR):
        return []
    found = []
    for project in os.listdir(PROJECTS_DIR):
        folder = os.path.join(PROJECTS_DIR, project)
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if name.endswith(".jsonl") and os.path.isfile(path):
                found.append(path)
    return found


def project_name(cwd, path):
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or cwd
    return os.path.basename(os.path.dirname(path)).lstrip("-")


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def _check_fts5():
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:
        sys.exit("This Python's SQLite has no FTS5 full-text search. "
                 "Use a Python from python.org, Homebrew or your Linux distribution.")


def connect():
    _check_fts5()
    os.umask(0o077)  # index readable by you only
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.executescript("""
            DROP TABLE IF EXISTS files;
            DROP TABLE IF EXISTS sessions;
            DROP TABLE IF EXISTS seen;
            DROP TABLE IF EXISTS copies;
            DROP TABLE IF EXISTS messages;
            CREATE TABLE files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER);
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, path TEXT, project TEXT, cwd TEXT,
                title TEXT, started TEXT, ended TEXT, count INTEGER);
            -- seen: the file a message is counted in; copies: other files that repeat it
            CREATE TABLE seen (uuid TEXT PRIMARY KEY, path TEXT);
            CREATE INDEX seen_path ON seen (path);
            CREATE TABLE copies (uuid TEXT, path TEXT, PRIMARY KEY (uuid, path));
            CREATE INDEX copies_path ON copies (path);
            CREATE TABLE IF NOT EXISTS forgotten (session_id TEXT PRIMARY KEY);
            CREATE VIRTUAL TABLE messages USING fts5(
                text, session_id UNINDEXED, ts UNINDEXED, role UNINDEXED,
                path UNINDEXED, tokenize = 'unicode61 remove_diacritics 2');
        """)
        conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        conn.commit()
    os.chmod(DB_PATH, 0o600)
    return conn


def _drop_file(conn, path):
    conn.execute("DELETE FROM messages WHERE path = ?", (path,))
    conn.execute("DELETE FROM seen WHERE path = ?", (path,))
    conn.execute("DELETE FROM copies WHERE path = ?", (path,))
    conn.execute("DELETE FROM sessions WHERE path = ?", (path,))
    conn.execute("DELETE FROM files WHERE path = ?", (path,))


def update_index(conn):
    """Re-reads transcripts that are new or changed since the last run."""
    forgotten = {r[0] for r in conn.execute("SELECT session_id FROM forgotten")}
    known = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, mtime, size FROM files")}
    current = {}
    for path in transcript_files():
        try:
            st = os.stat(path)
        except OSError:
            continue
        current[path] = (st.st_mtime, st.st_size)

    for path in set(known) - set(current):
        _drop_file(conn, path)

    changed = {p for p in current if known.get(p) != current[p]}
    # When a file is gone (Claude Code cleans up old transcripts), its messages
    # may still live on as copies in a newer file: re-read those files.
    changed.update(r[0] for r in conn.execute(
        "SELECT DISTINCT path FROM copies WHERE uuid NOT IN (SELECT uuid FROM seen)"))

    # Oldest first: a rewound conversation copies earlier messages into a new
    # file, and each message should be counted once, in the file it began in.
    for path in sorted((p for p in changed if p in current), key=lambda p: current[p][0]):
        _drop_file(conn, path)
        try:
            info, messages = parse_transcript(path)
        except OSError:
            continue
        mtime, size = current[path]
        conn.execute("INSERT INTO files VALUES (?, ?, ?)", (path, mtime, size))
        sid = info["session_id"]
        kept = []
        for uuid, ts, role, text in messages:
            if uuid:
                if conn.execute("SELECT 1 FROM seen WHERE uuid = ?", (uuid,)).fetchone():
                    conn.execute("INSERT OR IGNORE INTO copies VALUES (?, ?)", (uuid, path))
                    continue
                conn.execute("INSERT INTO seen VALUES (?, ?)", (uuid, path))
            kept.append((text, sid, ts, role, path))
        # A forgotten session still claims its messages, so copies of them in
        # other files stay out of the index too.
        if not kept or sid in forgotten:
            continue
        conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", kept)
        stamps = [k[2] for k in kept if k[2]]
        conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, path, project_name(info["cwd"], path), info["cwd"],
             info["title"] or info["first_prompt"] or "(untitled)",
             min(stamps) if stamps else "", max(stamps) if stamps else "", len(kept)),
        )
    conn.commit()


def resolve_session(conn, prefix):
    rows = conn.execute(
        "SELECT session_id FROM sessions WHERE session_id LIKE ? ORDER BY ended DESC",
        (prefix + "%",)).fetchall()
    if not rows:
        sys.exit("No session matches '%s'." % prefix)
    if len(rows) > 1 and rows[0][0] != prefix:
        sys.exit("'%s' matches %d sessions; use more characters." % (prefix, len(rows)))
    return rows[0][0]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _local(ts, length=16):
    """Transcript timestamps are UTC ('...Z'); show them in local time."""
    try:
        when = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return when.astimezone().isoformat(" ")[:length]
    except (ValueError, AttributeError):
        return (ts or "?")[:length]


def _date(value):
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD, e.g. 2026-01-31")
    return value


def _fts_query(words, any_word):
    tokens = re.findall(r"\w+", " ".join(words), re.UNICODE)
    if not tokens:
        sys.exit("Nothing to search for.")
    quoted = ['"%s"' % t for t in tokens]
    return (" OR " if any_word else " ").join(quoted)


def cmd_search(conn, args):
    sql = """
        SELECT m.session_id, m.ts, m.role,
               snippet(messages, 0, '»', '«', ' … ', 20) AS snip,
               s.project, s.title
        FROM messages m JOIN sessions s ON s.session_id = m.session_id
        WHERE messages MATCH ?"""
    params = []
    if args.project:
        sql += " AND s.project LIKE ?"
        params.append("%" + args.project + "%")
    if args.since:
        sql += " AND m.ts >= ?"
        params.append(args.since)
    sql += " ORDER BY bm25(messages) LIMIT ?"
    params.append(args.limit)

    rows = conn.execute(sql, [_fts_query(args.words, args.any)] + params).fetchall()
    if not rows and not args.any and len(args.words) > 0:
        rows = conn.execute(sql, [_fts_query(args.words, True)] + params).fetchall()
        if rows:
            print("(No message contains all words; showing messages with any of them.)\n")
    if not rows:
        print("No matches.")
        return
    for sid, ts, role, snip, project, title in rows:
        print("[%s] %s · %s · %s" % (_local(ts, 10), project, title, sid[:8]))
        print("  %s: %s\n" % ("you" if role == "user" else "claude", " ".join(snip.split())))


def cmd_show(conn, args):
    sid = resolve_session(conn, args.session)
    s = conn.execute("SELECT project, title, started, ended, cwd FROM sessions WHERE session_id = ?",
                     (sid,)).fetchone()
    print("Session %s · %s · %s" % (sid, s[0], s[1]))
    print("%s → %s · %s\n" % (_local(s[2]), _local(s[3]), s[4] or ""))
    rows = conn.execute("SELECT ts, role, text FROM messages WHERE session_id = ? ORDER BY ts, rowid",
                        (sid,)).fetchall()
    keep = range(len(rows))
    if args.match:
        needle = args.match.lower()
        hits = [i for i, r in enumerate(rows) if needle in r[2].lower()]
        if not hits:
            print("No message in this session contains '%s'." % args.match)
            return
        keep = sorted({j for i in hits for j in range(i - args.context, i + args.context + 1)
                       if 0 <= j < len(rows)})
    last = None
    for i in keep:
        if last is not None and i != last + 1:
            print("   …\n")
        ts, role, text = rows[i]
        if len(text) > args.max_chars:
            text = text[:args.max_chars] + " … [%d more characters]" % (len(text) - args.max_chars)
        print("── %s · %s" % ("you" if role == "user" else "claude", _local(ts)[11:16]))
        print(text + "\n")
        last = i


def cmd_sessions(conn, args):
    sql = "SELECT session_id, project, title, ended, count FROM sessions"
    params = []
    if args.project:
        sql += " WHERE project LIKE ?"
        params.append("%" + args.project + "%")
    sql += " ORDER BY ended DESC LIMIT ?"
    params.append(args.limit)
    for sid, project, title, ended, count in conn.execute(sql, params):
        print("%s  %s  %-20s %s (%d msgs)" % (_local(ended, 10), sid[:8], project[:20], title, count))


def cmd_forget(conn, args):
    sid = resolve_session(conn, args.session)
    conn.execute("INSERT OR IGNORE INTO forgotten VALUES (?)", (sid,))
    conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
    conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
    conn.commit()
    conn.execute("VACUUM")
    print("Removed session %s from the index. Claude Code's own transcript is untouched." % sid)


def cmd_stats(conn, args):
    sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    forgotten = conn.execute("SELECT COUNT(*) FROM forgotten").fetchone()[0]
    span = conn.execute("SELECT MIN(started), MAX(ended) FROM sessions").fetchone()
    print("Index:     %s (%.1f MB)" % (DB_PATH, os.path.getsize(DB_PATH) / 1e6))
    print("Sessions:  %d   Messages: %d   Forgotten: %d" % (sessions, messages, forgotten))
    if span[0]:
        print("Covers:    %s to %s" % (span[0][:10], span[1][:10]))


def main():
    parser = argparse.ArgumentParser(description="Search your past Claude Code conversations.")
    parser.add_argument("--version", action="version", version="chat-memory " + __version__)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("search", help="full-text search")
    p.add_argument("words", nargs="+")
    p.add_argument("--project")
    p.add_argument("--since", type=_date, help="YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--any", action="store_true", help="match any word instead of all")

    p = sub.add_parser("show", help="print one session")
    p.add_argument("session", help="session id or its first characters")
    p.add_argument("--match", help="only messages containing this text, with context")
    p.add_argument("--context", type=int, default=2)
    p.add_argument("--max-chars", type=int, default=2000)

    p = sub.add_parser("sessions", help="list recent sessions")
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("forget", help="drop a session from the index for good")
    p.add_argument("session")

    sub.add_parser("stats", help="index size and coverage")
    sub.add_parser("reindex", help="rebuild the index from scratch")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    conn = connect()
    if args.command == "reindex":
        conn.execute("DELETE FROM files")
        conn.commit()
    update_index(conn)
    if args.command == "reindex":
        cmd_stats(conn, args)
        return
    {"search": cmd_search, "show": cmd_show, "sessions": cmd_sessions,
     "forget": cmd_forget, "stats": cmd_stats}[args.command](conn, args)


if __name__ == "__main__":
    main()
