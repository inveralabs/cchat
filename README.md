# cchat — a browser for your Claude Code and Codex history

Claude Code keeps every conversation as JSONL under `~/.claude/projects/`, and Codex
keeps its own under `~/.codex/`. That is great for resuming and useless for finding
anything. `cchat` indexes both and gives you one page: projects on the left, a chat
list you can filter by agent, model and time, and full-text search across every message
you have exchanged with either agent.

No dependencies — Python 3 standard library only. Nothing is uploaded anywhere; the
server binds to `127.0.0.1` and the transcripts are only ever read.

## Use it

```sh
cd ~/code/cchat
python3 cchat.py index      # build the index (incremental after the first run)
python3 cchat.py serve      # opens http://127.0.0.1:7842
```

### One command from anywhere

A `hist` shortcut is installed via one line in `~/.zshrc` that sources
`shell-init.zsh` from this directory:

```sh
hist        # index, then serve, then it's at http://127.0.0.1:7842
his         # same
chats       # same
cchat       # the underlying function
```

It re-indexes incrementally first (a fraction of a second) and only falls back to
`index --full` if the schema moved on — the one case that needs it. If a server is
already listening it opens the browser instead of failing on the busy port.
`CCHAT_PORT` and `CCHAT_HOME` override the defaults.

**It deliberately does not touch `history`.** That is a zsh builtin, and Oh My Zsh
aliases it to `omz_history`; shadowing it would break listing your shell commands.
To remove the shortcut, delete the `source` line from `~/.zshrc`
(a backup of the original is at `~/.zshrc.bak-cchat`).

`python3 cchat.py stats` prints a per-project summary in the terminal.

**Your marks are backed up.** Pins, message pins, renames and deprecations are the only
things in the index that cannot be rebuilt from the transcripts — they exist nowhere
else. Every change writes a copy to `marks.json` beside the index, and the previous copy
is kept as `marks.prev.json` whenever the new one has fewer rows, which is what an
accidental wipe looks like from here. `python3 cchat.py marks` shows what you have and
where the copy is; `marks --restore` puts back anything the index is missing, and never
removes anything. `CCHAT_MARKS` moves the file.

**It follows the chat you are having.** Open a chat here and leave it open: the page
watches the file on disk, and each new prompt, tool call and reply appears in the
transcript within about two seconds. No reload, no ↻. A chat that is still being
written opens at its end, where the answer you are waiting for lands.

- The **Live** button in the chat header turns following off and on. The choice sticks.
- Scrolled up, reading? Nothing moves. A **↓ new messages** button appears instead,
  and takes you to the end when you want it.
- A tool result that arrives after its call still folds into the same block, and the
  blocks you expanded stay expanded.
- New chats — and new turns in chats you are not reading — reach the list on their own.

A rename in the terminal reaches the app the same way. `/rename` writes a
`custom-title` record into the transcript; the open chat re-reads its own row every
15 seconds or so and the header changes under you, Find box and all. A title you set
*here* still wins — the row keeps a dot, and its tooltip says what the transcript
calls it.

**It keeps itself current.** The page asks the server twice a second-ish whether the
files have moved past what the index holds, and re-indexes when they have; `serve`
also rescans every 90 seconds (`--watch SEC`, `0` to disable). Both are a `stat()` per
file, so they cost nothing. The ↻ button still forces a full rescan, and lights up when
there is something new but the page did not want to pull the list out from under you.
Nothing is polled while the tab is in the background.

## Codex chats, in the same place

Codex chats sit in the same list as Claude chats, with a small **codex** badge. A
project holds the work of both agents: a folder you used with Claude and with Codex is
one project, not two. The **Filters** panel above the list picks which chats you see.

A Codex chat opens like a Claude chat, with the same transcript, Find, copy, pins,
turn map, files, commits and search. **Resume** runs `codex resume <id>`. **Share →
Continue in Claude Code** hands a Codex chat to Claude, the same way a Claude chat goes
to Codex. The running-now board shows Codex sessions too, and notifications name the
agent.

**Where it reads from.** Codex keeps its chats in three layers:

| Layer | What it holds | cchat |
|---|---|---|
| `sessions/**/rollout-*.jsonl` | The source of truth: raw model input and output. A shell command is a JavaScript string for a generic `exec` tool. | Token counts only, plus a `stat()` to see when a chat moved |
| `state_N.sqlite` → `threads` | One row per chat: folder, name, model, branch, archived | The chat list |
| `thread_history_N.sqlite` → `thread_items` | Typed items Codex decoded itself: messages, commands with output, file changes with diffs, MCP calls, web searches | The transcripts |

Both databases are opened read-only (`mode=ro`). cchat never writes to anything under
`~/.codex/`. It does not use `immutable=1`, because Codex writes these files live
through a WAL and that mode would miss the newest turns. `CODEX_HOME` moves the folder.

**When Codex changes its layout.** The `_N` in the database names moves when Codex
changes its schema. cchat takes the highest number it finds and checks for the columns
it needs (`CODEX_NEED` in `cchat.py`). If a column is missing, Codex chats disappear
from the list, and the page says why in a red notice. Wrong data never shows. Claude
chats are not affected.

**What is different about a Codex chat:**

- **Delete** is refused. Codex's databases point at the rollout file, so moving it
  would break Codex. Deprecate works as usual. To really delete a Codex chat, run
  `codex delete <id>`.
- **Cost** reads `n/a` for GPT models until you add them to `PRICES`. Codex counts
  cached tokens inside its input count; cchat splits them out to match Claude's columns.
- **How each model works** leaves Codex out. Its tools (`shell`, `apply_patch`) do
  not line up with Claude's Read, Edit and Bash, so the columns would compare
  different things.
- **Recaps and open tasks** do not exist in Codex. A memory reset is marked, but Codex
  does not record how many tokens it dropped.
- **Subagent threads** Codex spawns are left out of the list.
- **Archived Codex chats** are listed, and the header says "archived in Codex". Pins
  and renames survive archiving: a Codex chat is keyed by its thread id, not by the
  file path Codex moves when it archives.

## Four things the transcripts already knew

Claude Code writes more into its logs than a conversation. cchat now reads the rest
of it.

**Running now.** A board at the top of the page of every chat that has moved lately,
and what it is doing: *working* (the file is still being written), *needs you* (a tool
call has no result and nothing is moving — it is sitting on a permission prompt),
*your turn* (it answered and stopped). Each row carries the last thing it said or ran,
a clock, and Resume. Turn on 🔔 **Notify me** and your browser tells you the moment a
session stops and waits for you. Written for the case where three sessions are running
and the only question is which one needs a human.

**Recaps.** When you walk away from a chat, Claude writes a one-line recap of what it
did and what comes next. Those lines were sitting unread in the transcripts — 657 of
them here. The Recaps tab is the feed: newest first, by day, each one a click away from
the point in the chat where it was written. The same tab holds every memory reset and
every failed API call.

**Memory resets.** A chat that fills its context gets compacted, and everything before
that point is gone from what it remembers. The transcript records exactly where, and
what it cost. cchat marks it in the chat — *"167k tokens in, 13.6k kept, 153.5k
dropped — it took 186s"* — which explains a whole class of "why did it forget that".

**Commits from this chat.** A chat knows which files it edited and when it ran; git
knows which commits touched which files and when they landed. Overlap the two and a
conversation points at the code it produced. The chat header lists them with the
evidence attached — *"28 of its 64 files are ones this chat edited"* — because a match
on a file name alone is weaker than a match on a whole path, and you should see which
one you have. Click a commit to copy its sha.

Time is measured, not guessed. Claude Code times every turn and writes the number down;
the method table's **Time a turn** column and each chat's *"of model time"* are those
numbers, not an estimate from timestamps.

Nothing about models is hardcoded into the *views* — models, tools, projects and
per-tool loops are all discovered from the transcripts, so a model released tomorrow
shows up as its own row with no code change. Three lists in `cchat.py` do need a human
when the world moves, and each fails loudly rather than silently:

| List | What it affects | If it's missing an entry |
|---|---|---|
| `PRICES` | cost column | Model shows `n/a` and the card names it: *"No price on file for X"* |
| `IDLE_GAP` | "time spent" | A different pause threshold changes what counts as working time |
| `VERIFY_RE` | the Verifies column | A new test runner isn't recognised, so Verifies reads low |
| `READ_KIND` / `EDIT_KIND` / `DELEGATE_KIND` | Reads→edit, Delegates | A new tool isn't counted in those columns |

Editing any of them changes how past turns are classified, so re-run
`python3 cchat.py index --full` afterwards — an incremental pass skips unchanged files
and would leave the old values in place.

## What you get

- **Search everything** — every prompt and every reply, ranked, with highlighted
  snippets. Click a snippet to open the transcript at that exact message.
  `/` or `⌘K` focuses the box.
- **Projects sidebar** — chat count, last activity and size per project. Worktrees
  show as `repo ⑂ branch`.
- **Filters** — a panel above the chat list, with three rows:
  - **Agent**: all chats, only Claude Code, or only Codex (GPT).
  - **Time**: today, the last 7, 30 or 90 days, the last year, or a custom range of
    dates. A chat counts if any part of it falls inside the window.
  - **Model**: one chip per model, grouped by agent. Pick one or more; a chat shows if
    it used any of them. Chips read "Opus 5.5" and "GPT-6 Astra"; hover for the raw id.

  Each choice shows how many chats it would give you, with every other filter and your
  search applied. A choice that would give none is dimmed. The line at the top says what
  is in force, and **Clear all** resets it. The page remembers your filters in this
  browser.
- **Chat list** — title (Claude's own, or yours if you renamed it), first prompt,
  prompt/reply/tool counts, duration, git branch, models used, size on disk.
  Sort by recency, length, or size.
- **Resume in one click** — every chat row has a `▶ Resume` button, and the
  transcript has a big one. It opens a new terminal window already running
  `cd <cwd> && claude --resume <id>`. No copy-pasting.
- **Transcript view** — each tool call is its own block, tagged by what it is:
  `shell`, `read`, `edit`, `web`, `agent` (with the subagent type), `mcp` (server ·
  tool), `task`, `skill`. The call and its output live in the same collapsible block,
  so the page reads as prose plus discrete actions instead of alternating
  "claude / you / claude / you". Markdown tables render as real tables. A **Find in
  this chat** box filters to matching messages. Subagent logs (including workflow
  fleets) are listed and openable.
- **Copy anything out of a chat** — hover a message and it offers `copy` (the message as
  markdown), `quote` (the words only, each line prefixed with `>`) and `link` (a URL that
  reopens the chat at that message). Every code block, tool payload and output pane has its
  own ⧉ copy button. Highlight any part of a message and a small bar follows the selection:
  **Copy**, **Quote**, **In this chat** (filter to messages containing it) and **All chats**
  (search everything for it), with a character and word count. `⧉ Copy chat` takes the whole
  transcript as markdown, the conversation without tool calls, only what Find left on screen,
  only your prompts, or the raw records as JSON — each with a header naming the chat, its
  directory and its source file. A copy of a partly loaded chat says so.
- **Share a chat with another agent** — `↗ Share` in the chat header hands the chat to
  Codex, ChatGPT or anything else that reads markdown. The server builds a *handoff*
  from the whole transcript, not the loaded page: a short brief for the receiver, the
  directory, branch and models, the files it changed, any unfinished tasks, then every
  prompt and reply in full with one line per tool call. Tool output is left out unless
  the call failed; thinking and subagent chatter stay behind. A chat bigger than ~30k
  tokens keeps its opening request and as much of the end as fits, and says what it cut.
  - **Continue in Codex** writes the handoff to `~/.claude/cchat-handoffs/` and opens
    Terminal in the chat's directory running `codex` with a prompt that points at the
    file. On a Codex chat the same button reads **Continue in Claude Code** and runs
    `claude` instead. The file holds the whole chat, like a download. Codex reports where the work
    stands and waits for you before it changes anything. Same token guard as Resume; `CCHAT_HANDOFFS` moves the folder.
  - **Open in ChatGPT** copies the handoff and opens chatgpt.com. Paste with ⌘V.
  - **Copy handoff** for everything else.
  - **Download handoff** saves the whole chat as a `.md` file. Nothing is cut: no
    message is shortened and none is left out, because a file has no paste limit. Copy
    and ChatGPT shorten a message past 6,000 characters and keep to the ~30k budget.
  - From the terminal: `python3 cchat.py handoff <session-id> | pbcopy` (the first few
    characters of the id are enough).
- **Pin the messages you keep coming back to** — the decision, the command that worked,
  the error you will hit again. Hover a message and press **pin** (or `p` for whatever you
  are reading); it becomes a chip at the top of the chat that takes you straight back,
  even when that part of the transcript is not loaded. `shift+P` walks the pins in order,
  so a 200-turn chat gets a table of contents. Every pin also lands on the Dashboard under
  **Pinned messages**, so you can jump into the middle of a chat from outside it. Pins are
  yours, like renames and chat pins: an index rebuild never touches them.
- **Expand all** — one button opens (or closes) every tool call and thinking block in the
  chat, for reading or for a plain-text sweep with ⌘F.
- **Search covers tool calls, not just chat text** — Bash commands, file paths, MCP
  query bodies and the code an edit introduced are all indexed, so "which chat ran the
  query with `block_month`" is answerable (14 chats here). Hits are labelled `tool`.
- **Files** — a query that matches paths shows a Files strip above the results; click a
  file to see every chat that touched it. Each chat lists the files it read and edited.
- **Threads** — chats that worked on the same files are grouped automatically, so a
  project reads as a few efforts instead of a pile of chats. Click the badge to see the
  rest of a thread.
- **Open work** — the last task reminder in a chat is kept, so chats with unfinished
  items carry an "N open" badge and list them when you open them.
- **Resume-cost hint** — a chat whose final prompt was over 200k tokens says so, and
  offers a fresh start in the same directory instead.
- **Dashboard tab** (`#dash`) — pinned chats grouped under their project name and laid
  out across the width, plus three lenses: **recently worked on**, **most prompts**,
  **most time spent**. Every row there carries ☆ (pin) and ▶ (resume).
  The sidebar scopes it: pick a project and the dashboard shows that project's name,
  path, chats, prompts, time, cost and errors, with its own lenses. Bookmarkable as
  `#dash&p=<project>`; "Browse these chats →" carries the scope into the list view.
- **Turn strips** — one tick per turn on every row: height is how tool-heavy the turn
  was, red is a failed tool, amber is a turn you interrupted. You read a chat's shape
  before opening it. Long chats compress to 56 ticks, keeping the worst of each bucket.
- **Turn map inside a chat** — the same bar, full width and uncompressed, at the top of
  the transcript. Every tick is a button that jumps to that turn (loading the right page
  first if it isn't on screen), and hovering names the turn: `Turn 42 — 3 failed tools
  · 1,204 words`. It is the outline rail a 2,600-line chat needs.
- **Project overview** — selecting a project shows its state: chats, prompts, tool
  errors, cost, threads, most-worked files, and what's unfinished — not just a filter.
- **Deprecated tab** (`#dep`) — chats you have set aside with ⊘, grouped by project in the
  same compact grid. They disappear from the chat list, the dashboard, its lenses and
  search, and the tab shows how many and how much disk they hold. ↩ restores one.
- **Deleting** (Claude chats only — see *Codex chats* above) is deliberately two-step: a chat must be deprecated first, then Delete asks
  for a second click. Even then the transcript and its subagent logs are **moved** to
  `~/.claude/cchat-trash/<timestamp>/<project>/`, never unlinked — restoring is one `mv`
  plus a re-index. Override the location with `CCHAT_TRASH`.
- **Rename any chat** — ✎ on a row, in a chat, or on a dashboard card turns the title
  into an input; Enter saves, Escape cancels, and clearing it reverts to whatever the
  transcript called it. Renamed titles carry a small ✎ and keep the original on hover.
  Stored in the index, so it survives re-indexing and never touches your transcripts.
- **Keyboard** — `/` or `⌘K` search, `j`/`k` (or ↑/↓) move, `↵` open, `r` resume the
  selected chat, `Esc` close. Inside a chat: `c` copies the message you are looking at,
  `C` copies the whole chat, `a` expands or collapses every block, `f` jumps to Find,
  `p` pins the message you are reading, `shift+P` walks the pins.
- **Deep links** — `#c=<uid>` opens a specific chat, so you can bookmark one. Add
  `&l=<line>` to land on one message — that is what the `link` button copies.
- **Two edges you can drag** — the projects list, and the left edge of an open chat.
  Project names are long, diffs are wide, and no two screens are the same. Double-click
  an edge to put it back; with an edge focused, ← and → resize it (hold shift for bigger
  steps) and Home resets it. Both widths are remembered, and neither is allowed to
  squeeze the other side of the window away.
- Light and dark, following your OS; the ◐ button overrides it.

## The Resume button

It opens **Terminal.app** — a new window, brought to the front, already running the
resume command. macOS only; elsewhere the button reports an error and copies the
command instead.

Two things that make it actually work rather than *look* like it works:

- **The new window has to be raised explicitly.** `activate` alone just brings
  whatever Terminal window was already frontmost forward, so the resumed chat opens
  behind it and the button appears to "only open the terminal". `set index of … to 1`
  on the new window is what fixes it.
- **A chat whose directory has been deleted can't be resumed.** The server checks
  before launching and returns an error instead of opening a window where `cd` fails
  and nothing happens. Those chats show a dashed Resume button with the reason on
  hover.

**Why an endpoint that runs commands is safe here.** `POST /api/resume` takes only a
chat id — never a command — and resolves the directory and session id from the index
itself. It requires an `X-CChat-Token` header holding a secret minted fresh at every
server start and inlined into the page, which a cross-origin page cannot read or set,
and it rejects any request whose `Origin` is not this server. The server binds to
`127.0.0.1`. If you leave a tab open across a restart, the button will tell you to
reload the page — that is the token rotating, working as intended.

## Usage by model — what the numbers mean

The Browse page no longer shows the Activity heatmap, the stat tiles, *Usage by model* or
*How each model works*. The index still records all of it. `python3 cchat.py stats`
prints the per-model usage, and `/api/usage` and `/api/method` still serve both.

Every assistant message records its own token usage, so the per-model totals are
measured, not estimated. They include subagent logs, which is where a surprising
share of the spend lives (adding them moved sonnet-5 from 484 to 2,547 responses here).

**Cost is an API-list-price valuation, not a bill.** A Claude subscription does not
charge per token, so the dollar figure tells you how large the workload was, not what
you paid. Rates are the published per-million prices in the `PRICES` table at the top
of `cchat.py` — edit them if yours differ. Cache writes bill at 1.25× input for the
5-minute TTL and 2× for the 1-hour TTL; cache reads at 0.1×. The transcripts record
the 1h/5m split per message (`usage.cache_creation`), so the two write rates are
applied separately rather than averaged. A model with no entry in `PRICES` shows `n/a`
rather than a guess.

The breakdown is usually the interesting part. On this history, fresh input is a
rounding error and **cache reads are 3.6 billion tokens** — half the total valuation —
with 1-hour cache writes most of the rest.

## How each model works

One row per human prompt is recorded: which model answered, the ordered list of tools
it called, whether a thinking block came before the first tool, and what it did after
its first edit. Aggregated per model — and per project, which matters more than it
sounds.

**Compare within a project, not globally.** Across the whole history the models were
given different work, so the global table mixes temperament with task mix. Scoped to
one project the comparison is fair, and the differences get sharper: in
`zohopay_reconciliation`, fable-5 ran a test/build after **91%** of its edits while
sonnet-4-6 and sonnet-5 did so after **6%** — same codebase, same kind of task.

Column definitions, so the numbers aren't over-read:

| Column | Means |
|---|---|
| Prompts | Turns that started with something you typed (slash commands and interrupts excluded) |
| Tools per turn | Median tool calls between your prompt and the reply |
| Thinks first | Share of turns with a thinking block before the first tool call |
| Reads before 1st edit | Median Read/Grep/Glob calls preceding the first Edit/Write, over turns that edited |
| Delegates | Share of turns that spawned an Agent/Task/Workflow |
| Verifies | Of turns that edited, the share that then ran a test/build/lint command |
| Typical loop | Most frequent consecutive tool pairs |

Every assistant message in these transcripts carries exactly one tool call — across
1,335 turns there is not a single parallel batch — so there is deliberately no
"parallelism" column; it would read 1.0 forever.

## Why there is no "unfinished tasks" list

There was one, briefly. It surfaced chats whose last task reminder still had open items,
quietest first. Checked against real history, **every item it showed was already dead**:
for each one, most of the files that chat touched had been modified *after* it ended, and
several later chats had worked on the same files. Sorting by "quietest" actively promoted
the most stale entry to the top.

The mistake was conceptual. An unfinished task list at the end of a chat is a snapshot of
intent at the moment you walked away, not a to-do list — the work usually continues in a
later chat or straight in the editor, and nobody goes back to tick the box. "Has open
items" survives only as a small badge on a row; the dashboard ranks by **recency, prompt
count and time spent**, which are true by construction.

**Time spent** is real working time, not wall-clock: consecutive message timestamps are
summed and any gap over 30 minutes (`IDLE_GAP`) is treated as you walking away. A chat
spanning three weeks reports the hours actually worked in it.

## What "deprecated" hides

Everything. The browse list and its search, the dashboard's pinned section and lenses, a
project's overview counts, *Usage by model* and *How each model works* — a deprecated chat
is absent from all of them, so every number in the app agrees with every other.

I tried the other way first, keeping deprecated chats in the cost and method analytics on
the grounds that the work really did happen. Side by side in one tile row that reads as a
bug: "87 chats · 284 tool errors · $8,824" where the first two exclude the set-aside chats
and the third includes them. Consistency won.

Nothing is lost — the rows stay in the index, and restoring with ↩ brings the chat and its
numbers straight back. Pinning and deprecating are mutually exclusive; setting one clears
the other.

## Your own state vs derived state

Three tables hold things only you know: `pins`, `deprecated` and `titles`. They are
excluded from the schema-migration drop and from `index --full`'s wipe, so rebuilding the
index from scratch keeps your pins, hidden chats and renames. Everything else in the
database is derived from the transcripts and is safe to throw away at any time.

Renaming stores an override; the transcript-derived title is still returned as
`auto_title`, which is what a blank rename restores and what the ✎ tooltip shows. Search
still matches message text, not titles — renaming a chat does not make it findable by its
new name.

## Running more than one copy

`serve` refuses to start if the port is taken, and a server whose build is older than
the index now **exits** rather than fighting over it. That matters because the schema
is versioned: an old `serve` left running in the background will otherwise keep
migrating a newer index back down to its own shape on every rescan. If chats vanish
after an upgrade, look for a stale `cchat.py serve` process first.

## Three things that made the index destroy itself

Both were mine, both are fixed, and both are worth knowing if you change this code.

**1. Pruning inferred deletion from absence.** The incremental pass built a list of
files it saw and deleted every indexed chat that wasn't in it. One bad enumeration — a
read error, a race — and the whole index goes. It now deletes a chat only when its file
is genuinely missing from disk (`Path.exists()`), and refuses to prune at all if a scan
turns up no transcripts. Verified by pointing `CLAUDE_PROJECTS_DIR` at an empty
directory: all 92 chats survive.

**2. Migrations could run backwards.** `index --full` from an older build happily
dropped a newer schema and rebuilt its own. Downgrades are now refused outright —
`index.db is schema v6, newer than this build (v5)` — regardless of who asks.

**3. Readers were allowed to migrate the schema.** `connect()` dropped and recreated
every table on a `user_version` mismatch. A long-running `serve` from an older build
therefore fought a newer one over the same file, each wiping the other on a timer — the
auto-rescan turned that into a loop. Only the indexer may migrate now; a reader on a
mismatched schema raises `index.db is schema vN, this build expects vM — run index
--full` instead of silently rebuilding.

Indexing also checkpoints the WAL (`wal_checkpoint(TRUNCATE)`) so committed data lands
in `index.db` itself. Don't `rm index.db-wal` out from under a live connection.

## How it handles the awkward parts of the format

- **Huge lines.** Some transcripts are enormous (one here is 405 MB in 2,668 lines —
  103 of those lines are multi-MB tool payloads). Lines over 1 MB are counted but
  never parsed, which is why a full scan of ~590 MB takes seconds.
- **Duplicate session ids.** Resuming a session inside a git worktree writes a second
  file with the *same* session id under a different project directory. Rows are keyed
  by a hash of the file path, not the id, so neither copy is lost.
- **Command noise.** `<command-name>`, `<local-command-stdout>`, caveat blocks,
  `<system-reminder>` and ANSI escapes are stripped; a bare `/model` renders as a
  small chip instead of three lines of markup, and never becomes a chat's preview text.
- **Empty thinking blocks.** Recent transcripts keep only the signature of a thinking
  block, so those are skipped rather than shown as empty collapsibles.
- **Stub files.** Transcripts with no prompts and no replies are ignored.

Search covers message *text*. Tool inputs and outputs are shown in the transcript but
not indexed — that is what keeps the index at ~12 MB instead of ~600 MB.

## Files

| | |
|---|---|
| `cchat.py` | indexer (Claude transcripts and Codex's databases), SQLite/FTS5 store, and the local HTTP API |
| `app.html` | the whole UI, no build step |
| `index.db` | generated; delete it and re-run `index` to start over |

Environment overrides: `CLAUDE_PROJECTS_DIR` (default `~/.claude/projects`),
`CCHAT_DB` (default `index.db` next to the script).
