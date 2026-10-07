#!/usr/bin/env python3
"""cchat - index, browse and search your Claude Code chat history.

    python3 cchat.py index      # build/refresh the index (incremental)
    python3 cchat.py serve      # open the browser UI

Reads ~/.claude/projects/**/*.jsonl. Never writes to it.
Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import shutil
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECTS_DIR = Path(os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects"))
# Codex keeps its own history here; read-only, like the Claude transcripts
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
DB_PATH = Path(os.environ.get("CCHAT_DB", HERE / "index.db"))
# deleted transcripts are moved here, never unlinked - recovery beats regret
TRASH_DIR = Path(os.environ.get("CCHAT_TRASH", Path.home() / ".claude" / "cchat-trash"))
# handoffs written for another agent (Codex) to read - ours, not Claude Code's
HANDOFF_DIR = Path(os.environ.get("CCHAT_HANDOFFS", Path.home() / ".claude" / "cchat-handoffs"))

# Lines bigger than this are tool payloads / file attachments. We count them but
# never parse them - a few of them are hundreds of MB.
HUGE_LINE = 1_000_000
BODY_CAP = 8_000          # chars of message text kept for search/preview
TOOL_OUT_CAP = 4_000      # chars of tool output sent to the transcript view
IDLE_GAP = 30 * 60        # a pause longer than this doesn't count as time worked

SCHEMA_VERSION = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, size INTEGER, mtime REAL, uid TEXT, kind TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  uid TEXT PRIMARY KEY,          -- path-derived: a resumed session can exist twice
                                 -- (same id) under a project and its git worktree
  id TEXT,
  project TEXT, cwd TEXT, path TEXT,
  title TEXT, ai_title TEXT, custom_title TEXT,
  first_prompt TEXT, last_prompt TEXT,
  started TEXT, ended TEXT,
  n_user INTEGER, n_asst INTEGER, n_tool INTEGER, n_side INTEGER, n_lines INTEGER,
  models TEXT, branches TEXT, entrypoints TEXT, tools TEXT, agents TEXT,
  in_tok INTEGER, out_tok INTEGER, cache_tok INTEGER,
  bytes INTEGER, sub_bytes INTEGER, n_sub INTEGER, mtime REAL, version TEXT,
  open_tasks TEXT,               -- json: unfinished items from the last task reminder
  n_files INTEGER,               -- distinct files this chat touched
  last_ctx INTEGER,              -- prompt size at the end == what a resume replays
  n_errors INTEGER, n_interrupts INTEGER,
  active_secs INTEGER,           -- time actually worked, idle gaps excluded
  turn_secs INTEGER,             -- what the transcript itself timed each turn at
  n_compact INTEGER, ctx_lost INTEGER,   -- auto-compactions, and context they threw away
  n_away INTEGER, n_api_err INTEGER,
  source TEXT                    -- 'claude' or 'codex': which agent wrote this chat
);
CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS sessions_ended ON sessions(ended);
CREATE INDEX IF NOT EXISTS sessions_id ON sessions(id);
CREATE TABLE IF NOT EXISTS subagents (
  path TEXT PRIMARY KEY, uid TEXT, name TEXT, bytes INTEGER, n_lines INTEGER
);
CREATE INDEX IF NOT EXISTS subagents_session ON subagents(uid);
CREATE TABLE IF NOT EXISTS days (
  uid TEXT, project TEXT, day TEXT, n INTEGER,
  PRIMARY KEY (uid, day)
);
CREATE INDEX IF NOT EXISTS days_day ON days(day);
CREATE TABLE IF NOT EXISTS msgs (
  id INTEGER PRIMARY KEY, uid TEXT, seq INTEGER, line INTEGER,
  role TEXT, ts TEXT, body TEXT
);
CREATE INDEX IF NOT EXISTS msgs_session ON msgs(uid);
CREATE TABLE IF NOT EXISTS usage (
  uid TEXT, project TEXT, model TEXT, msgs INTEGER,
  in_tok INTEGER, out_tok INTEGER,
  cache_5m INTEGER, cache_1h INTEGER, cache_read INTEGER,
  PRIMARY KEY (uid, model)
);
CREATE INDEX IF NOT EXISTS usage_model ON usage(model);
CREATE INDEX IF NOT EXISTS usage_project ON usage(project);
-- one row per human prompt: what the model did in response to it
CREATE TABLE IF NOT EXISTS method (
  uid TEXT, turn INTEGER, project TEXT, model TEXT,
  tools TEXT,                    -- json array of tool names, in order
  n_tools INTEGER, batch INTEGER, thought INTEGER, words INTEGER,
  deleg INTEGER, edited INTEGER, verified INTEGER, reads_before_edit INTEGER,
  errors INTEGER, interrupted INTEGER,
  secs REAL,                     -- measured, from the transcript's own turn_duration
  line INTEGER,                  -- transcript line the turn starts on, for jump-to
  PRIMARY KEY (uid, turn)
);
CREATE INDEX IF NOT EXISTS method_project ON method(project);
CREATE INDEX IF NOT EXISTS method_model ON method(model);
-- which chat touched which file, so "who edited X" is one query
CREATE TABLE IF NOT EXISTS pins (
  uid TEXT PRIMARY KEY, pinned_at TEXT
);
-- also user data: chats you have put out of sight but not thrown away
CREATE TABLE IF NOT EXISTS deprecated (
  uid TEXT PRIMARY KEY, at TEXT
);
-- and titles you set yourself, which override whatever the transcript says
CREATE TABLE IF NOT EXISTS titles (
  uid TEXT PRIMARY KEY, title TEXT, at TEXT
);
-- messages you marked inside a chat, to walk back to without searching for them.
-- The label is taken at pin time, so a pin still reads as something before that
-- part of the transcript is on screen.
CREATE TABLE IF NOT EXISTS msgpins (
  uid TEXT, line INTEGER, at TEXT, who TEXT, label TEXT,
  PRIMARY KEY (uid, line)
);
CREATE INDEX IF NOT EXISTS msgpins_uid ON msgpins(uid);
CREATE TABLE IF NOT EXISTS filerefs (
  uid TEXT, project TEXT, path TEXT, reads INTEGER, writes INTEGER, last_ts TEXT,
  PRIMARY KEY (uid, path)
);
CREATE INDEX IF NOT EXISTS filerefs_path ON filerefs(path);
CREATE INDEX IF NOT EXISTS filerefs_project ON filerefs(project);
-- things that happened to a chat rather than in it: the recaps Claude writes
-- while you are away, the points where its memory was compacted, api failures
CREATE TABLE IF NOT EXISTS moments (
  uid TEXT, line INTEGER, ts TEXT, kind TEXT, project TEXT,
  text TEXT, a INTEGER, b INTEGER, secs REAL,
  PRIMARY KEY (uid, line)
);
CREATE INDEX IF NOT EXISTS moments_kind ON moments(kind, ts);
CREATE INDEX IF NOT EXISTS moments_project ON moments(project);
CREATE VIRTUAL TABLE IF NOT EXISTS msgs_fts
  USING fts5(body, content='msgs', content_rowid='id', tokenize='porter unicode61');
"""

# List prices in USD per million tokens (Anthropic published rates, 2026-06-24).
# Cache writes bill at 1.25x input for the 5-minute TTL and 2x for the 1-hour TTL;
# cache reads bill at 0.1x input. Edit this table if your rates differ.
PRICES = {
    "claude-fable-5":    (10.0, 50.0),
    "claude-mythos-5":   (10.0, 50.0),
    "claude-opus-5":     (5.0, 25.0),
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-opus-4-7":   (5.0, 25.0),
    "claude-opus-4-6":   (5.0, 25.0),
    "claude-opus-4-5":   (5.0, 25.0),
    "claude-sonnet-5":   (3.0, 15.0),   # $2/$10 intro rate applies through 2026-08-31
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
}
CACHE_WRITE_5M, CACHE_WRITE_1H, CACHE_READ_RATE = 1.25, 2.0, 0.1


def model_cost(model: str, u: dict) -> dict | None:
    """What these tokens would list for on the API, split by cost component.
    None when the model has no price on file."""
    # transcripts sometimes carry the dated full ID (claude-haiku-4-5-20251001)
    p = PRICES.get(model) or PRICES.get(re.sub(r"-\d{8}$", "", model or ""))
    if not p:
        return None
    inp, outp = p
    parts = {
        "fresh": (u["in_tok"] or 0) * inp / 1e6,
        "write": ((u["cache_5m"] or 0) * CACHE_WRITE_5M
                  + (u["cache_1h"] or 0) * CACHE_WRITE_1H) * inp / 1e6,
        "read": (u["cache_read"] or 0) * inp * CACHE_READ_RATE / 1e6,
        "output": (u["out_tok"] or 0) * outp / 1e6,
    }
    parts["total"] = sum(parts.values())
    return parts


SYS_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
CMD_TAG = re.compile(r"<(command-name|command-message|command-args|local-command-[a-z]+)>(.*?)</\1>", re.S)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\[[0-9]{1,2}m")
CAVEAT = re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.S)
CMD_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
CMD_OUT = re.compile(r"<local-command-(?:stdout|stderr)>(.*?)</local-command-(?:stdout|stderr)>", re.S)


# --------------------------------------------------------------------------- db

def connect(path: Path = DB_PATH, migrate: bool = False) -> sqlite3.Connection:
    """Readers never rewrite the schema. Only the indexer may (migrate=True).

    A long-running `serve` from an older build shares this file with a newer one; if
    every connection were allowed to drop-and-recreate on a version mismatch, the two
    would take turns wiping each other's index. Readers fail loudly instead.
    """
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    have = db.execute("PRAGMA user_version").fetchone()[0]
    if have and have != SCHEMA_VERSION:
        if have > SCHEMA_VERSION:
            # a newer build wrote this file; migrating "down" would silently destroy
            # its data, so refuse regardless of who is asking
            raise RuntimeError(
                f"index.db is schema v{have}, newer than this build (v{SCHEMA_VERSION}). "
                f"You are running an old copy of cchat.py - use the newer one."
            )
        if not migrate:
            raise RuntimeError(
                f"index.db is schema v{have}, this build expects v{SCHEMA_VERSION}. "
                f"Run: python3 {Path(__file__).name} index --full"
            )
        # note: pins/deprecated/titles/msgpins are absent on purpose - your marks,
        # not anything an index run could rebuild
        for t in ("files", "sessions", "subagents", "days", "usage", "method",
                  "filerefs", "moments", "msgs_fts", "msgs"):
            db.execute(f"DROP TABLE IF EXISTS {t}")
    db.executescript(SCHEMA)
    if migrate or not have:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return db


def uid_of(path: Path | str) -> str:
    return hashlib.sha1(str(path).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------- parsing

def clean(text: str) -> str:
    text = SYS_REMINDER.sub("", text)
    text = CMD_TAG.sub(lambda m: m.group(2) if m.group(1) == "command-name" else "", text)
    return text.strip()


def blocks_text(content) -> tuple[str, list[str], int, bool]:
    """-> (visible text, tool names used, n tool_result blocks, had thinking)"""
    if isinstance(content, str):
        return content, [], 0, False
    if not isinstance(content, list):
        return "", [], 0, False
    parts, tools, n_res, thinking = [], [], 0, False
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text") or "")
        elif t == "thinking":
            thinking = True
        elif t == "tool_use":
            tools.append(b.get("name") or "?")
        elif t == "tool_result":
            n_res += 1
    return "\n".join(parts), tools, n_res, thinking


def iter_lines(path: Path, start: int = 0):
    """Yield (lineno, parsed_dict_or_None, nbytes). Huge lines are not parsed.

    Lines before `start` are counted but not parsed. Tailing a live chat asks
    for the end of the file every couple of seconds, and parsing everything
    before it is the expensive half of that work.
    """
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for i, raw in enumerate(fh):
            if i < start:
                continue
            n = len(raw)
            if n > HUGE_LINE:
                yield i, None, n
                continue
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield i, json.loads(raw), n
            except (ValueError, RecursionError):
                yield i, None, n


def scan_usage(path: Path) -> tuple[dict[str, dict], int]:
    """Per-model token usage in a transcript, plus its line count.
    Used for subagent logs, whose spend would otherwise go uncounted."""
    per_model: dict[str, dict] = {}
    n = 0
    for lineno, d, _b in iter_lines(path):
        n = lineno + 1
        if d is None or d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        u, model = msg.get("usage"), msg.get("model")
        if not u or not model:
            continue
        cc = u.get("cache_creation") or {}
        h1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
        m5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
        if not (h1 or m5):
            m5 = int(u.get("cache_creation_input_tokens") or 0)
        row = per_model.setdefault(model, {
            "msgs": 0, "in_tok": 0, "out_tok": 0,
            "cache_5m": 0, "cache_1h": 0, "cache_read": 0,
        })
        row["msgs"] += 1
        row["in_tok"] += int(u.get("input_tokens") or 0)
        row["out_tok"] += int(u.get("output_tokens") or 0)
        row["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        row["cache_5m"] += m5
        row["cache_1h"] += h1
    return per_model, n


def scan_session(path: Path, project: str) -> tuple[dict, list[tuple]]:
    """Read one transcript. -> (session row dict, list of message tuples)"""
    s = {
        "uid": uid_of(path), "id": path.stem, "project": project, "path": str(path),
        "source": "claude", "cwd": None, "ai_title": None, "custom_title": None,
        "first_prompt": None, "last_prompt": None,
        "started": None, "ended": None,
        "n_user": 0, "n_asst": 0, "n_tool": 0, "n_side": 0, "n_lines": 0,
        "in_tok": 0, "out_tok": 0, "cache_tok": 0, "version": None, "last_ctx": 0,
        "n_errors": 0, "n_interrupts": 0,
        "turn_secs": 0, "n_compact": 0, "ctx_lost": 0, "n_away": 0, "n_api_err": 0,
    }
    models, branches, entries, agents = set(), set(), set(), []
    tools: Counter[str] = Counter()
    msgs: list[tuple] = []
    per_model: dict[str, dict] = {}
    files_touched: dict[str, dict] = {}
    open_tasks: list[dict] = []
    moments: list[dict] = []
    turns: list[dict] = []
    turn: dict | None = None
    seq = 0

    def close_turn():
        """A turn is one human prompt plus everything the model did about it."""
        if not turn or not turn["model"] or not (turn["tools"] or turn["words"]):
            return
        tl = turn["tools"]
        edited = next((i for i, x in enumerate(tl) if x in EDIT_KIND), None)
        turns.append({
            "model": turn["model"], "tools": tl, "batch": max(turn["batch"] or [0]),
            "thought": int(turn["thought"]), "words": turn["words"],
            "deleg": int(any(x in DELEGATE_KIND for x in tl)),
            "edited": int(edited is not None),
            "verified": int(edited is not None and turn["verified_after"] >= edited),
            "rbe": sum(1 for x in tl[:edited] if x in READ_KIND) if edited is not None else None,
            "errors": turn["errors"], "interrupted": int(turn["interrupted"]),
            "secs": round(turn["secs"], 1) or None,
            "line": turn["line"],
        })

    for lineno, d, _n in iter_lines(path):
        s["n_lines"] = lineno + 1
        if d is None:
            s["n_tool"] += 1          # huge line == tool payload
            continue
        t = d.get("type")

        if t == "ai-title":
            s["ai_title"] = d.get("aiTitle") or s["ai_title"]
            continue
        if t == "custom-title":
            s["custom_title"] = d.get("customTitle") or s["custom_title"]
            continue
        if t == "agent-name":
            name = d.get("agentName")
            if name and name not in agents:
                agents.append(name)
            continue
        if t == "last-prompt":
            s["last_prompt"] = d.get("lastPrompt") or s["last_prompt"]
            continue

        ts = d.get("timestamp")
        if ts:
            if s["started"] is None or ts < s["started"]:
                s["started"] = ts
            if s["ended"] is None or ts > s["ended"]:
                s["ended"] = ts
        if d.get("cwd") and not s["cwd"]:
            s["cwd"] = d["cwd"]
        if d.get("gitBranch"):
            branches.add(d["gitBranch"])
        if d.get("entrypoint"):
            entries.add(d["entrypoint"])
        if d.get("version"):
            s["version"] = d["version"]

        if t == "attachment":
            att = d.get("attachment") or {}
            if att.get("type") == "task_reminder":
                items = att.get("content")
                open_tasks = [
                    {"subject": i.get("subject"), "status": i.get("status")}
                    for i in (items or []) if isinstance(i, dict)
                    and i.get("status") not in ("completed", "cancelled")
                ] if isinstance(items, list) else []
            continue

        if t == "system":
            sub = d.get("subtype")
            if sub == "turn_duration":
                secs = (d.get("durationMs") or 0) / 1000
                s["turn_secs"] += secs
                if turn is not None:
                    turn["secs"] += secs
            elif sub == "away_summary":
                # Claude's own recap of what it did while you were not looking
                txt = re.sub(r"\s*\(disable recaps in /config\)\s*$", "",
                             (d.get("content") or "").strip())
                if txt:
                    s["n_away"] += 1
                    moments.append({"line": lineno, "ts": ts, "kind": "away",
                                    "text": txt[:1200], "a": None, "b": None, "secs": None})
            elif sub == "compact_boundary":
                cm = d.get("compactMetadata") or {}
                pre, post = int(cm.get("preTokens") or 0), int(cm.get("postTokens") or 0)
                s["n_compact"] += 1
                s["ctx_lost"] += max(0, pre - post)
                moments.append({"line": lineno, "ts": ts, "kind": "compact",
                                "text": cm.get("trigger") or "auto", "a": pre, "b": post,
                                "secs": round((cm.get("durationMs") or 0) / 1000, 1)})
            elif sub == "api_error":
                err = d.get("error") or {}
                code = err.get("status")
                s["n_api_err"] += 1
                moments.append({"line": lineno, "ts": ts, "kind": "error",
                                "text": f"api error {code or '?'}"
                                        + (f" · retry {d.get('retryAttempt')}"
                                           f"/{d.get('maxRetries')}" if d.get("retryAttempt") else ""),
                                "a": code if isinstance(code, int) else None,
                                "b": None, "secs": None})
            continue

        if t not in ("user", "assistant"):
            continue

        side = bool(d.get("isSidechain"))
        msg = d.get("message") or {}
        text, used, n_res, thinking = blocks_text(msg.get("content"))
        if t == "assistant":
            model = msg.get("model")
            if model:
                models.add(model)
            u = msg.get("usage") or {}
            s["in_tok"] += int(u.get("input_tokens") or 0)
            s["out_tok"] += int(u.get("output_tokens") or 0)
            s["cache_tok"] += int(u.get("cache_read_input_tokens") or 0)
            if u and model:
                # cache writes bill differently per TTL, so keep the 1h/5m split
                cc = u.get("cache_creation") or {}
                created = int(u.get("cache_creation_input_tokens") or 0)
                h1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
                m5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
                if not (h1 or m5):
                    m5 = created                      # older records: no split recorded
                row = per_model.setdefault(model, {
                    "msgs": 0, "in_tok": 0, "out_tok": 0,
                    "cache_5m": 0, "cache_1h": 0, "cache_read": 0,
                })
                row["msgs"] += 1
                row["in_tok"] += int(u.get("input_tokens") or 0)
                row["out_tok"] += int(u.get("output_tokens") or 0)
                row["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
                row["cache_5m"] += m5
                row["cache_1h"] += h1
            for name in used:
                tools[name] += 1
            # what a resume would replay: the last prompt this session sent
            s["last_ctx"] = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
            for b in (msg.get("content") or []):
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                nm, inp = b.get("name") or "?", b.get("input") or {}
                writes = nm in EDIT_KIND
                for fp in tool_paths(nm, inp):
                    ref = files_touched.setdefault(fp, {"reads": 0, "writes": 0, "last": None})
                    ref["writes" if writes else "reads"] += 1
                    ref["last"] = ts or ref["last"]
                body_t = clean(tool_search_text(nm, inp))[:BODY_CAP]
                if body_t:
                    msgs.append((s["uid"], seq, lineno, "tool", ts, body_t))
                    seq += 1
            if turn is not None and not side:
                if model and not turn["model"]:
                    turn["model"] = model
                if thinking and not turn["tools"]:
                    turn["thought"] = True
                if used:
                    turn["batch"].append(len(used))
                    for b in (msg.get("content") or []):
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            inp = b.get("input") or {}
                            cmd = inp.get("command") if isinstance(inp, dict) else None
                            if b.get("name") == "Bash" and cmd and VERIFY_RE.search(cmd):
                                turn["verified_after"] = len(turn["tools"])
                    turn["tools"] += used
                turn["words"] += len(text.split())
        if side:
            s["n_side"] += 1
        s["n_tool"] += n_res + len(used)
        failed = sum(1 for b in (msg.get("content") or [])
                     if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"))
        if failed:
            s["n_errors"] += failed
            if turn is not None:
                turn["errors"] += failed

        body = clean(text)
        if not body:
            continue
        if t == "user":
            if side:
                continue                       # subagent's own prompt, not the human
            s["n_user"] += 1
            if "Request interrupted" in body:
                s["n_interrupts"] += 1
                if turn is not None:
                    turn["interrupted"] = True
            else:
                close_turn()
                turn = {"model": None, "tools": [], "batch": [], "thought": False,
                        "words": 0, "verified_after": -1, "errors": 0, "interrupted": False,
                        "secs": 0.0, "line": lineno}
            # a bare "/model" or "/clear" makes a useless preview - hold out for real text
            if s["first_prompt"] is None and not re.fullmatch(r"/[\w:-]+(\s+\S{0,20})?", body):
                s["first_prompt"] = body[:600]
        else:
            s["n_asst"] += 1
        msgs.append((s["uid"], seq, lineno, t + ("+side" if side else ""), ts, body[:BODY_CAP]))
        seq += 1

    s["title"] = s["custom_title"] or s["ai_title"] or (agents[0] if agents else None)
    s["models"] = json.dumps(sorted(models))
    s["branches"] = json.dumps(sorted(branches))
    s["entrypoints"] = json.dumps(sorted(entries))
    s["tools"] = json.dumps(tools.most_common(30))
    s["agents"] = json.dumps(agents[:20])
    st = path.stat()
    s["bytes"] = st.st_size
    s["mtime"] = st.st_mtime
    close_turn()
    stamps = sorted(m[4] for m in msgs if m[4])
    active = 0
    for a, b in zip(stamps, stamps[1:]):
        try:
            gap = (datetime.fromisoformat(b.replace("Z", "+00:00"))
                   - datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            continue
        if 0 < gap <= IDLE_GAP:            # longer than this and you walked away
            active += gap
    s["active_secs"] = int(active)
    s["n_files"] = len(files_touched)
    s["open_tasks"] = json.dumps(open_tasks) if open_tasks else None
    s["_files"] = files_touched
    s["_usage"] = per_model
    s["_method"] = turns
    s["_moments"] = moments
    s["turn_secs"] = int(s["turn_secs"])
    return s, msgs


# ------------------------------------------------------------------------ codex
# Codex keeps its chats in three layers. The rollout JSONL files are the source of
# truth but hard to read: a shell command is a JavaScript string for a generic
# `exec` tool. Codex decodes them itself into typed items in thread_history_N.sqlite
# and lists its chats in state_N.sqlite. We read those two - never write them - and
# stat the rollout files to know when a chat moved. Token counts are only in the
# rollouts, so those are read too, one cheap pass that parses two record types.

CODEX_NEED = {                     # the columns this reader relies on, per table
    "state": {"threads": {"id", "rollout_path", "cwd", "created_at", "updated_at",
                          "source", "model", "git_branch", "archived", "name"}},
    "thread_history": {
        "thread_items": {"thread_id", "turn_id", "rollout_ordinal", "created_at_ms",
                         "item_json", "item_type"},
        "thread_turns": {"thread_id", "turn_id", "rollout_ordinal", "status",
                         "duration_ms", "error_json"},
    },
}
CODEX_PROBLEM: str | None = None   # set when Codex's storage no longer looks as expected
_codex_local = threading.local()


class CodexFormat(RuntimeError):
    pass


def codex_sqlite(prefix: str) -> Path | None:
    """state_5.sqlite today. The number moves when Codex changes its schema, so take
    the highest one there is and let the column check decide whether we can read it."""
    best = None
    for p in CODEX_HOME.glob(f"{prefix}_*.sqlite"):
        m = re.fullmatch(rf"{re.escape(prefix)}_(\d+)\.sqlite", p.name)
        if m and (best is None or int(m.group(1)) > best[0]):
            best = (int(m.group(1)), p)
    return best[1] if best else None


def codex_db(prefix: str) -> sqlite3.Connection | None:
    """A read-only connection, one per thread. mode=ro and not immutable=1: Codex
    writes these live through a WAL, and immutable would skip the newest turns."""
    path = codex_sqlite(prefix)
    if path is None:
        return None
    cache = getattr(_codex_local, "dbs", None)
    if cache is None:
        cache = _codex_local.dbs = {}
    if str(path) in cache:
        return cache[str(path)]
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10, check_same_thread=False)
    db.row_factory = sqlite3.Row
    for table, cols in CODEX_NEED[prefix].items():
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        missing = cols - have
        if missing:
            db.close()
            raise CodexFormat(f"Codex changed {path.name}: {table} has no "
                              f"{', '.join(sorted(missing))}. Codex chats are not shown "
                              f"until cchat.py learns the new layout.")
    cache[str(path)] = db
    return db


def codex_threads() -> dict[str, dict]:
    """rollout path -> thread row, for every top-level Codex chat. Subagent threads
    carry a JSON source and belong to their parent, so they are left out."""
    global CODEX_PROBLEM
    if not CODEX_HOME.is_dir():
        return {}
    try:
        db = codex_db("state")
        if db is None:
            return {}
        rows = db.execute("SELECT * FROM threads WHERE source NOT LIKE '{%'").fetchall()
        codex_db("thread_history")                  # check it too, before anyone relies on it
    except (CodexFormat, sqlite3.Error) as exc:
        if str(exc) != CODEX_PROBLEM:
            print(f"codex: {exc}", file=sys.stderr, flush=True)
        CODEX_PROBLEM = str(exc)
        return {}
    CODEX_PROBLEM = None
    return {r["rollout_path"]: dict(r) for r in rows}


def codex_uid(thread_id: str) -> str:
    """Keyed by thread id, not by path: Codex moves a rollout when you archive the
    chat, and your pins and renames should not fall off when it does."""
    return uid_of("codex:" + thread_id)


def codex_project(cwd: str) -> str:
    """The key Claude Code would give this directory, so both tools share a project."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd or "")


def ms_iso(ms) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


SHELL_WRAP = re.compile(r"""^/bin/(?:ba|z)?sh -lc (["'])(.*)\1$""", re.S)


def codex_command(cmd) -> str:
    """`/bin/zsh -lc "rg -n foo"` -> `rg -n foo`. The wrapper is on every call."""
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    m = SHELL_WRAP.match(cmd or "")
    if not m:
        return cmd or ""
    return m.group(2).replace('\\"', '"') if m.group(1) == '"' else m.group(2)


def _capped(text: str) -> tuple[str, int]:
    text = ANSI.sub("", text or "")
    return text[:TOOL_OUT_CAP], max(0, len(text) - TOOL_OUT_CAP)


def codex_event(r, model: str | None) -> dict | None:
    """One Codex item as the same event the transcript view draws for Claude.
    A tool call arrives with its result already attached."""
    try:
        it = json.loads(r["item_json"])
    except (TypeError, ValueError):
        return None
    t = it.get("type")
    base = {"side": False, "ts": ms_iso(r["created_at_ms"]), "line": r["rollout_ordinal"],
            "uuid": it.get("id"), "is_cmd": False}

    def said(role, text, kind="text"):
        text = (text or "").strip()
        return {**base, "role": role, "model": model if role == "assistant" else None,
                "parts": [{"kind": kind, "text": text}]} if text else None

    if t == "userMessage":
        bits = [c.get("text") or "" if c.get("type") == "text" else "[image]"
                for c in it.get("content") or [] if isinstance(c, dict)]
        return said("user", "\n".join(b for b in bits if b))
    if t == "agentMessage":
        return said("assistant", it.get("text"))
    if t == "plan":
        return said("assistant", "**Plan**\n\n" + (it.get("text") or ""))
    if t == "reasoning":
        bits = [b if isinstance(b, str) else (b or {}).get("text") or ""
                for b in (it.get("summary") or []) + (it.get("content") or [])]
        return said("assistant", "\n\n".join(b for b in bits if b), "thinking")
    if t == "contextCompaction":
        return {**base, "role": "system", "model": None, "parts": [
            {"kind": "moment", "what": "compact", "a": None, "b": None, "secs": 0,
             "text": "compaction"}]}

    if t == "commandExecution":
        cmd = codex_command(it.get("command"))
        out, over = _capped(it.get("aggregatedOutput") or "")
        code = it.get("exitCode")
        err = it.get("status") == "failed" or (code is not None and code != 0)
        if err and not out:
            out = f"exit code {code}" if code is not None else "failed"
        part = {"tool": "bash", "name": "shell", "label": "shell", "brief": " ".join(cmd.split())[:220],
                "input": cmd[:TOOL_OUT_CAP], "result": {"text": out, "truncated": over, "error": err}}
    elif t == "fileChange":
        ch = [c for c in it.get("changes") or [] if isinstance(c, dict)]
        body = "\n\n".join(f"{(c.get('kind') or {}).get('type', 'edit')} {c.get('path')}\n{c.get('diff') or ''}"
                           for c in ch)
        inp, _ = _capped(body)
        part = {"tool": "edit", "name": "apply_patch", "label": "apply_patch",
                "brief": ", ".join(str(c.get("path")) for c in ch)[:220], "input": inp,
                "result": {"text": it.get("status") or "", "truncated": 0,
                           "error": it.get("status") == "failed"}}
    elif t == "mcpToolCall":
        args = it.get("arguments") or {}
        res = it.get("result") or {}
        txt = "\n".join(c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict))
        err = it.get("error")
        out, over = _capped(txt or (json.dumps(err) if err else ""))
        part = {"tool": "mcp", "name": f"mcp__{it.get('server')}__{it.get('tool')}",
                "label": f"{it.get('server')} · {it.get('tool')}", "brief": tool_brief(args, "mcp"),
                "input": json.dumps(args, indent=2)[:TOOL_OUT_CAP],
                "result": {"text": out, "truncated": over, "error": bool(err)}}
    elif t == "webSearch":
        hits = "\n".join(f"{h.get('url') or ''} — {h.get('snippet') or ''}"
                         for h in it.get("results") or [] if isinstance(h, dict))
        out, over = _capped(hits)
        part = {"tool": "search", "name": "web_search", "label": "web search",
                "brief": (it.get("query") or "")[:220],
                "input": json.dumps(it.get("action") or {"query": it.get("query")}, indent=2),
                "result": {"text": out, "truncated": over, "error": False}}
    elif t == "imageView":
        part = {"tool": "read", "name": "view_image", "label": "view image",
                "brief": it.get("path") or "", "input": it.get("path") or "", "result": None}
    else:
        return None                                  # sleep, review-mode markers, subagent pings
    part.update({"kind": "tool", "id": it.get("id"), "agent_type": None})
    return {**base, "role": "assistant", "model": model, "parts": [part]}


def codex_usage(path: Path, model: str | None) -> tuple[dict[str, dict], int]:
    """Per-model tokens from the rollout, plus the size of the last prompt.

    Each token_count event carries the usage of one response in last_token_usage;
    the same event is sometimes written twice, so a repeat is skipped. Only two
    record types are parsed - the rest of the line is never decoded."""
    per_model: dict[str, dict] = {}
    prev, last_ctx = None, 0
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return per_model, 0
    with fh:
        for raw in fh:
            head = raw[:200]
            if '"turn_context"' in head:
                try:
                    model = (json.loads(raw).get("payload") or {}).get("model") or model
                except ValueError:
                    pass
            elif '"token_count"' in head:
                try:
                    info = (json.loads(raw).get("payload") or {}).get("info") or {}
                except ValueError:
                    continue
                u = info.get("last_token_usage")
                key = json.dumps([info.get("total_token_usage"), u], sort_keys=True)
                if not u or key == prev or not model:
                    continue
                prev = key
                cached = int(u.get("cached_input_tokens") or 0)
                wrote = int(u.get("cache_write_input_tokens") or 0)
                row = per_model.setdefault(model, {"msgs": 0, "in_tok": 0, "out_tok": 0,
                                                   "cache_5m": 0, "cache_1h": 0, "cache_read": 0})
                row["msgs"] += 1
                # OpenAI counts cached tokens inside input_tokens; ours are separate
                row["in_tok"] += max(0, int(u.get("input_tokens") or 0) - cached - wrote)
                row["cache_read"] += cached
                row["cache_5m"] += wrote
                row["out_tok"] += int(u.get("output_tokens") or 0)
                last_ctx = int(u.get("input_tokens") or 0)
    return per_model, last_ctx


def scan_codex(th: dict, project: str) -> tuple[dict, list[tuple]]:
    """One Codex chat -> the same (session row, message tuples) scan_session makes."""
    hist = codex_db("thread_history")
    path = Path(th["rollout_path"])
    uid = codex_uid(th["id"])
    per_model, last_ctx = codex_usage(path, th.get("model"))
    model = th.get("model") or next(iter(per_model), None)
    s = {
        "uid": uid, "id": th["id"], "project": project, "path": str(path), "source": "codex",
        "cwd": th["cwd"], "ai_title": th.get("name") or None, "custom_title": None,
        "first_prompt": None, "last_prompt": None, "started": None, "ended": None,
        "n_user": 0, "n_asst": 0, "n_tool": 0, "n_side": 0, "n_lines": 0,
        "in_tok": sum(u["in_tok"] for u in per_model.values()),
        "out_tok": sum(u["out_tok"] for u in per_model.values()),
        "cache_tok": sum(u["cache_read"] for u in per_model.values()),
        "version": th.get("cli_version"), "last_ctx": last_ctx,
        "n_errors": 0, "n_interrupts": 0, "turn_secs": 0, "n_compact": 0, "ctx_lost": 0,
        "n_away": 0, "n_api_err": 0, "open_tasks": None,
    }
    tools: Counter[str] = Counter()
    msgs: list[tuple] = []
    files_touched: dict[str, dict] = {}
    moments: list[dict] = []
    turns: dict[str, dict] = {}
    for tr in hist.execute("SELECT * FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal",
                           (th["id"],)):
        turns[tr["turn_id"]] = {"line": tr["rollout_ordinal"], "kinds": [], "tools": [],
                                "thought": False, "words": 0, "errors": 0, "verified_after": -1,
                                "interrupted": tr["status"] == "interrupted",
                                "secs": (tr["duration_ms"] or 0) / 1000, "human": False}
        s["turn_secs"] += (tr["duration_ms"] or 0) / 1000
        if tr["status"] == "interrupted":
            s["n_interrupts"] += 1
        if tr["error_json"]:
            try:
                msg = (json.loads(tr["error_json"]) or {}).get("message") or "error"
            except ValueError:
                msg = "error"
            s["n_api_err"] += 1
            moments.append({"line": tr["rollout_ordinal"], "ts": ms_iso((tr["started_at"] or 0) * 1000),
                            "kind": "error", "text": msg[:300], "a": None, "b": None, "secs": None})

    seq = 0

    def touch(fp, how, ts):
        ref = files_touched.setdefault(fp, {"reads": 0, "writes": 0, "last": None})
        ref[how] += 1
        ref["last"] = ts or ref["last"]

    for r in hist.execute("SELECT * FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal",
                          (th["id"],)):
        s["n_lines"] = r["rollout_ordinal"] + 1
        ev = codex_event(r, model)
        if not ev:
            continue
        ts, line, p = ev["ts"], ev["line"], ev["parts"][0]
        if ts:
            s["started"] = min(s["started"] or ts, ts)
            s["ended"] = max(s["ended"] or ts, ts)
        turn = turns.setdefault(r["turn_id"], {"line": line, "kinds": [], "tools": [], "thought": False,
                                               "words": 0, "errors": 0, "verified_after": -1,
                                               "interrupted": False, "secs": 0, "human": False})
        if p["kind"] == "moment":
            s["n_compact"] += 1
            moments.append({"line": line, "ts": ts, "kind": "compact", "text": "auto",
                            "a": None, "b": None, "secs": None})
        elif p["kind"] == "thinking":
            if not turn["tools"]:
                turn["thought"] = True
        elif p["kind"] == "text":
            body = clean(p["text"])
            if ev["role"] == "user":
                s["n_user"] += 1
                turn["human"] = True
                s["first_prompt"] = s["first_prompt"] or body[:600]
                s["last_prompt"] = body[:600]
            else:
                s["n_asst"] += 1
                turn["words"] += len(body.split())
            msgs.append((uid, seq, line, ev["role"], ts, body[:BODY_CAP]))
            seq += 1
        elif p["kind"] == "tool":
            s["n_tool"] += 1
            tools[p["label"]] += 1
            it = json.loads(r["item_json"])
            kind = p["tool"]
            if kind == "bash":
                acts = [a for a in it.get("commandActions") or [] if isinstance(a, dict)]
                reads = [a["path"] for a in acts if a.get("type") == "read" and a.get("path")]
                for fp in reads:
                    touch(fp, "reads", ts)
                if acts and all(a.get("type") in ("read", "search", "listFiles") for a in acts):
                    kind = "read"                    # Codex reads files through the shell
                if VERIFY_RE.search(p["input"]):
                    turn["verified_after"] = len(turn["tools"])
            elif kind == "edit":
                for c in it.get("changes") or []:
                    if isinstance(c, dict) and c.get("path"):
                        touch(c["path"], "writes", ts)
            turn["tools"].append(p["label"])
            turn["kinds"].append(kind)
            if p["result"] and p["result"]["error"]:
                s["n_errors"] += 1
                turn["errors"] += 1
            body_t = clean(f"{p['label']} {p['brief']} {p['input'][:1500]}")[:BODY_CAP]
            msgs.append((uid, seq, line, "tool", ts, body_t))
            seq += 1

    method = []
    for t in sorted(turns.values(), key=lambda x: x["line"]):
        if not t["human"] or not (t["tools"] or t["words"]):
            continue
        edited = next((i for i, k in enumerate(t["kinds"]) if k == "edit"), None)
        method.append({
            "model": model, "tools": t["tools"], "batch": 0, "thought": int(t["thought"]),
            "words": t["words"], "deleg": 0, "edited": int(edited is not None),
            "verified": int(edited is not None and t["verified_after"] >= edited),
            "rbe": sum(1 for k in t["kinds"][:edited] if k == "read") if edited is not None else None,
            "errors": t["errors"], "interrupted": int(t["interrupted"]),
            "secs": round(t["secs"], 1) or None, "line": t["line"],
        })

    st = path.stat()
    stamps = sorted(m[4] for m in msgs if m[4])
    active = 0.0
    for a, b in zip(stamps, stamps[1:]):
        gap = (datetime.fromisoformat(b.replace("Z", "+00:00"))
               - datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds()
        if 0 < gap <= IDLE_GAP:
            active += gap
    entries = [f"codex-{th.get('source') or 'cli'}"] + (["archived in Codex"] if th.get("archived") else [])
    s.update({
        "title": s["ai_title"],
        "started": s["started"] or ms_iso((th.get("created_at") or 0) * 1000),
        "ended": s["ended"] or ms_iso((th.get("updated_at") or 0) * 1000),
        "models": json.dumps(sorted(set(per_model) | ({model} if model else set()))),
        "branches": json.dumps([th["git_branch"]] if th.get("git_branch") else []),
        "entrypoints": json.dumps(entries), "tools": json.dumps(tools.most_common(30)),
        "agents": "[]", "bytes": st.st_size, "mtime": st.st_mtime,
        "active_secs": int(active), "n_files": len(files_touched), "turn_secs": int(s["turn_secs"]),
        "_files": files_touched, "_usage": per_model, "_method": method, "_moments": moments,
    })
    return s, msgs


def codex_items(thread_id: str, start: int = 0, limit: int | None = None):
    """Rows for the transcript view, in order, from rollout ordinal `start`."""
    hist = codex_db("thread_history")
    sql = ("SELECT item_json, rollout_ordinal, created_at_ms FROM thread_items "
           "WHERE thread_id=? AND rollout_ordinal>=? ORDER BY rollout_ordinal")
    return hist.execute(sql + (" LIMIT ?" if limit else ""),
                        (thread_id, start, limit) if limit else (thread_id, start))


CODEX_CMD_ARG = re.compile(r'cmd\s*:\s*"((?:[^"\\]|\\.)*)"')


def codex_live_shape(recs: list[dict]) -> dict:
    """live_shape, for the tail of a Codex rollout."""
    pending: dict[str, dict] = {}
    facts = {"text": "", "tool": None, "model": None, "prompt": None}
    for d in recs:
        t, p = d.get("type"), d.get("payload") or {}
        if t == "turn_context":
            facts["model"] = p.get("model") or facts["model"]
        elif t == "response_item":
            pt = p.get("type")
            if pt in ("custom_tool_call", "function_call", "local_shell_call"):
                raw = str(p.get("input") or p.get("arguments") or "")
                m = CODEX_CMD_ARG.search(raw)          # the exec tool wraps the real command in JS
                brief = m.group(1).encode().decode("unicode_escape", "ignore") if m else raw
                tool = {"name": "shell" if m else (p.get("name") or "tool"),
                        "brief": " ".join(brief.split())[:120]}
                facts["tool"] = tool
                pending[p.get("call_id") or p.get("id")] = tool
            elif pt and pt.endswith("_output"):
                pending.pop(p.get("call_id"), None)
        elif t == "event_msg":
            et = p.get("type")
            if et == "item_completed":
                item = p.get("item") or {}
                text = " ".join(c.get("text") or "" for c in item.get("content") or []
                                if isinstance(c, dict))
                text = " ".join(text.split())[:160]
                if item.get("type") == "AgentMessage" and text:
                    facts["text"] = text
                elif item.get("type") == "UserMessage" and text:
                    facts["prompt"] = text
            elif et in ("task_complete", "turn_aborted"):
                pending.clear()
    facts["pending"] = next(iter(pending.values()), None)
    return facts


def codex_transcript(row, meta: dict, qs: dict) -> dict:
    """api_transcript for a Codex chat. `line` is Codex's rollout ordinal, so deep
    links, pins and paging work the same way they do for a Claude transcript."""
    start = int(qs.get("from") or 0)
    limit = min(int(qs.get("limit") or 400), 2000)
    tid, path = row["id"], Path(row["path"])
    model = next((m for m in meta.get("models") or []), None)
    hist = codex_db("thread_history")
    tailed = False
    if qs.get("tail") == "auto" and start == 0:
        try:
            fresh = time.time() - path.stat().st_mtime < IDLE_GAP
        except OSError:
            fresh = False
        if fresh:
            r = hist.execute("SELECT rollout_ordinal FROM thread_items WHERE thread_id=? "
                             "ORDER BY rollout_ordinal DESC LIMIT 1 OFFSET ?",
                             (tid, limit * 2)).fetchone()
            if r:
                start, tailed = r[0], True
    events, last = [], start - 1
    more = False
    for r in codex_items(tid, start):
        if len(events) >= limit:
            more = True
            break
        last = r["rollout_ordinal"]
        ev = codex_event(r, model)
        if ev:
            events.append(ev)
    try:
        fst = path.stat()
        fstat = {"size": fst.st_size, "mtime": fst.st_mtime}
    except OSError:
        fstat = None
    return {"meta": meta, "events": events, "from": start, "read_to": last + 1, "tailed": tailed,
            "file": fstat, "next": last + 1 if more and not tailed else None,
            "n_lines": meta.get("n_lines") or last + 1, "huge_skipped": 0}


# ---------------------------------------------------------------------- indexer

def pretty_project(mangled: str, cwd: str | None) -> str:
    return cwd or "/" + mangled.lstrip("-").replace("-", "/")


def short_name(label: str) -> str:
    """Last path segment, but keep worktrees readable: 'inveralabs ⑂ scoring-v2'."""
    label = label.rstrip("/")
    m = re.search(r"/([^/]+)/\.claude/worktrees/(.+)$", label)
    if m:
        return f"{m.group(1)} ⑂ {m.group(2).split('/')[-1]}"
    return label.split("/")[-1] or label


def transcript_files() -> list[tuple[Path, str, str | None]]:
    """Every transcript on disk as (file, project dir name, parent session id).

    Parent is None for a top-level chat. Subagent logs live at
    <project>/<session-id>/subagents/[workflows/<wf>/]agent-*.jsonl.
    """
    files: list[tuple[Path, str, str | None]] = []
    for pdir in sorted(PROJECTS_DIR.iterdir()):
        if not pdir.is_dir():
            continue
        for f in sorted(pdir.glob("*.jsonl")):
            files.append((f, pdir.name, None))
        for sdir in sorted(d for d in pdir.iterdir() if d.is_dir()):
            for f in sorted(sdir.rglob("*.jsonl")):
                files.append((f, pdir.name, sdir.name))
    return files


def index(full: bool = False, verbose: bool = True, only: str | None = None) -> dict:
    """`only` re-reads a single transcript - what a chat you are watching needs,
    without paying for a walk over every other file."""
    db = connect(migrate=True)
    if full:
        db.executescript(
            "DELETE FROM files; DELETE FROM sessions; DELETE FROM subagents;"
            "DELETE FROM days; DELETE FROM msgs; DELETE FROM msgs_fts;"
            "DELETE FROM usage; DELETE FROM method; DELETE FROM filerefs;"
            "DELETE FROM moments;"
        )
    if db.execute("SELECT COUNT(*) c FROM files").fetchone()["c"] == 0:
        full = True                                    # fresh/reset db: nothing to reuse
    known = {r["path"]: (r["size"], r["mtime"]) for r in db.execute("SELECT path,size,mtime FROM files")}
    seen: set[str] = set()
    stats = {"scanned": 0, "skipped": 0, "sessions": 0, "subagents": 0, "bytes": 0}
    t0 = time.time()

    if not PROJECTS_DIR.is_dir():
        raise SystemExit(f"no such directory: {PROJECTS_DIR}")
    codex = codex_threads()
    files = transcript_files() + [(Path(p), codex_project(t["cwd"]), None) for p, t in codex.items()]
    if only:
        files = [f for f in files if str(f[0]) == only]
        if not files:
            return {"error": "not a transcript we know", "sessions": 0}

    for n, (path, project, parent) in enumerate(files, 1):
        key = str(path)
        seen.add(key)
        try:
            st = path.stat()
        except OSError:
            continue
        if not full and known.get(key) == (st.st_size, st.st_mtime):
            stats["skipped"] += 1
            continue
        stats["scanned"] += 1
        stats["bytes"] += st.st_size
        if verbose:
            mb = st.st_size / 1e6
            print(f"[{n}/{len(files)}] {path.parent.name}/{path.name}  {mb:,.1f} MB", flush=True)

        if parent is not None:
            sub_usage, n_lines = scan_usage(path)
            rel = path.relative_to(PROJECTS_DIR / project / parent).as_posix()
            name = rel[:-6] if rel.endswith(".jsonl") else rel
            name = name.replace("subagents/", "").replace("workflows/", "")
            puid = uid_of(PROJECTS_DIR / project / f"{parent}.jsonl")
            db.execute("DELETE FROM subagents WHERE path=?", (key,))
            db.execute(
                "INSERT INTO subagents(path,uid,name,bytes,n_lines) VALUES(?,?,?,?,?)",
                (key, puid, name, st.st_size, n_lines),
            )
            # keyed by the subagent's own uid so it never collides with the parent's rows
            suid = uid_of(path)
            db.execute("DELETE FROM usage WHERE uid=?", (suid,))
            db.executemany(
                "INSERT INTO usage(uid,project,model,msgs,in_tok,out_tok,cache_5m,cache_1h,cache_read)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                [(suid, project, m, u["msgs"], u["in_tok"], u["out_tok"],
                  u["cache_5m"], u["cache_1h"], u["cache_read"]) for m, u in sub_usage.items()],
            )
            db.execute(
                "INSERT OR REPLACE INTO files(path,size,mtime,uid,kind) VALUES(?,?,?,?,'sub')",
                (key, st.st_size, st.st_mtime, puid),
            )
            stats["subagents"] += 1
            continue

        try:
            s, msgs = scan_codex(codex[key], project) if key in codex else scan_session(path, project)
        except Exception as exc:                      # a corrupt transcript must not stop the run
            print(f"  ! skipped ({exc})", file=sys.stderr)
            continue

        if s["n_user"] == 0 and s["n_asst"] == 0:
            stats["empty"] = stats.get("empty", 0) + 1
            db.execute(
                "INSERT OR REPLACE INTO files(path,size,mtime,uid,kind) VALUES(?,?,?,?,'empty')",
                (key, st.st_size, st.st_mtime, s["uid"]),
            )
            continue                              # stub file: no prompts, no replies

        sid = s["uid"]
        db.execute("DELETE FROM msgs_fts WHERE rowid IN (SELECT id FROM msgs WHERE uid=?)", (sid,))
        db.execute("DELETE FROM msgs WHERE uid=?", (sid,))
        db.execute("DELETE FROM days WHERE uid=?", (sid,))
        db.execute("DELETE FROM usage WHERE uid=?", (sid,))
        db.execute("DELETE FROM method WHERE uid=?", (sid,))
        db.execute("DELETE FROM filerefs WHERE uid=?", (sid,))
        db.execute("DELETE FROM moments WHERE uid=?", (sid,))
        db.execute("DELETE FROM sessions WHERE uid=?", (sid,))

        db.executemany(
            "INSERT INTO usage(uid,project,model,msgs,in_tok,out_tok,cache_5m,cache_1h,cache_read)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            [(sid, project, m, u["msgs"], u["in_tok"], u["out_tok"],
              u["cache_5m"], u["cache_1h"], u["cache_read"])
             for m, u in s.pop("_usage", {}).items()],
        )

        db.executemany(
            "INSERT INTO method(uid,turn,project,model,tools,n_tools,batch,thought,words,"
            "deleg,edited,verified,reads_before_edit,errors,interrupted,secs,line)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(sid, i, project, t["model"], json.dumps(t["tools"]), len(t["tools"]), t["batch"],
              t["thought"], t["words"], t["deleg"], t["edited"], t["verified"], t["rbe"],
              t["errors"], t["interrupted"], t["secs"], t["line"])
             for i, t in enumerate(s.pop("_method", []))],
        )

        db.executemany(
            "INSERT INTO moments(uid,line,ts,kind,project,text,a,b,secs)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            [(sid, m["line"], m["ts"], m["kind"], project, m["text"], m["a"], m["b"], m["secs"])
             for m in s.pop("_moments", [])],
        )

        db.executemany(
            "INSERT INTO filerefs(uid,project,path,reads,writes,last_ts) VALUES(?,?,?,?,?,?)",
            [(sid, project, fp, f["reads"], f["writes"], f["last"])
             for fp, f in s.pop("_files", {}).items()],
        )

        cols = ("uid,id,project,cwd,path,title,ai_title,custom_title,first_prompt,last_prompt,started,ended,"
                "n_user,n_asst,n_tool,n_side,n_lines,models,branches,entrypoints,tools,agents,"
                "in_tok,out_tok,cache_tok,bytes,sub_bytes,n_sub,mtime,version,"
                "open_tasks,n_files,last_ctx,n_errors,n_interrupts,active_secs,"
                "turn_secs,n_compact,ctx_lost,n_away,n_api_err,source")
        vals = [s.get(c) for c in cols.split(",")]
        db.execute(f"INSERT INTO sessions({cols}) VALUES({','.join('?' * len(vals))})", vals)

        per_day: Counter[str] = Counter()
        for m in msgs:
            cur = db.execute(
                "INSERT INTO msgs(uid,seq,line,role,ts,body) VALUES(?,?,?,?,?,?)", m)
            db.execute("INSERT INTO msgs_fts(rowid,body) VALUES(?,?)", (cur.lastrowid, m[5]))
            if m[4] and m[3] != "tool":
                per_day[m[4][:10]] += 1
        db.executemany(
            "INSERT INTO days(uid,project,day,n) VALUES(?,?,?,?)",
            [(sid, project, day, cnt) for day, cnt in per_day.items()],
        )
        db.execute(
            "INSERT OR REPLACE INTO files(path,size,mtime,uid,kind) VALUES(?,?,?,?,'session')",
            (key, st.st_size, st.st_mtime, sid),
        )
        stats["sessions"] += 1
        db.commit()

    # Only drop a chat when its file is genuinely gone from disk. Inferring "deleted"
    # from "absent from this scan" is unsafe: one bad enumeration (a read error, a race
    # with a concurrent indexer) would wipe the whole index.
    if only:
        gone = []                                 # one file says nothing about the rest
    elif known and not files:
        print("index: found no transcripts - refusing to prune", file=sys.stderr)
        gone = []
    else:
        gone = [p for p in known if p not in seen and not Path(p).exists()]
    for p in gone:
        row = db.execute("SELECT uid,kind FROM files WHERE path=?", (p,)).fetchone()
        moved = row and db.execute("SELECT 1 FROM files WHERE uid=? AND path!=? AND kind='session'",
                                   (row["uid"], p)).fetchone()
        if row and row["kind"] == "session" and not moved:
            sid = row["uid"]
            db.execute("DELETE FROM msgs_fts WHERE rowid IN (SELECT id FROM msgs WHERE uid=?)", (sid,))
            db.execute("DELETE FROM msgs WHERE uid=?", (sid,))
            db.execute("DELETE FROM days WHERE uid=?", (sid,))
            db.execute("DELETE FROM usage WHERE uid=?", (sid,))
            db.execute("DELETE FROM method WHERE uid=?", (sid,))
            db.execute("DELETE FROM filerefs WHERE uid=?", (sid,))
            db.execute("DELETE FROM moments WHERE uid=?", (sid,))
            db.execute("DELETE FROM sessions WHERE uid=?", (sid,))
        else:
            db.execute("DELETE FROM subagents WHERE path=?", (p,))
        db.execute("DELETE FROM files WHERE path=?", (p,))

    # roll subagent totals up onto their parent session
    db.execute("""
        UPDATE sessions SET
          n_sub = COALESCE((SELECT COUNT(*) FROM subagents WHERE uid = sessions.uid), 0),
          sub_bytes = COALESCE((SELECT SUM(bytes) FROM subagents WHERE uid = sessions.uid), 0)
    """)
    db.commit()
    if not only:
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('optimize')")
        db.commit()
    # fold the WAL back into the db file so the index survives being copied or
    # having its sidecar files removed between runs
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if not only:
        backup_marks(db)
    stats["removed"] = len(gone)
    stats["seconds"] = round(time.time() - t0, 1)
    if verbose:
        print(f"\nindexed {stats['sessions']} sessions, {stats['subagents']} subagent logs, "
              f"skipped {stats['skipped']} unchanged, {stats['bytes']/1e6:,.0f} MB read "
              f"in {stats['seconds']}s -> {DB_PATH}")
    return stats


# ------------------------------------------------------------------------- api

def fts_query(q: str) -> str:
    """User text -> a safe FTS5 MATCH expression."""
    toks = re.findall(r'"[^"]+"|\S+', q)
    out = []
    for tok in toks:
        if tok.startswith('"') and tok.endswith('"') and len(tok) > 2:
            out.append(tok)
        else:
            t = re.sub(r'[^\w\s]', " ", tok).strip()
            if t:
                out.append('"%s"' % t)
    if not out:
        return ""
    out[-1] = out[-1][:-1] + '"*' if out[-1].endswith('"') else out[-1]
    return " AND ".join(out)


def api_meta(db) -> dict:
    projects = [dict(r) for r in db.execute("""
        SELECT project AS key,
               MIN(cwd) AS cwd,
               COUNT(*) AS n_sessions,
               SUM(n_user) AS n_user,
               SUM(n_asst) AS n_asst,
               SUM(bytes + COALESCE(sub_bytes,0)) AS bytes,
               MAX(ended) AS last, MIN(started) AS first
        FROM sessions GROUP BY project ORDER BY last DESC
    """)]
    for p in projects:
        p["label"] = pretty_project(p["key"], p["cwd"])
        p["name"] = short_name(p["label"])
    tot = dict(db.execute("""
        SELECT COUNT(*) AS sessions, SUM(n_user) AS prompts, SUM(n_asst) AS replies,
               SUM(n_tool) AS tools, SUM(out_tok) AS out_tok, SUM(in_tok) AS in_tok,
               SUM(bytes + COALESCE(sub_bytes,0)) AS bytes, SUM(n_sub) AS subagents,
               MIN(started) AS first, MAX(ended) AS last
        FROM sessions
    """).fetchone())
    models = [dict(r) for r in db.execute("SELECT models, source FROM sessions WHERE models != '[]'")]
    mc: Counter[str] = Counter()
    msrc: dict[str, set] = {}
    for row in models:
        for m in json.loads(row["models"]):
            mc[m] += 1
            msrc.setdefault(m, set()).add(row["source"] or "claude")
    tools: Counter[str] = Counter()
    for row in db.execute("SELECT tools FROM sessions WHERE tools != '[]'"):
        for name, cnt in json.loads(row["tools"]):
            tools[name] += cnt
    usage = api_usage(db)
    codex_n = db.execute("SELECT COUNT(*) c FROM sessions WHERE source='codex'").fetchone()["c"]
    return {"codex": {"chats": codex_n, "problem": CODEX_PROBLEM}, "projects": projects, "totals": tot, "usage": usage,
            "unpriced": unpriced_models(usage), "method": api_method(db),
            "models": mc.most_common(), "tools": tools.most_common(15),
            "model_sources": {m: sorted(v) for m, v in msrc.items()},
            "indexed_at": os.path.getmtime(DB_PATH) if DB_PATH.exists() else None}


def api_usage(db, project: str | None = None) -> list[dict]:
    """Per-model totals, biggest spend first."""
    where, args = ((f"WHERE project = ? AND {HIDE}", [project]) if project
                   else (f"WHERE {HIDE}", []))
    rows = db.execute(f"""
        SELECT model,
               SUM(msgs) AS msgs, COUNT(DISTINCT uid) AS chats,
               SUM(in_tok) AS in_tok, SUM(out_tok) AS out_tok,
               SUM(cache_5m) AS cache_5m, SUM(cache_1h) AS cache_1h,
               SUM(cache_read) AS cache_read
        FROM usage {where} GROUP BY model
    """, args)
    out = []
    for r in rows:
        u = dict(r)
        if u["model"] == "<synthetic>":            # API errors, not a real model
            continue
        u["billed_in"] = (u["in_tok"] or 0) + (u["cache_5m"] or 0) + (u["cache_1h"] or 0) + (u["cache_read"] or 0)
        parts = model_cost(u["model"], u)
        u["parts"] = parts
        u["cost"] = parts["total"] if parts else None
        u["priced"] = parts is not None
        out.append(u)
    out.sort(key=lambda u: (u["cost"] or 0, u["out_tok"] or 0), reverse=True)
    return out


def unpriced_models(usage: list[dict]) -> list[str]:
    """Models seen in the data that PRICES has no entry for - shown in the UI so a
    newly released model reads as 'add a price', not as 'this was free'."""
    return sorted({u["model"] for u in usage if not u["priced"]})


def strips(db, uids: list[str]) -> dict[str, list]:
    """One compact tick per turn, so a chat's shape is readable at row size.
    0 = talk only, 1-3 = how tool-heavy, 4 = a tool failed, 5 = you interrupted it."""
    if not uids:
        return {}
    marks = ",".join("?" * len(uids))
    out: dict[str, list] = {u: [] for u in uids}
    for r in db.execute(
        f"SELECT uid, n_tools, errors, interrupted FROM method "
        f"WHERE uid IN ({marks}) ORDER BY uid, turn", uids
    ):
        n = r["n_tools"] or 0
        if r["interrupted"]:
            v = 5
        elif r["errors"]:
            v = 4
        elif n == 0:
            v = 0
        elif n <= 2:
            v = 1
        elif n <= 6:
            v = 2
        else:
            v = 3
        out[r["uid"]].append(v)
    return out


def api_overview(db, project: str) -> dict:
    """The state of one project: what's running, what's stalled, what it costs."""
    row = db.execute("""
        SELECT COUNT(*) chats, SUM(n_user) prompts, SUM(n_asst) replies,
               SUM(n_tool) tools, SUM(n_errors) errors, SUM(n_interrupts) interrupts,
               SUM(bytes + COALESCE(sub_bytes,0)) bytes, MIN(started) first, MAX(ended) last,
               MIN(cwd) cwd
        FROM sessions WHERE project = ? AND """ + HIDE, (project,)).fetchone()
    usage = api_usage(db, project)
    threads = compute_threads(db, project)
    open_rows = [dict(r) for r in db.execute(
        "SELECT uid,title,ended,open_tasks FROM sessions "
        "WHERE project=? AND open_tasks IS NOT NULL ORDER BY ended DESC", (project,))]
    for o in open_rows:
        o["open_tasks"] = json.loads(o["open_tasks"] or "[]")
    files = [dict(r) for r in db.execute(
        "SELECT path, COUNT(DISTINCT uid) chats, SUM(writes) writes FROM filerefs "
        "WHERE project=? GROUP BY path ORDER BY writes DESC, chats DESC LIMIT 6", (project,))]
    return {
        "project": project, "label": pretty_project(project, row["cwd"]),
        "name": short_name(pretty_project(project, row["cwd"])),
        "stats": dict(row),
        "cost": sum(u["cost"] or 0 for u in usage),
        "models": [{"model": u["model"], "msgs": u["msgs"]} for u in usage[:4]],
        "threads": len(threads),
        "threaded_chats": sum(len(g["chats"]) for g in threads.values()),
        "open": open_rows[:6],
        "files": files,
    }


def api_pickup(db, project: str | None = None, quiet_days: int = 1) -> list[dict]:
    """Work you started and left: threads holding unfinished items, quietest first."""
    threads = compute_threads(db, project)
    chat_thread = {u: root for root, g in threads.items() for u in g["chats"]}
    where, args = ("WHERE project = ?", [project]) if project else ("", [])
    rows = [dict(r) for r in db.execute(
        f"SELECT uid,title,project,cwd,ended,open_tasks,id FROM sessions "
        f"{where + (' AND' if where else 'WHERE')} open_tasks IS NOT NULL", args)]
    groups: dict[str, dict] = {}
    for r in rows:
        try:
            r["open_tasks"] = json.loads(r["open_tasks"] or "[]")
        except ValueError:
            r["open_tasks"] = []
        if not r["open_tasks"]:
            continue
        key = chat_thread.get(r["uid"], r["uid"])
        g = groups.setdefault(key, {"chats": [], "tasks": [], "last": "", "title": r["title"]})
        g["chats"].append({"uid": r["uid"], "title": r["title"], "id": r["id"],
                           "cwd": r["cwd"], "ended": r["ended"]})
        g["tasks"] += r["open_tasks"]
        if (r["ended"] or "") > g["last"]:
            g["last"], g["title"] = r["ended"] or "", r["title"]
    now = datetime.now(timezone.utc)
    out = []
    for key, g in groups.items():
        try:
            age = (now - datetime.fromisoformat(g["last"].replace("Z", "+00:00"))).days
        except (ValueError, AttributeError):
            age = 0
        out.append({"key": key, "title": g["title"], "chats": g["chats"],
                    "tasks": g["tasks"][:8], "n_tasks": len(g["tasks"]),
                    "last": g["last"], "days": age})
    out.sort(key=lambda x: x["last"])
    return out


def pinned_set(db) -> set:
    return {r["uid"] for r in db.execute("SELECT uid FROM pins")}


def deprecated_set(db) -> set:
    return {r["uid"] for r in db.execute("SELECT uid FROM deprecated")}


def user_titles(db) -> dict:
    return {r["uid"]: r["title"] for r in db.execute("SELECT uid, title FROM titles")}


def user_marks(db) -> tuple[set, set, dict]:
    """The three pieces of user state every list needs: pins, hidden, renames."""
    return pinned_set(db), deprecated_set(db), user_titles(db)


def set_title(db, uid: str, title: str | None) -> dict:
    """An empty title clears the override and the derived one comes back."""
    title = (title or "").strip()[:200]
    if title:
        db.execute("INSERT OR REPLACE INTO titles(uid,title,at) VALUES(?,?,?)",
                   (uid, title, datetime.now(timezone.utc).isoformat()))
    else:
        db.execute("DELETE FROM titles WHERE uid = ?", (uid,))
    db.commit()
    row = db.execute("SELECT * FROM sessions WHERE uid = ?", (uid,)).fetchone()
    out = decorate(dict(row), None, None, None, user_titles(db)) if row else {}
    backup_marks(db)
    return {"uid": uid, "title": out.get("title"), "auto_title": out.get("auto_title"),
            "renamed": bool(title)}


# every list-style query hides deprecated chats; analytics (cost, method) keep them,
# because they really did cost that money and really did behave that way
HIDE = "uid NOT IN (SELECT uid FROM deprecated)"


def set_deprecated(db, uid: str, on: bool) -> dict:
    if on:
        db.execute("INSERT OR REPLACE INTO deprecated(uid,at) VALUES(?,?)",
                   (uid, datetime.now(timezone.utc).isoformat()))
        db.execute("DELETE FROM pins WHERE uid = ?", (uid,))      # can't be both
    else:
        db.execute("DELETE FROM deprecated WHERE uid = ?", (uid,))
    db.commit()
    backup_marks(db)
    return {"uid": uid, "deprecated": bool(on), "count": len(deprecated_set(db))}


def api_deprecated(db) -> dict:
    """Only the set-aside chats, grouped by project."""
    titles = user_titles(db)
    rows = [decorate(dict(r), None, None, None, titles) for r in db.execute(
        "SELECT s.*, d.at AS deprecated_at FROM deprecated d JOIN sessions s ON s.uid = d.uid "
        "ORDER BY d.at DESC")]
    st = strips(db, [r["uid"] for r in rows])
    groups: dict[str, dict] = {}
    for r in rows:
        r["strip"] = st.get(r["uid"], [])
        r["deprecated"] = True
        g = groups.setdefault(r["project"], {"project": r["project"], "name": r["name"],
                                             "label": r["label"], "chats": []})
        g["chats"].append(r)
    out = sorted(groups.values(), key=lambda g: -len(g["chats"]))
    return {"groups": out, "count": len(rows),
            "bytes": sum((r["bytes"] or 0) + (r["sub_bytes"] or 0) for r in rows)}


def delete_chat(db, uid: str) -> dict:
    """Move the transcript (and its subagent logs) to the trash, then drop its rows.
    Nothing is unlinked, so a mistake is one `mv` away from being undone."""
    row = db.execute("SELECT path, project, id, source FROM sessions WHERE uid = ?", (uid,)).fetchone()
    if not row:
        return {"error": "unknown chat"}
    if row["source"] == "codex":
        # Codex's databases point at this file; moving it would leave Codex broken
        return {"error": f"cchat does not delete Codex chats. Run: codex delete {row['id']}"}
    src = Path(row["path"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dest_dir = TRASH_DIR / stamp / row["project"]
    moved = []
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.move(str(src), str(dest_dir / src.name))
            moved.append(str(dest_dir / src.name))
        subdir = src.parent / row["id"]                  # <project>/<session-id>/subagents/...
        if subdir.is_dir():
            shutil.move(str(subdir), str(dest_dir / subdir.name))
            moved.append(str(dest_dir / subdir.name))
    except OSError as exc:
        return {"error": f"could not move the transcript: {exc}"}

    for t in ("msgs_fts",):
        db.execute(f"DELETE FROM {t} WHERE rowid IN (SELECT id FROM msgs WHERE uid=?)", (uid,))
    for t in ("msgs", "days", "usage", "method", "filerefs", "sessions",
              "pins", "deprecated", "subagents"):
        db.execute(f"DELETE FROM {t} WHERE uid = ?", (uid,))
    db.execute("DELETE FROM files WHERE uid = ?", (uid,))
    db.commit()
    return {"ok": True, "uid": uid, "moved": moved, "trash": str(TRASH_DIR)}


def api_dashboard(db, project: str | None = None, limit: int = 8) -> dict:
    """Everything worth returning to, in one place: what you pinned, what you touched
    last, what you have poured the most prompts into, and what has eaten the most time.
    Scoped to one project when asked, so the sidebar drives this view too."""
    threads = thread_map(db, project)
    pins, deps, titles = user_marks(db)
    scope = (f"WHERE project = ? AND {HIDE}", [project]) if project else (f"WHERE {HIDE}", [])

    def rows(sql, args=()):
        out = [decorate(dict(r), threads, pins, deps, titles) for r in db.execute(sql, args)]
        st = strips(db, [r["uid"] for r in out])
        for r in out:
            r["strip"] = st.get(r["uid"], [])
        return out

    def lens(order, extra=""):
        where = scope[0]
        if extra:
            where = f"{where} AND {extra}"
        return rows(f"SELECT * FROM sessions {where} ORDER BY {order} LIMIT ?",
                    scope[1] + [limit])

    pin_sql = ("SELECT s.* FROM pins p JOIN sessions s ON s.uid = p.uid"
               " WHERE s.uid NOT IN (SELECT uid FROM deprecated)"
               + (" AND s.project = ?" if project else "")
               + " ORDER BY p.pinned_at DESC")
    totals = dict(db.execute(
        f"SELECT SUM(active_secs) active, COUNT(*) chats, SUM(n_user) prompts, "
        f"SUM(n_errors) errors, MAX(ended) last, MIN(cwd) cwd FROM sessions {scope[0]}",
        scope[1]).fetchone())
    usage = api_usage(db, project)
    return {
        "project": project,
        "label": pretty_project(project, totals["cwd"]) if project else None,
        "name": short_name(pretty_project(project, totals["cwd"])) if project else "All projects",
        "pinned": rows(pin_sql, [project] if project else []),
        "pinned_total": len(pins),
        "recent": lens("ended DESC", "ended IS NOT NULL"),
        "used": lens("n_user DESC"),
        "time": lens("active_secs DESC", "active_secs > 0"),
        "totals": totals,
        "cost": sum(u["cost"] or 0 for u in usage),
        "threads": len(threads),
    }


def set_pin(db, uid: str, on: bool) -> dict:
    if on:
        db.execute("INSERT OR REPLACE INTO pins(uid,pinned_at) VALUES(?,?)",
                   (uid, datetime.now(timezone.utc).isoformat()))
    else:
        db.execute("DELETE FROM pins WHERE uid = ?", (uid,))
    db.commit()
    backup_marks(db)
    return {"uid": uid, "pinned": bool(on), "count": len(pinned_set(db))}


# ------------------------------------------------------------- your own marks

# Pins, renames and deprecations are the only things in the index that cannot be
# rebuilt from the transcripts: they exist nowhere else. So every change writes a
# copy beside the index, and the previous copy is kept whenever the new one has
# fewer rows - which is what a bad DELETE looks like from here.
USER_TABLES = ("pins", "deprecated", "titles", "msgpins")
MARKS_PATH = Path(os.environ.get("CCHAT_MARKS", HERE / "marks.json"))


def backup_marks(db) -> dict:
    data = {t: [dict(r) for r in db.execute(f"SELECT * FROM {t}")] for t in USER_TABLES}
    n = sum(len(v) for v in data.values())
    try:
        if MARKS_PATH.exists():
            old = json.loads(MARKS_PATH.read_text())
            if sum(len(v) for v in old.values() if isinstance(v, list)) > n:
                MARKS_PATH.with_suffix(".prev.json").write_text(json.dumps(old, indent=1))
        tmp = MARKS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str))
        tmp.replace(MARKS_PATH)
    except OSError as exc:                 # a backup that fails must not fail the write
        return {"saved": 0, "error": str(exc)}
    return {"saved": n, "path": str(MARKS_PATH)}


def restore_marks(db, path: Path | None = None) -> dict:
    """Put back everything in the copy that is not in the index. Never removes."""
    src = path or MARKS_PATH
    if not src.exists():
        return {"error": f"no copy at {src}"}
    data = json.loads(src.read_text())
    out = {}
    for t in USER_TABLES:
        rows = [r for r in data.get(t) or [] if isinstance(r, dict)]
        if not rows:
            continue
        cols = list(rows[0])
        before = db.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        db.executemany(
            f"INSERT OR IGNORE INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            [[r.get(c) for c in cols] for r in rows])
        out[t] = db.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] - before
    db.commit()
    return {"restored": out, "from": str(src)}


def marks_of(db, uid: str) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT line, at, who, label FROM msgpins WHERE uid=? ORDER BY line", (uid,))]


def set_mark(db, uid: str, line: int, on: bool, who: str = "", label: str = "") -> dict:
    """Pin or unpin one message inside a chat."""
    if on:
        db.execute("INSERT OR REPLACE INTO msgpins(uid,line,at,who,label) VALUES(?,?,?,?,?)",
                   (uid, line, datetime.now(timezone.utc).isoformat(),
                    (who or "")[:40], " ".join((label or "").split())[:200]))
    else:
        db.execute("DELETE FROM msgpins WHERE uid=? AND line=?", (uid, line))
    db.commit()
    backup_marks(db)
    return {"uid": uid, "line": line, "pinned": bool(on), "marks": marks_of(db, uid)}


def api_marks(db, qs: dict) -> dict:
    """Every pinned message, newest first - the same marks, read across chats."""
    limit = min(int(qs.get("limit") or 100), 500)
    args: list = []
    where = ""
    if qs.get("project"):
        where, args = "WHERE s.project = ?", [qs["project"]]
    rows = [dict(r) for r in db.execute(
        f"SELECT m.*, s.title, s.project, s.cwd FROM msgpins m "
        f"LEFT JOIN sessions s USING(uid) {where} ORDER BY m.at DESC LIMIT ?", (*args, limit))]
    titles = user_titles(db)
    for r in rows:
        r["title"] = titles.get(r["uid"]) or r["title"] or "(untitled)"
        r["label2"] = pretty_project(r["project"], r["cwd"]) if r["project"] else ""
    return {"marks": rows,
            "count": db.execute("SELECT COUNT(*) c FROM msgpins").fetchone()["c"]}


def api_files(db, qs: dict) -> dict:
    """Files these chats touched, most-worked-on first."""
    q = (qs.get("q") or "").strip().lower()
    project = qs.get("project")
    where, args = [], []
    if project:
        where.append("project = ?"); args.append(project)
    if q:
        where.append("lower(path) LIKE ?"); args.append(f"%{q}%")
    sql = "SELECT path, COUNT(DISTINCT uid) chats, SUM(reads) reads, SUM(writes) writes, " \
          "MAX(last_ts) last FROM filerefs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY path ORDER BY writes DESC, chats DESC, reads DESC LIMIT 40"
    return {"files": [dict(r) for r in db.execute(sql, args)]}


def compute_threads(db, project: str | None = None) -> dict[str, dict]:
    """Chats that worked on the same files are almost always the same piece of work
    split across sessions. Union them into threads so a project reads as a handful of
    efforts rather than a pile of chats."""
    where, args = ("WHERE project = ?", [project]) if project else ("", [])
    rows = list(db.execute(
        f"SELECT uid, project, path, writes FROM filerefs {where}", args))
    by_chat: dict[str, set] = {}
    wrote: dict[str, set] = {}
    proj: dict[str, str] = {}
    for r in rows:
        by_chat.setdefault(r["uid"], set()).add(r["path"])
        if r["writes"]:
            wrote.setdefault(r["uid"], set()).add(r["path"])
        proj[r["uid"]] = r["project"]

    parent: dict[str, str] = {u: u for u in by_chat}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    uids = list(by_chat)
    for i, a in enumerate(uids):
        for b in uids[i + 1:]:
            if proj[a] != proj[b]:
                continue
            shared = by_chat[a] & by_chat[b]
            # two files in common, or one file both of them changed
            if len(shared) >= 2 or (shared & wrote.get(a, set()) & wrote.get(b, set())):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

    groups: dict[str, list] = {}
    for u in uids:
        groups.setdefault(find(u), []).append(u)
    return {root: {"chats": members} for root, members in groups.items() if len(members) > 1}


def thread_map(db, project: str | None = None) -> dict[str, dict]:
    """uid -> {id, size} for chats that belong to a multi-chat thread."""
    out = {}
    for root, g in compute_threads(db, project).items():
        for u in g["chats"]:
            out[u] = {"id": root, "size": len(g["chats"])}
    return out


def median(vals: list) -> float:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return 0.0
    n = len(v)
    return float(v[n // 2]) if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def api_method(db, project: str | None = None, min_turns: int = 8) -> list[dict]:
    """How each model goes about the work: what it reaches for first, how many
    moves it makes, whether it reads before editing, whether it checks itself."""
    where, args = ((f"WHERE project = ? AND {HIDE}", [project]) if project
                   else (f"WHERE {HIDE}", []))
    # Claude only: Codex's tools (shell, apply_patch) do not line up with Claude's
    # Read/Edit/Bash, so side by side the columns would compare different things
    where += " AND uid NOT IN (SELECT uid FROM sessions WHERE source = 'codex')"
    rows = list(db.execute(f"SELECT * FROM method {where}", args))
    by_model: dict[str, list] = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)

    out = []
    for model, rs in by_model.items():
        if model == "<synthetic>" or len(rs) < min_turns:
            continue
        seqs = [json.loads(r["tools"] or "[]") for r in rs]
        first = Counter(sq[0] for sq in seqs if sq)
        trans: Counter[tuple] = Counter()
        tool_use: Counter[str] = Counter()
        for sq in seqs:
            tool_use.update(sq)
            trans.update(zip(sq, sq[1:]))
        edited = [r for r in rs if r["edited"]]
        n = len(rs)
        out.append({
            "model": model, "turns": n,
            "tools_per_turn": median([r["n_tools"] for r in rs]),
            "think_pct": round(100 * sum(r["thought"] for r in rs) / n),
            "reads_before_edit": median([r["reads_before_edit"] for r in edited]),
            "deleg_pct": round(100 * sum(r["deleg"] for r in rs) / n),
            "verify_pct": round(100 * sum(r["verified"] for r in edited) / len(edited)) if edited else None,
            "words": median([r["words"] for r in rs]),
            # the transcript times each turn itself - no need to guess from stamps
            "secs": median([r["secs"] for r in rs if r["secs"]]),
            "hours": round(sum(r["secs"] or 0 for r in rs) / 3600, 1),
            "n_edit_turns": len(edited),
            "first": [{"tool": t, "pct": round(100 * c / n)} for t, c in first.most_common(4)],
            "steps": [{"from": a, "to": b, "n": c} for (a, b), c in trans.most_common(5)],
            "tools": [{"tool": t, "n": c} for t, c in tool_use.most_common(6)],
        })
    out.sort(key=lambda m: m["turns"], reverse=True)
    return out


def api_activity(db, project: str | None) -> dict:
    if project:
        rows = db.execute("SELECT day, SUM(n) AS n FROM days WHERE project=? GROUP BY day", (project,))
    else:
        rows = db.execute("SELECT day, SUM(n) AS n FROM days GROUP BY day")
    days = {r["day"]: r["n"] for r in rows}
    if project:
        srows = db.execute("SELECT substr(started,1,10) AS d, COUNT(*) c FROM sessions WHERE project=? GROUP BY d", (project,))
    else:
        srows = db.execute("SELECT substr(started,1,10) AS d, COUNT(*) c FROM sessions GROUP BY d")
    starts = {r["d"]: r["c"] for r in srows if r["d"]}
    return {"days": days, "starts": starts}


SORTS = {
    "recent": "ended DESC",
    "oldest": "ended ASC",
    "longest": "(n_user + n_asst) DESC",
    "biggest": "(bytes + COALESCE(sub_bytes,0)) DESC",
    "prompts": "n_user DESC",
}


def session_filters(db, qs: dict, skip: tuple = ()) -> tuple[list, list]:
    """The WHERE clauses every chat-list filter adds. `skip` leaves some out, so a
    facet can count what each of its own choices would show."""
    where, args = ["1=1", HIDE], []
    if qs.get("project"):
        where.append("project = ?"); args.append(qs["project"])
    if qs.get("day"):
        where.append("uid IN (SELECT uid FROM days WHERE day = ?)"); args.append(qs["day"])
    if qs.get("file"):
        where.append("uid IN (SELECT uid FROM filerefs WHERE path = ?)"); args.append(qs["file"])
    if "source" not in skip and qs.get("source") in ("claude", "codex"):
        where.append("source = ?"); args.append(qs["source"])
    models = [m for m in (qs.get("model") or "").split(",") if m]
    if "model" not in skip and models:
        where.append(f"EXISTS (SELECT 1 FROM json_each(models) WHERE value IN ({','.join('?' * len(models))}))")
        args += models
    # a time window catches every chat that was active inside it, not only ones it wholly holds
    if "time" not in skip and qs.get("since"):
        where.append("ended >= ?"); args.append(qs["since"])
    if "time" not in skip and qs.get("until"):
        where.append("started <= ?"); args.append(qs["until"])
    if qs.get("thread"):
        members = compute_threads(db).get(qs["thread"], {}).get("chats", ["-"])
        where.append(f"uid IN ({','.join('?' * len(members))})"); args += members
    return where, args


# the Time presets, in days back from now; "today" starts at local midnight
TIME_PRESETS = {"today": None, "7": 7, "30": 30, "90": 90, "365": 365}


def api_facets(db, qs: dict) -> dict:
    """How many chats each Agent, Model and Time choice would show, given every
    other filter in force - so a choice never leads to an empty list by surprise."""
    q = (qs.get("q") or "").strip()
    match, margs = "", []
    if q:
        expr = fts_query(q)
        if not expr:
            return {"source": {}, "models": [], "time": {}}
        match = (" AND uid IN (SELECT m.uid FROM msgs_fts JOIN msgs m ON m.id = msgs_fts.rowid"
                 " WHERE msgs_fts MATCH ?)")
        margs = [expr]

    def count(skip, extra="", extra_args=(), group=None):
        where, args = session_filters(db, qs, skip)
        sql = f"FROM sessions WHERE {' AND '.join(where)}{match}{extra}"
        if group:
            return db.execute(f"SELECT {group} k, COUNT(*) c {sql} GROUP BY k", args + margs + list(extra_args)).fetchall()
        return db.execute(f"SELECT COUNT(*) c {sql}", args + margs + list(extra_args)).fetchone()["c"]

    src = {r["k"] or "claude": r["c"] for r in count(("source",), group="source")}
    src[""] = sum(src.values())
    where, args = session_filters(db, qs, ("model",))
    models = [dict(r) for r in db.execute(f"""
        SELECT j.value AS model, COUNT(*) AS n,
               MAX(COALESCE(s.source, 'claude')) AS source, MAX(s.ended) AS last
        FROM (SELECT uid, models, source, ended FROM sessions
              WHERE {' AND '.join(where)}{match}) s, json_each(s.models) j
        WHERE j.value != '<synthetic>'
        GROUP BY j.value ORDER BY n DESC""", args + margs)]
    now = datetime.now()
    time = {"": count(("time",))}
    for key, days in TIME_PRESETS.items():
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) if days is None else now - timedelta(days=days)
        iso = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        time[key] = count(("time",), " AND ended >= ?", [iso])
    return {"source": src, "models": models, "time": time}


def api_sessions(db, qs: dict) -> dict:
    q = (qs.get("q") or "").strip()
    limit = min(int(qs.get("limit") or 60), 500)
    offset = int(qs.get("offset") or 0)
    sort = SORTS.get(qs.get("sort") or "recent", SORTS["recent"])
    where, args = session_filters(db, qs)

    threads = thread_map(db)
    pins, deps, titles = user_marks(db)
    hits: dict[str, list] = {}
    if q:
        expr = fts_query(q)
        if not expr:
            return {"sessions": [], "total": 0, "query": q}
        sub_args = list(args)
        rows = db.execute(f"""
            SELECT m.uid AS sid, m.seq, m.line, m.role, m.ts,
                   snippet(msgs_fts, 0, '<b>', '</b>', ' … ', 18) AS snip,
                   bm25(msgs_fts) AS score
            FROM msgs_fts JOIN msgs m ON m.id = msgs_fts.rowid
            WHERE msgs_fts MATCH ?
              AND m.uid IN (SELECT uid FROM sessions WHERE {' AND '.join(where)})
            ORDER BY score LIMIT 4000
        """, [expr] + sub_args)
        order: list[str] = []
        for r in rows:
            sid = r["sid"]
            if sid not in hits:
                hits[sid] = []
                order.append(sid)
            if len(hits[sid]) < 4:
                hits[sid].append({"seq": r["seq"], "line": r["line"], "role": r["role"],
                                  "ts": r["ts"], "snip": r["snip"]})
        total = len(order)
        page = order[offset:offset + limit]
        if not page:
            return {"sessions": [], "total": total, "query": q}
        marks = ",".join("?" * len(page))
        srows = {r["uid"]: dict(r) for r in db.execute(
            f"SELECT * FROM sessions WHERE uid IN ({marks})", page)}
        out = []
        for sid in page:
            row = srows.get(sid)
            if not row:
                continue
            row["hits"] = hits[sid]
            row["n_hits"] = len(hits[sid])
            out.append(decorate(row, threads, pins, deps, titles))
        st = strips(db, [r["uid"] for r in out])
        for r in out:
            r["strip"] = st.get(r["uid"], [])
        return {"sessions": out, "total": total, "query": q}

    total = db.execute(f"SELECT COUNT(*) c FROM sessions WHERE {' AND '.join(where)}", args).fetchone()["c"]
    rows = db.execute(
        f"SELECT * FROM sessions WHERE {' AND '.join(where)} ORDER BY {sort} LIMIT ? OFFSET ?",
        args + [limit, offset])
    out = [decorate(dict(r), threads, pins, deps, titles) for r in rows]
    st = strips(db, [r["uid"] for r in out])
    for r in out:
        r["strip"] = st.get(r["uid"], [])
    return {"sessions": out, "total": total, "query": ""}


def decorate(row: dict, threads: dict | None = None, pins: set | None = None,
             deprecated: set | None = None, titles: dict | None = None) -> dict:
    for k in ("models", "branches", "entrypoints", "tools", "agents"):
        try:
            row[k] = json.loads(row.get(k) or "[]")
        except ValueError:
            row[k] = []
    row["label"] = pretty_project(row["project"], row["cwd"])
    row["name"] = short_name(row["label"])
    row["resume"], row["resume_problem"] = resume_target(row)
    try:
        row["open_tasks"] = json.loads(row.get("open_tasks") or "[]")
    except ValueError:
        row["open_tasks"] = []
    if threads is not None:
        row["thread"] = threads.get(row["uid"])
    if pins is not None:
        row["pinned"] = row["uid"] in pins
    if deprecated is not None:
        row["deprecated"] = row["uid"] in deprecated
    if not row.get("title"):
        fp = (row.get("first_prompt") or row.get("last_prompt") or "").strip()
        row["title"] = (fp[:70] + ("…" if len(fp) > 70 else "")) or "(untitled)"
    row["auto_title"] = row["title"]                  # what the transcript implies
    if titles and row["uid"] in titles:               # what you called it
        row["title"] = titles[row["uid"]]
        row["renamed"] = True
    return row


def user_parts(text: str) -> list[dict]:
    """A user turn is often a slash command plus its console output, wrapped in
    markup that is noise on screen. Split it into typed parts and drop the rest."""
    t = ANSI.sub("", SYS_REMINDER.sub("", CAVEAT.sub("", text)))
    parts = []
    name = CMD_NAME.search(t)
    if name:
        args = CMD_ARGS.search(t)
        label = name.group(1).strip() + (" " + args.group(1).strip() if args and args.group(1).strip() else "")
        parts.append({"kind": "command", "text": label})
    for out in CMD_OUT.finditer(t):
        body = out.group(1).strip()
        if body:
            parts.append({"kind": "stdout", "text": body[:TOOL_OUT_CAP]})
    rest = CMD_TAG.sub("", t).strip()
    if rest:
        parts.insert(0 if not parts else len(parts), {"kind": "text", "text": rest})
    return parts


READ_KIND = {"Read", "Grep", "Glob", "LS", "NotebookRead"}
EDIT_KIND = {"Edit", "Write", "MultiEdit", "NotebookEdit", "ApplyPatch"}
DELEGATE_KIND = {"Agent", "Task", "Workflow"}
VERIFY_RE = re.compile(
    r"\b(test|tests|pytest|unittest|tox|nose|jest|vitest|mocha|jasmine|karma|rspec|minitest|"
    r"phpunit|ctest|bazel|buck|dotnet\s+test|go\s+test|cargo\s+(t|test|check|clippy)|"
    r"mvn|gradle|sbt|make\s+(check|test|lint)|npm\s+(t|run\s+(test|lint|build|typecheck))|"
    r"(pnpm|yarn|bun|deno|uv|poetry|pipenv)\s+\S*(test|lint|check|build)|"
    r"tsc|eslint|biome|prettier|ruff|flake8|black|mypy|pyright|golangci|shellcheck|"
    r"build|lint|typecheck|verify)\b", re.I)

READ_TOOLS = {"read", "glob", "grep", "ls", "notebookread", "lsp"}
EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "applypatch"}
TASK_TOOLS = {"taskcreate", "taskupdate", "tasklist", "taskget", "taskoutput",
              "taskstop", "todowrite", "toolsearch"}


def tool_kind(name: str) -> str:
    """Group a tool call so the transcript can show it as its own kind of thing."""
    n = (name or "").lower()
    if n in ("task", "agent"):
        return "agent"
    if n.startswith("websearch") or n.startswith("webfetch"):
        return "search"
    if n.startswith("mcp__"):
        return "mcp"
    if n in ("bash", "bashoutput", "killshell"):
        return "bash"
    if n in EDIT_TOOLS:
        return "edit"
    if n in READ_TOOLS:
        return "read"
    if n in ("skill", "slashcommand", "workflow"):
        return "skill"
    if n in TASK_TOOLS:
        return "task"
    return "tool"


def tool_label(name: str, kind: str) -> str:
    if kind == "mcp":
        bits = name.split("__")
        return " · ".join(bits[1:]) if len(bits) > 2 else name.replace("mcp__", "")
    return name


def tool_brief(inp, kind: str) -> str:
    """The one line worth showing before you expand the call."""
    if not isinstance(inp, dict):
        return str(inp)[:200]
    pick = {
        "bash": ("command", "description"),
        "read": ("file_path", "path", "pattern", "query"),
        "edit": ("file_path", "path", "notebook_path"),
        "search": ("query", "url", "prompt"),
        "agent": ("description", "prompt"),
        "skill": ("skill", "command", "name"),
        "task": ("description", "prompt", "query"),
    }.get(kind, ("description", "command", "file_path", "query", "prompt", "path", "pattern"))
    for k in pick:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:220]
    for v in inp.values():                      # nothing named usefully - take the first string
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:220]
    return ""


def result_text(b: dict) -> tuple[str, int, bool]:
    c = b.get("content")
    if isinstance(c, list):
        txt = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    elif isinstance(c, str):
        txt = c
    else:
        try:
            txt = json.dumps(c, indent=2)
        except (TypeError, ValueError):
            txt = str(c)
    txt = ANSI.sub("", txt or "")
    return txt[:TOOL_OUT_CAP], max(0, len(txt) - TOOL_OUT_CAP), bool(b.get("is_error"))


PATH_KEYS = ("file_path", "path", "notebook_path")
TEXT_KEYS = ("command", "query", "pattern", "url", "prompt", "description", "sql")


def tool_search_text(name: str, inp) -> str:
    """What to put in the search index for a tool call: the name, its target, and
    the payload that makes it findable later (the command, the SQL, the new code)."""
    if not isinstance(inp, dict):
        return f"{name} {str(inp)[:400]}"
    bits = [name]
    for k in PATH_KEYS:
        if isinstance(inp.get(k), str):
            bits.append(inp[k])
    for k in TEXT_KEYS:
        if isinstance(inp.get(k), str):
            bits.append(inp[k][:2000])
    for k in ("new_string", "content"):            # the code an edit introduced
        if isinstance(inp.get(k), str):
            bits.append(inp[k][:1500])
    if len(bits) == 1:                             # nothing named usefully (MCP args)
        try:
            bits.append(json.dumps(inp)[:1200])
        except (TypeError, ValueError):
            bits.append(str(inp)[:600])
    return " ".join(bits)


def tool_paths(name: str, inp) -> list[str]:
    if not isinstance(inp, dict):
        return []
    return [inp[k] for k in PATH_KEYS if isinstance(inp.get(k), str) and inp[k].startswith("/")]


def render_event(d: dict, lineno: int) -> dict | None:
    t = d.get("type")
    if t == "system":
        # things that happened to the chat: a recap written while you were away,
        # the point where its memory was compacted, a failed API call
        sub, part = d.get("subtype"), None
        if sub == "away_summary":
            txt = re.sub(r"\s*\(disable recaps in /config\)\s*$", "",
                         (d.get("content") or "").strip())
            if txt:
                part = {"kind": "moment", "what": "away", "text": txt}
        elif sub == "compact_boundary":
            cm = d.get("compactMetadata") or {}
            pre, post = int(cm.get("preTokens") or 0), int(cm.get("postTokens") or 0)
            part = {"kind": "moment", "what": "compact", "a": pre, "b": post,
                    "secs": round((cm.get("durationMs") or 0) / 1000),
                    "text": f"{cm.get('trigger') or 'auto'} compaction"}
        elif sub == "api_error":
            err = d.get("error") or {}
            part = {"kind": "moment", "what": "error",
                    "text": f"api error {err.get('status') or '?'}"
                            + (f" · retry {d.get('retryAttempt')}/{d.get('maxRetries')}"
                               if d.get("retryAttempt") else "")}
        if not part:
            return None
        return {"role": "system", "side": bool(d.get("isSidechain")), "ts": d.get("timestamp"),
                "line": lineno, "model": None, "parts": [part], "is_cmd": False,
                "uuid": d.get("uuid")}
    if t not in ("user", "assistant"):
        return None
    msg = d.get("message") or {}
    content = msg.get("content")
    parts = []
    if isinstance(content, str):
        parts += user_parts(content) if t == "user" else [{"kind": "text", "text": content}]
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                raw = b.get("text") or ""
                parts += user_parts(raw) if t == "user" else [{"kind": "text", "text": raw}]
            elif bt == "thinking":
                # recent transcripts keep only the signature, so the text is empty
                if (b.get("thinking") or "").strip():
                    parts.append({"kind": "thinking", "text": b["thinking"]})
            elif bt == "tool_use":
                inp = b.get("input") or {}
                name = b.get("name") or "?"
                kind = tool_kind(name)
                try:
                    pretty = json.dumps(inp, indent=2)[:TOOL_OUT_CAP]
                except (TypeError, ValueError):
                    pretty = str(inp)[:TOOL_OUT_CAP]
                parts.append({
                    "kind": "tool", "tool": kind, "name": name,
                    "label": tool_label(name, kind), "brief": tool_brief(inp, kind),
                    "input": pretty, "id": b.get("id"),
                    "agent_type": inp.get("subagent_type") if isinstance(inp, dict) else None,
                })
            elif bt == "tool_result":
                txt, over, err = result_text(b)
                parts.append({"kind": "tool_result", "text": txt, "truncated": over,
                              "error": err, "for": b.get("tool_use_id")})
    if not parts:
        return None
    return {
        "role": t, "side": bool(d.get("isSidechain")), "ts": d.get("timestamp"),
        "line": lineno, "model": msg.get("model"), "parts": parts,
        "is_cmd": all(p["kind"] in ("command", "stdout") for p in parts),
        "uuid": d.get("uuid"),
    }


def api_transcript(db, qs: dict) -> dict:
    sid = qs.get("id")
    sub = qs.get("sub")
    start = int(qs.get("from") or 0)
    limit = min(int(qs.get("limit") or 400), 2000)
    if sub:
        row = db.execute("SELECT * FROM subagents WHERE path=?", (sub,)).fetchone()
        if not row:
            return {"error": "unknown subagent log"}
        path, meta = Path(row["path"]), {"kind": "subagent", "name": row["name"],
                                         "parent": row["uid"], "bytes": row["bytes"],
                                         "path": row["path"], "n_lines": row["n_lines"]}
    else:
        row = db.execute("SELECT * FROM sessions WHERE uid=?", (sid,)).fetchone()
        if not row:
            return {"error": "unknown session"}
        meta = decorate(dict(row), None, pinned_set(db), deprecated_set(db), user_titles(db))
        meta["kind"] = "session"
        meta["subagents"] = [dict(r) for r in db.execute(
            "SELECT path,name,bytes,n_lines FROM subagents WHERE uid=? ORDER BY name", (sid,))]
        meta["files"] = [dict(r) for r in db.execute(
            "SELECT path,reads,writes FROM filerefs WHERE uid=? "
            "ORDER BY writes DESC, reads DESC LIMIT 40", (sid,))]
        meta["thread"] = thread_map(db).get(sid)
        meta["marks"] = marks_of(db, sid)
        meta["turns"] = [
            {"line": r["line"], "n": r["n_tools"], "e": r["errors"],
             "i": r["interrupted"], "w": r["words"]}
            for r in db.execute(
                "SELECT line,n_tools,errors,interrupted,words FROM method "
                "WHERE uid=? ORDER BY turn", (sid,))
        ]
        path = Path(row["path"])
        if row["source"] == "codex":
            try:
                return codex_transcript(row, meta, qs)
            except (CodexFormat, sqlite3.Error) as exc:
                return {"error": f"could not read this Codex chat: {exc}"}
    if not path.exists():
        return {"error": f"transcript file is gone: {path}"}

    # A chat you are still having opens at its end, not at its beginning - that
    # is where the answer you are waiting for lands. Asked for explicitly, so a
    # deep link to a line and the "jump to start" button still win.
    tailed = False
    if qs.get("tail") == "auto" and start == 0:
        try:
            fresh = time.time() - path.stat().st_mtime < IDLE_GAP
        except OSError:
            fresh = False
        if fresh:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                total = sum(1 for _ in fh)
            if total > limit:
                start, tailed = total - limit, True

    # A tool result arrives as its own user-role record, later in the file. Fold each
    # one back into the call it answers so the transcript reads as one block per call
    # instead of alternating "claude / you / claude / you".
    events, skipped, read_to, done, page_end = [], 0, 0, False, 0
    pending: dict[str, dict] = {}
    for lineno, d, nbytes in iter_lines(path, start):
        read_to = lineno + 1
        if d is None:
            skipped += 1
            if not done:
                events.append({"role": "assistant", "side": False, "ts": None, "line": lineno,
                               "parts": [{"kind": "tool_result", "error": False, "truncated": nbytes,
                                          "text": f"[{nbytes/1e6:,.1f} MB payload not shown]"}]})
            continue
        ev = render_event(d, lineno)
        if not ev:
            continue

        keep = []
        for p in ev["parts"]:
            if p["kind"] == "tool_result" and pending.get(p.get("for")) is not None:
                call = pending.pop(p["for"])
                call["result"] = {"text": p["text"], "truncated": p["truncated"], "error": p["error"]}
            elif done:
                continue                       # past the page: only filling in results
            else:
                keep.append(p)
                if p["kind"] == "tool" and p.get("id"):
                    pending[p["id"]] = p
        if keep and not done:
            ev["parts"] = keep
            events.append(ev)
        if not done and len(events) >= limit:
            done, page_end = True, lineno
        # once the page is full, read a little further only to close open calls
        if done and (not pending or lineno > page_end + 400):
            break
    # read_to is where this page stopped; n_lines is the whole file (from the index)
    try:
        fst = path.stat()
        fstat = {"size": fst.st_size, "mtime": fst.st_mtime}
    except OSError:
        fstat = None
    # the page starts watching from exactly what it just rendered, so a turn that
    # lands between this read and the first poll is not silently adopted
    return {"meta": meta, "events": events, "from": start, "read_to": read_to, "tailed": tailed,
            "file": fstat,
            "next": events[-1]["line"] + 1 if events and len(events) >= limit and not tailed else None,
            "n_lines": meta.get("n_lines") or read_to, "huge_skipped": skipped}


# ---------------------------------------------------------------------- handoff
# A chat written down for another agent - Codex, ChatGPT, anything that reads
# markdown. The receiver needs what was asked, what was said and what was done,
# not 4,000 characters of every `ls`. So tool calls shrink to one line each,
# outputs are dropped unless they failed, and thinking stays behind.

HANDOFF_BUDGET = 120_000   # chars, ~30k tokens: fits a ChatGPT paste and a Codex prompt
HANDOFF_MSG_CAP = 6_000    # one pasted log should not eat the whole budget

# which agent wrote a chat: (product, what it is, how a reply is signed)
AGENTS = {"claude": ("Claude Code", "Anthropic's coding agent", "Claude"),
          "codex": ("Codex", "OpenAI's coding agent", "Codex")}

HANDOFF_PREAMBLE = """\
You are picking up a coding session that started in {product} ({what}).
Below is a condensed record of it: what the user asked, what the agent replied, and
what it did. Tool calls are one line each. Their output is left out unless the call failed.

Read it, then continue the work from where it stopped. Do not redo steps that already
succeeded. The files may have changed since the session ended, so check their current
state before you trust what the transcript says about them."""


def _clip(text: str, cap: int | None = HANDOFF_MSG_CAP) -> str:
    text = text.strip()
    if cap is None or len(text) <= cap:
        return text
    return text[:cap].rstrip() + f"\n\n[… {len(text) - cap:,} more characters cut]"


def _code(s: str) -> str:
    """Inline code that survives backticks inside it."""
    s = " ".join(s.split())
    fence = "``" if "`" in s else "`"
    return f"{fence} {s} {fence}" if fence == "``" else f"`{s}`"


def _when(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts[:16]


def handoff_events(row):
    """Every event of a chat, from whichever agent wrote it."""
    if row["source"] == "codex":
        models = json.loads(row["models"] or "[]")
        for r in codex_items(row["id"]):
            ev = codex_event(r, models[0] if models else None)
            if ev:
                yield ev
        return
    for lineno, d, _ in iter_lines(Path(row["path"])):
        ev = render_event(d, lineno) if d is not None else None
        if ev:
            yield ev


def handoff_blocks(events, msg_cap: int | None = HANDOFF_MSG_CAP) -> list[dict]:
    """The chat as a list of {who, ts, model, items, line}, consecutive assistant
    records merged. Items are strings, or dicts for tool calls that a later
    result may still mark as failed (Claude) or that carry their result (Codex)."""
    blocks: list[dict] = []
    calls: dict[str, dict] = {}

    def block(who, ev):
        if not blocks or blocks[-1]["who"] != who or who == "moment":
            blocks.append({"who": who, "ts": ev.get("ts"), "model": ev.get("model"),
                           "items": [], "line": ev["line"]})
        return blocks[-1]

    for ev in events:
        if ev["side"] or ev.get("is_cmd"):
            continue
        for p in ev["parts"]:
            k = p["kind"]
            if k == "tool_result":
                call = calls.pop(p.get("for"), None)
                if call is not None and p["error"]:
                    call["error"] = (p["text"] or "").strip().split("\n")[0][:240]
            elif k == "moment":
                if p["what"] == "compact":
                    block("moment", ev)["items"].append(
                        "*The agent's context was compacted here — it lost detail of "
                        "everything above this point.*")
                elif p["what"] == "away":
                    block("moment", ev)["items"].append(f"*Agent's recap:* {p['text']}")
            elif k == "text" and p["text"].strip():
                who = "user" if ev["role"] == "user" else "assistant"
                block(who, ev)["items"].append(_clip(p["text"], msg_cap))
            elif k == "tool":
                res = p.get("result")
                call = {"label": p["label"], "brief": p["brief"],
                        "error": (res["text"] or "failed").strip().split("\n")[0][:240]
                        if res and res["error"] else None}
                block("assistant", ev)["items"].append(call)
                if p.get("id"):
                    calls[p["id"]] = call
    return [b for b in blocks if b["items"]]


def _render_block(b: dict, signed: str = "Claude") -> str:
    head = {"user": "User", "assistant": signed, "moment": "—"}[b["who"]]
    bits = [head]
    if b["who"] == "assistant" and b.get("model"):
        bits.append(str(b["model"]).replace("claude-", ""))
    if b["who"] != "moment" and b.get("ts"):
        bits.append(_when(b["ts"]))
    out, run = [f"### {' · '.join(bits)}"], []

    def flush():
        if run:
            out.append("\n".join(run))
            run.clear()

    for it in b["items"]:
        if isinstance(it, dict):
            line = f"- {it['label']}" + (f" {_code(it['brief'])}" if it["brief"] else "")
            if it["error"]:
                line += f" — **failed:** {it['error']}"
            run.append(line)
        else:
            flush()
            out.append(it)
    flush()
    return "\n\n".join(out)


def handoff_markdown(db, uid: str, budget: int | None = HANDOFF_BUDGET,
                     msg_cap: int | None = HANDOFF_MSG_CAP) -> dict:
    """budget and msg_cap of None give the whole chat - a download has no paste limit."""
    row = db.execute("SELECT * FROM sessions WHERE uid=?", (uid,)).fetchone()
    if not row:
        return {"error": "unknown chat"}
    m = decorate(dict(row), None, None, None, user_titles(db))
    path = Path(m["path"])
    if not path.exists():
        return {"error": f"transcript file is gone: {path}"}
    product, what, signed = AGENTS.get(m["source"] or "claude", AGENTS["claude"])

    head = [f"# Handoff: {m['title']}", "", HANDOFF_PREAMBLE.format(product=product, what=what),
            "", "## Session", ""]
    facts = [
        ("Working directory", _code(m["cwd"] or m["label"])),
        ("Git branch", ", ".join(m["branches"])),
        ("Started", _when(m["started"])),
        ("Last active", _when(m["ended"])),
        ("Models", ", ".join(x.replace("claude-", "") for x in m["models"] if x != "<synthetic>")),
        ("Size", f"{m['n_user'] or 0} prompts, {m['n_tool'] or 0} tool calls"),
        (f"{product} session", _code(m["id"])),
    ]
    head += [f"- {k}: {v}" for k, v in facts if v]

    files = db.execute("SELECT path,reads,writes FROM filerefs WHERE uid=? "
                       "ORDER BY writes DESC, reads DESC", (uid,)).fetchall()
    edited = [f for f in files if f["writes"]]
    if edited:
        head += ["", "## Files it changed", ""]
        root = (m["cwd"] or "").rstrip("/") + "/"
        head += [f"- {_code(f['path'][len(root):] if f['path'].startswith(root) else f['path'])}"
                 f" ({f['writes']} edit{'s' if f['writes'] != 1 else ''})" for f in edited[:40]]
        if len(edited) > 40:
            head.append(f"- … and {len(edited) - 40} more")
    if m["open_tasks"]:
        head += ["", "## Unfinished tasks", "",
                 "From the agent's last task list. It may be stale.", ""]
        head += [f"- [{t.get('status') or 'open'}] {t.get('subject') or ''}"
                 for t in m["open_tasks"]]

    try:
        blocks = handoff_blocks(handoff_events(row), msg_cap)
    except (CodexFormat, sqlite3.Error) as exc:
        return {"error": f"could not read this chat: {exc}"}
    rendered = [_render_block(b, signed) for b in blocks]
    head_txt = "\n".join(head)

    # Over budget: keep the opening ask and as much of the end as fits. The end
    # is where the work is; the start is what it was for.
    omitted = 0
    room = budget - len(head_txt) - 400 if budget else 0
    if budget and sum(len(r) + 2 for r in rendered) > room and len(rendered) > 2:
        first = next((i for i, b in enumerate(blocks) if b["who"] == "user"), 0)
        keep_head = rendered[:first + 1]
        room -= sum(len(r) + 2 for r in keep_head)
        tail: list[str] = []
        for r in reversed(rendered[first + 1:]):
            if room - len(r) - 2 < 0 and tail:
                break
            tail.insert(0, r)
            room -= len(r) + 2
        omitted = len(rendered) - len(keep_head) - len(tail)
        if omitted:
            note = (f"*[… {omitted} messages from the middle of the session are left out to "
                    f"fit. The full transcript is the JSONL file at {_code(str(path))}.]*")
            rendered = keep_head + [note] + tail

    last_user = next((b for b in reversed(blocks) if b["who"] == "user"), None)
    tail_txt = ["", "---", "", "## Where it stopped", ""]
    if last_user:
        ask = last_user["items"][0] if isinstance(last_user["items"][0], str) else ""
        tail_txt += ["The user's last request was:", "",
                     "\n".join("> " + ln for ln in _clip(ask, 1500).split("\n"))]
    if blocks and blocks[-1]["who"] == "user":
        tail_txt += ["", "The agent had not answered it yet."]
    tail_txt += ["", "Continue from here."]

    text = (head_txt + "\n\n## Transcript\n\n" + "\n\n".join(rendered)
            + "\n" + "\n".join(tail_txt) + "\n")
    return {"markdown": text, "chars": len(text), "tokens": len(text) // 4,
            "omitted": omitted, "messages": len(blocks), "title": m["title"],
            "id": m["id"], "cwd": m["cwd"], "source": m["source"] or "claude", "row": row}


def handoff_filename(h: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (h["title"] or "chat").lower()).strip("-")[:48] or "chat"
    return f"{slug}-{h['id'][:8]}.md"


def launch_handoff(db, uid: str) -> dict:
    """Write the handoff to disk and open the *other* agent in the chat's directory,
    told to read it: a Claude chat goes to Codex, a Codex chat goes to Claude Code.
    The prompt is a pointer, not the text: a 100 KB argv makes an unreadable first turn."""
    h = handoff_markdown(db, uid, None, None)     # the agent reads a file: no paste limit
    if h.get("error"):
        return h
    _, problem = resume_target(h["row"])
    if problem:
        return {"error": problem}
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    out = HANDOFF_DIR / handoff_filename(h)
    out.write_text(h["markdown"], encoding="utf-8")
    # it starts by reporting, not acting: told only to "continue", it could commit or
    # push work the user had deliberately held back in the first session
    frm, to = ("codex", "claude") if h["source"] == "codex" else ("claude", "codex")
    prompt = (f"Read the handoff file {out}. It records a {AGENTS[frm][0]} session in this "
              f"directory. Tell me in a few lines where the work stands and what you "
              f"would do next, then wait for me before you change anything.")
    cmd = f"cd {shlex.quote(h['cwd'])} && {to} {shlex.quote(prompt)}"
    open_terminal(cmd)
    return {"ok": True, "file": str(out), "command": cmd, "tokens": h["tokens"],
            "agent": AGENTS[to][0]}


def api_recaps(db, qs: dict) -> dict:
    """The recaps Claude writes when you step away, newest first.

    Nothing is generated here. Claude already wrote these lines into the
    transcript; they were simply never shown to you.
    """
    limit = min(int(qs.get("limit") or 60), 300)
    offset = int(qs.get("offset") or 0)
    kinds = [k for k in (qs.get("kind") or "away").split(",") if k in ("away", "compact", "error")]
    args: list = list(kinds)
    where = [f"m.kind IN ({','.join('?' * len(kinds))})"]
    if qs.get("project"):
        where.append("m.project = ?")
        args.append(qs["project"])
    if qs.get("uid"):
        where.append("m.uid = ?")
        args.append(qs["uid"])
    dep = deprecated_set(db)
    sql = (f"SELECT m.*, s.title, s.ai_title, s.custom_title, s.cwd, s.id AS sid "
           f"FROM moments m LEFT JOIN sessions s USING(uid) WHERE {' AND '.join(where)} "
           f"ORDER BY m.ts DESC LIMIT ? OFFSET ?")
    rows = [dict(r) for r in db.execute(sql, (*args, limit + len(dep) + 1, offset))]
    titles = user_titles(db)
    out = []
    for r in rows:
        if r["uid"] in dep:
            continue
        r["title"] = titles.get(r["uid"]) or r["title"] or "(untitled)"
        r["label"] = pretty_project(r["project"], r["cwd"])
        out.append(r)
    total = db.execute(
        f"SELECT COUNT(*) c FROM moments m WHERE {' AND '.join(where)}", args).fetchone()["c"]
    return {"moments": out[:limit], "total": total,
            "more": len(out) > limit,
            "counts": {r["kind"]: r["n"] for r in db.execute(
                "SELECT kind, COUNT(*) n FROM moments GROUP BY kind")}}


# ------------------------------------------------------------- chats to commits

_git_lock = threading.Lock()
_git_cache: dict[str, tuple[float, dict]] = {}       # uid -> (session mtime, answer)


def git(cwd: str, *args: str, timeout: int = 12) -> str:
    """Read-only git. Anything that fails answers with nothing at all."""
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def api_commits(db, qs: dict) -> dict:
    """The commits that came out of a chat.

    A chat knows which files it edited and when it ran. Git knows which commits
    touched which files and when they landed. Overlap the two and a conversation
    points at the code it produced - which is the question you actually ask of a
    history six months later.
    """
    row = db.execute("SELECT uid,cwd,started,ended,mtime FROM sessions WHERE uid=?",
                     (qs.get("uid"),)).fetchone()
    if not row:
        return {"error": "unknown chat"}
    uid, cwd = row["uid"], row["cwd"]
    with _git_lock:
        hit = _git_cache.get(uid)
    if hit and hit[0] == row["mtime"]:
        return hit[1]

    out = {"commits": [], "repo": None}
    if not cwd or not Path(cwd).is_dir():
        return out
    top = git(cwd, "rev-parse", "--show-toplevel").strip()
    if not top:
        return out                                   # not a repo, or git is unhappy
    out["repo"] = top

    touched = {r["path"] for r in db.execute(
        "SELECT path FROM filerefs WHERE uid=? AND writes > 0", (uid,))}
    names = {Path(p).name for p in touched}
    if not touched:
        return out

    # The work lands in git after the chat ends - often hours later, at the end of
    # the day. Epoch seconds keep git from reading a UTC stamp as local time.
    def epoch(iso: str | None) -> float | None:
        try:
            return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    t0, t1 = epoch(row["started"]), epoch(row["ended"])
    if t0 is None:
        return out
    log = git(top, "log", "--no-merges", f"--since=@{int(t0 - 2 * 3600)}",
              f"--until=@{int((t1 or t0) + 24 * 3600)}", "--name-only",
              "--pretty=format:%x1e%H%x1f%aI%x1f%an%x1f%s")
    for chunk in log.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head, _, body = chunk.partition("\n")
        bits = head.split("\x1f")
        if len(bits) < 4:
            continue
        sha, when, who, subject = bits[0], bits[1], bits[2], bits[3]
        files = [f for f in body.split("\n") if f.strip()]
        exact = [f for f in files if str(Path(top) / f) in touched]
        hits = exact or [f for f in files if Path(f).name in names]
        if not hits:
            continue
        out["commits"].append({
            "sha": sha[:8], "full": sha, "ts": when, "who": who, "subject": subject,
            "files": len(files), "matched": len(hits), "exact": len(exact),
            "after": bool(t1 and (epoch(when) or 0) > t1), "hit_files": hits[:12],
        })
    out["commits"].sort(key=lambda c: c["ts"])
    # The window reaches a day past the chat because that is when people commit.
    # A match on a file name alone is weaker than a match on the whole path, so
    # both counts travel with the commit instead of one being hidden.
    with _git_lock:
        _git_cache[uid] = (row["mtime"], out)
    return out


# ------------------------------------------------------------------ what's live

LIVE_TAIL = 96_000          # bytes of a transcript that tell us what it is doing now
WORKING_SECS = 25           # a file this fresh is being written to right now
NOW_WINDOW = 45 * 60        # how far back "recently" reaches


def tail_records(path: Path, nbytes: int = LIVE_TAIL) -> list[dict]:
    """The last handful of records in a transcript, without reading the file."""
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - nbytes))
            chunk = fh.read()
    except OSError:
        return []
    if size > nbytes and b"\n" in chunk:
        chunk = chunk.split(b"\n", 1)[1]        # drop the half record we landed in
    out = []
    for raw in chunk.splitlines():
        if not raw.strip() or len(raw) > HUGE_LINE:
            continue
        try:
            out.append(json.loads(raw.decode("utf-8", "replace")))
        except ValueError:
            pass
    return out


def live_shape(recs: list[dict]) -> dict:
    """What the end of a transcript says about the chat, age aside.

    Cached per (file, mtime), so the state it feeds can stay live for free.
    """
    pending: dict[str, dict] = {}
    facts = {"text": "", "tool": None, "model": None, "prompt": None}
    for d in recs:
        if not isinstance(d, dict):
            continue
        msg = d.get("message") or {}
        content = msg.get("content")
        if d.get("type") == "assistant":
            facts["model"] = msg.get("model") or facts["model"]
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and d.get("type") == "assistant":
                t = " ".join((b.get("text") or "").split())[:160]
                if t:
                    facts["text"] = t
            elif b.get("type") == "tool_use":
                nm, inp = b.get("name") or "?", b.get("input") or {}
                kind = tool_kind(nm)
                tool = {"name": tool_label(nm, kind), "brief": tool_brief(inp, kind)[:120]}
                facts["tool"] = tool
                if b.get("id"):
                    pending[b["id"]] = tool
            elif b.get("type") == "tool_result":
                pending.pop(b.get("tool_use_id"), None)
        if d.get("type") == "user" and not d.get("isSidechain") and isinstance(content, str):
            t = " ".join(clean(content).split())[:160]
            if t:
                facts["prompt"] = t
    facts["pending"] = next(iter(pending.values()), None)
    return facts


def live_state(facts: dict, age: float) -> dict:
    """working  the file is still moving - it is doing something right now
       blocked  a tool call has no result and nothing is being written: it is on a
                permission prompt, or on a command that has not come back
       waiting  it answered and stopped. Your turn.
       idle     nobody has touched it in a while"""
    if age < WORKING_SECS:
        state = "working"
    elif facts.get("pending"):
        state = "blocked"
    elif age < NOW_WINDOW:
        state = "waiting"
    else:
        state = "idle"
    t = facts.get("pending") if state == "blocked" else (
        facts.get("tool") if state == "working" else None)
    last = (f"{t['name']}: {t['brief']}" if t and t["brief"] else t["name"] if t else
            facts.get("text") or facts.get("prompt") or "")
    return {"state": state, "last": last, "model": facts.get("model")}


_now_lock = threading.Lock()
_now_cache: dict[str, tuple[float, dict]] = {}       # path -> (mtime read at, facts)


def api_now(db, qs: dict) -> dict:
    """Every chat that has moved lately, and what it is doing.

    This is the board you watch when three sessions are running and you want to
    know which one is waiting on you. It reads the last 96 KB of each recent
    transcript, and only when that file has changed since the last look.
    """
    window = min(int(qs.get("mins") or 45), 24 * 60) * 60
    now, out = time.time(), []
    titles = user_titles(db)
    codex = codex_threads()
    for path, project, parent in transcript_files() + [
            (Path(p), codex_project(t["cwd"]), None) for p, t in codex.items()]:
        if parent is not None:                       # subagent logs belong to their chat
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        age = now - st.st_mtime
        if age > window:
            continue
        key = str(path)
        with _now_lock:
            hit = _now_cache.get(key)
        if hit and hit[0] == st.st_mtime:
            facts = hit[1]
        else:
            recs = tail_records(path)
            facts = codex_live_shape(recs) if key in codex else live_shape(recs)
            with _now_lock:
                _now_cache[key] = (st.st_mtime, facts)

        uid = codex_uid(codex[key]["id"]) if key in codex else uid_of(path)
        row = db.execute(
            "SELECT uid,id,title,ai_title,custom_title,project,cwd,branches,n_user,source "
            "FROM sessions WHERE uid=?", (uid,)).fetchone()
        r = dict(row) if row else {}
        branches = json.loads(r.get("branches") or "[]")
        out.append({
            "uid": uid, "id": r.get("id") or path.stem,
            "title": (titles.get(uid) or r.get("title")
                      or (r.get("first_prompt") or path.stem)[:60]),
            "project": project, "label": pretty_project(project, r.get("cwd")),
            "cwd": r.get("cwd"), "branch": branches[0] if branches else None,
            "indexed": bool(row), "age": int(age), "bytes": st.st_size,
            "n_user": r.get("n_user"), "source": "codex" if key in codex else "claude",
            "resume": resume_command(r) if row else None,
            **live_state(facts, age),
        })
    order = {"blocked": 0, "working": 1, "waiting": 2, "idle": 3}
    out.sort(key=lambda x: (order.get(x["state"], 9), x["age"]))
    return {"now": out, "at": now}


# ----------------------------------------------------------------- live poll

_live_lock = threading.Lock()
_live_cache: tuple[float, str] = (0.0, "")


def projects_signature(max_age: float = 1.5) -> str:
    """`<files>:<total bytes>:<newest mtime>` over every transcript on disk.

    One stat() per file, and the answer is cached for a moment, so five open
    tabs asking at once cost one walk rather than five.
    """
    global _live_cache
    with _live_lock:
        when, sig = _live_cache
        if sig and time.time() - when < max_age:
            return sig
    n, total, newest = 0, 0, 0.0
    paths = [f[0] for f in transcript_files()] if PROJECTS_DIR.is_dir() else []
    paths += [Path(p) for p in codex_threads()]
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        n += 1
        total += st.st_size
        newest = max(newest, st.st_mtime)
    sig = f"{n}:{total}:{newest:.3f}"
    with _live_lock:
        _live_cache = (time.time(), sig)
    return sig


def indexed_signature(db) -> str:
    """The same signature, for the files the index has actually read."""
    r = db.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) b, COALESCE(MAX(mtime),0) m "
                   "FROM files").fetchone()
    return f"{r['n']}:{r['b']}:{r['m']:.3f}"


def api_live(db, qs: dict) -> dict:
    """Has anything moved? Cheap enough to ask every couple of seconds.

    `stale` means the transcripts on disk have grown past what the index holds,
    so the page knows to re-read without keeping any baseline of its own. With
    `id` (or `sub`) it also stats that one transcript, so an open chat can tail
    the conversation you are having right now. Nothing is parsed here.
    """
    disk, indexed = projects_signature(), indexed_signature(db)
    out: dict = {"sig": disk, "indexed": indexed, "stale": disk != indexed, "now": time.time()}
    sid, sub = qs.get("id"), qs.get("sub")
    if sid or sub:
        row = (db.execute("SELECT path FROM subagents WHERE path=?", (sub,)).fetchone() if sub
               else db.execute("SELECT path FROM sessions WHERE uid=?", (sid,)).fetchone())
        st = None
        if row:
            try:
                st = Path(row["path"]).stat()
            except OSError:
                st = None
        out["file"] = {"size": st.st_size, "mtime": st.st_mtime} if st else {"gone": True}
    return out


# ------------------------------------------------------------------ resuming

# One secret per server process. The page gets it inlined; /api/resume refuses
# anything without it. Together with the Origin check and the custom header
# (which a cross-origin form post cannot set) this keeps a random web page you
# happen to have open from launching commands through this server.
TOKEN = secrets.token_urlsafe(24)

TERMINAL = "Terminal.app"


def pick_terminal() -> str:
    return "terminal"


def open_terminal(cmd: str) -> str:
    """Open a new Terminal.app window running `cmd`, front and focused."""
    if sys.platform != "darwin":
        raise RuntimeError("launching a terminal is only wired up for macOS")
    # AppleScript string literal: backslashes and quotes need escaping
    esc = cmd.replace("\\", "\\\\").replace('"', '\\"')
    # `set index … to 1` is what actually raises the NEW window; activating Terminal
    # alone just brings whatever window was already frontmost forward, which looks
    # like "it opened the terminal but not my chat".
    script = (
        'tell application "Terminal"\n'
        'launch\n'
        f'set t to do script "{esc}"\n'
        'set index of (first window whose tabs contains t) to 1\n'
        'activate\n'
        'end tell'
    )
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=20)
    return "terminal"


def resume_command(row) -> str:
    cwd = row["cwd"] or pretty_project(row["project"], None)
    agent = "codex resume" if row["source"] == "codex" else "claude --resume"
    return f"cd {shlex.quote(cwd)} && {agent} {shlex.quote(row['id'])}"


def resume_target(row) -> tuple[str, str | None]:
    """(command, problem). A chat whose directory is gone cannot be resumed there."""
    cwd = row["cwd"] or pretty_project(row["project"], None)
    if not Path(cwd).is_dir():
        return resume_command(row), f"the chat's directory no longer exists: {cwd}"
    return resume_command(row), None


# ---------------------------------------------------------------------- server

_index_lock = threading.Lock()


def reindex(only: str | None = None) -> dict:
    """Serialised so the watcher thread and the Refresh button can't collide."""
    with _index_lock:
        return index(verbose=False, only=only)


def watch(interval: int):
    """Re-scan on a timer so new chats (and new models) show up without a click."""
    while True:
        time.sleep(interval)
        try:
            reindex()
        except RuntimeError as exc:
            if "newer than this build" in str(exc):
                # a newer cchat.py owns this index now; stop before we damage it
                print(f"watch: {exc}\nwatch: this server is out of date - exiting.",
                      file=sys.stderr, flush=True)
                os._exit(0)
            print(f"watch: {exc}", file=sys.stderr, flush=True)
        except Exception as exc:                      # never kill the server over this
            print(f"watch: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


_local = threading.local()


def thread_db() -> sqlite3.Connection:
    """sqlite connections are not thread-safe - give each server thread its own."""
    if getattr(_local, "db", None) is None:
        _local.db = connect()
    return _local.db


class Handler(BaseHTTPRequestHandler):
    server_version = "cchat"

    @property
    def db(self) -> sqlite3.Connection:
        return thread_db()

    def log_message(self, fmt, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

    def trusted(self) -> bool:
        """Only our own page, with the token, may run commands."""
        if self.headers.get("X-CChat-Token") != TOKEN:
            return False
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        return origin in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path not in ("/api/resume", "/api/pin", "/api/deprecate", "/api/delete",
                              "/api/rename", "/api/mark", "/api/handoff/launch"):
                return self._send(404, b"not found", "text/plain")
            if not self.trusted():
                return self.json({"error": "not allowed"}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or "{}")
            if u.path == "/api/rename":
                if not self.db.execute("SELECT 1 FROM sessions WHERE uid=?",
                                       (body.get("uid"),)).fetchone():
                    return self.json({"error": "unknown chat"}, 404)
                return self.json(set_title(self.db, body["uid"], body.get("title")))
            if u.path == "/api/deprecate":
                if not self.db.execute("SELECT 1 FROM sessions WHERE uid=?",
                                       (body.get("uid"),)).fetchone():
                    return self.json({"error": "unknown chat"}, 404)
                return self.json(set_deprecated(self.db, body["uid"], bool(body.get("on"))))
            if u.path == "/api/delete":
                # deleting requires the chat to be deprecated first, and an explicit
                # confirm flag - two deliberate steps before anything moves on disk
                uid = body.get("uid")
                if not body.get("confirm"):
                    return self.json({"error": "confirm flag required"}, 400)
                if uid not in deprecated_set(self.db):
                    return self.json({"error": "deprecate the chat first"}, 409)
                out = delete_chat(self.db, uid)
                return self.json(out, 200 if out.get("ok") else 400)
            if u.path == "/api/mark":
                if not self.db.execute("SELECT 1 FROM sessions WHERE uid=?",
                                       (body.get("uid"),)).fetchone():
                    return self.json({"error": "unknown chat"}, 404)
                return self.json(set_mark(self.db, body["uid"], int(body.get("line") or 0),
                                          bool(body.get("on")), body.get("who") or "",
                                          body.get("label") or ""))
            if u.path == "/api/handoff/launch":
                try:
                    out = launch_handoff(self.db, body.get("uid"))
                except Exception as exc:
                    return self.json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                return self.json(out, 200 if out.get("ok") else 400)
            if u.path == "/api/pin":
                if not self.db.execute("SELECT 1 FROM sessions WHERE uid=?",
                                       (body.get("uid"),)).fetchone():
                    return self.json({"error": "unknown chat"}, 404)
                return self.json(set_pin(self.db, body["uid"], bool(body.get("on"))))
            row = self.db.execute("SELECT * FROM sessions WHERE uid=?", (body.get("uid"),)).fetchone()
            if not row:
                return self.json({"error": "unknown chat"}, 404)
            cmd, problem = resume_target(row)
            if problem:
                return self.json({"error": problem, "command": cmd}, 409)
            try:
                open_terminal(cmd)
            except Exception as exc:
                return self.json({"error": f"{type(exc).__name__}: {exc}", "command": cmd}, 500)
            return self.json({"ok": True, "terminal": "Terminal", "command": cmd})
        except BrokenPipeError:
            pass
        except Exception as exc:
            return self.json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        p = u.path
        try:
            if p in ("/", "/index.html"):
                html = (HERE / "app.html").read_text(encoding="utf-8")
                html = html.replace("__CCHAT_TOKEN__", TOKEN).replace(
                    "__CCHAT_TERMINAL__", pick_terminal())
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if p == "/api/meta":
                return self.json(api_meta(self.db))
            if p == "/api/activity":
                return self.json(api_activity(self.db, qs.get("project")))
            if p == "/api/usage":
                u = api_usage(self.db, qs.get("project"))
                return self.json({"usage": u, "unpriced": unpriced_models(u)})
            if p == "/api/deprecated":
                return self.json(api_deprecated(self.db))
            if p == "/api/dashboard":
                return self.json(api_dashboard(self.db, qs.get("project")))
            if p == "/api/overview":
                return self.json(api_overview(self.db, qs["project"]) if qs.get("project") else {})
            if p == "/api/pickup":
                return self.json({"pickup": api_pickup(self.db, qs.get("project"))})
            if p == "/api/files":
                return self.json(api_files(self.db, qs))
            if p == "/api/method":
                return self.json({"method": api_method(self.db, qs.get("project"))})
            if p == "/api/sessions":
                return self.json(api_sessions(self.db, qs))
            if p == "/api/facets":
                return self.json(api_facets(self.db, qs))
            if p == "/api/transcript":
                return self.json(api_transcript(self.db, qs))
            if p == "/api/live":
                return self.json(api_live(self.db, qs))
            if p == "/api/now":
                return self.json(api_now(self.db, qs))
            if p == "/api/recaps":
                return self.json(api_recaps(self.db, qs))
            if p == "/api/commits":
                return self.json(api_commits(self.db, qs))
            if p == "/api/marks":
                return self.json(api_marks(self.db, qs))
            if p == "/api/handoff":
                # a file has no paste limit, so a download is the whole chat
                whole = bool(qs.get("download"))
                h = handoff_markdown(self.db, qs.get("uid") or "",
                                     None if whole else HANDOFF_BUDGET,
                                     None if whole else HANDOFF_MSG_CAP)
                if h.get("error"):
                    return self.json({"error": h["error"]}, 404)
                h.pop("row")
                if qs.get("download"):
                    body = h["markdown"].encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{handoff_filename(h)}"')
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                return self.json(h)
            if p == "/api/reindex":
                if qs.get("full"):
                    with _index_lock:
                        return self.json(index(full=True, verbose=False))
                if qs.get("uid"):
                    # one chat, so the page you are watching can keep its title,
                    # its counts and its turn map current for the price of one file
                    row = self.db.execute("SELECT path FROM sessions WHERE uid=?",
                                          (qs["uid"],)).fetchone()
                    if not row:
                        return self.json({"error": "unknown chat"}, 404)
                    return self.json(reindex(only=row["path"]))
                return self.json(reindex())
            return self._send(404, b"not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return self.json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def serve(port: int, open_browser: bool = True, watch_every: int = 90):
    if not DB_PATH.exists():
        print("no index yet - building one first…")
        index()
    n = connect().execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
    if n == 0:
        print("index is empty - run `python3 cchat.py index`", file=sys.stderr)

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise SystemExit(
            f"port {port} is already in use ({exc}).\n"
            f"Another cchat is running - stop it, or pass --port to use a different one."
        ) from None
    httpd.daemon_threads = True
    if watch_every:
        threading.Thread(target=watch, args=(watch_every,), daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"cchat serving {n} chats at {url}   (ctrl-c to stop)")
    print("resume opens a new Terminal.app window")
    print(f"watching for new chats every {watch_every}s" if watch_every else "watching disabled")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    pi = sub.add_parser("index", help="build or refresh the index")
    pi.add_argument("--full", action="store_true", help="rebuild from scratch")
    ps = sub.add_parser("serve", help="run the web UI")
    ps.add_argument("--port", type=int, default=7842)
    ps.add_argument("--no-open", action="store_true")
    ps.add_argument("--reindex", action="store_true", help="refresh the index first")
    ps.add_argument("--watch", type=int, default=90, metavar="SEC",
                    help="rescan for new chats every SEC seconds (0 disables)")
    sub.add_parser("stats", help="print a summary")
    ph = sub.add_parser("handoff", help="print a chat as markdown for another agent")
    ph.add_argument("chat", help="session id (or its first few characters)")
    pm = sub.add_parser("marks", help="your pins, renames and deprecations")
    pm.add_argument("--restore", action="store_true",
                    help="put back anything in marks.json that the index is missing")
    pm.add_argument("--from", dest="src", metavar="FILE",
                    help="restore from a particular copy (e.g. marks.prev.json)")
    args = ap.parse_args()

    if args.cmd == "marks":
        db = connect(migrate=True)
        if args.restore or args.src:
            out = restore_marks(db, Path(args.src) if args.src else None)
            if out.get("error"):
                raise SystemExit(out["error"])
            put = {k: v for k, v in out["restored"].items() if v}
            print(f"restored from {out['from']}: "
                  + (", ".join(f"{v} {k}" for k, v in put.items()) if put
                     else "nothing was missing"))
        else:
            for t in USER_TABLES:
                print(f"{db.execute(f'SELECT COUNT(*) c FROM {t}').fetchone()['c']:5d}  {t}")
            print(f"\ncopy: {MARKS_PATH}"
                  + ("" if MARKS_PATH.exists() else "  (not written yet)"))
            prev = MARKS_PATH.with_suffix(".prev.json")
            if prev.exists():
                print(f"one back: {prev}  — restore it with "
                      f"`marks --from {prev.name}`")
    elif args.cmd == "index":
        index(full=args.full)
    elif args.cmd == "handoff":
        db = connect()
        rows = db.execute("SELECT uid FROM sessions WHERE id LIKE ? OR uid=? "
                          "ORDER BY ended DESC", (args.chat + "%", args.chat)).fetchall()
        if not rows:
            raise SystemExit(f"no chat matches {args.chat!r}")
        h = handoff_markdown(db, rows[0]["uid"])
        if h.get("error"):
            raise SystemExit(h["error"])
        sys.stdout.write(h["markdown"])
    elif args.cmd == "stats":
        db = connect()
        m = api_meta(db)
        t = m["totals"]
        print(f"{t['sessions']} chats across {len(m['projects'])} projects, "
              f"{t['prompts']} prompts, {t['bytes']/1e9:.2f} GB on disk")
        for p in m["projects"][:40]:
            print(f"  {p['n_sessions']:>4}  {str(p['last'])[:10]}  {p['label']}")
        print("\nper model (API-equivalent list price, not what a subscription bills):")
        for u in m["usage"]:
            cost = f"${u['cost']:,.2f}" if u["priced"] else "     n/a"
            print(f"  {u['model']:<20} {u['msgs']:>6} msgs  "
                  f"in {u['billed_in']/1e6:>8.1f}M  out {u['out_tok']/1e6:>6.2f}M  {cost:>12}")
    else:
        if getattr(args, "reindex", False):
            index()
        serve(args.port if args.cmd == "serve" else 7842,
              not getattr(args, "no_open", False),
              getattr(args, "watch", 90))


if __name__ == "__main__":
    main()
