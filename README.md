# chat-memory

Search your past Claude Code conversations, locally.

Claude Code keeps every session's transcript on your machine, but finding something from last week means digging through large JSON files. chat-memory indexes the conversation text and gives Claude a skill to search it when you ask things like *"How did we fix the build last week?"* or *"What did we decide about the database?"*

- **Conversation text only.** Your messages and Claude's replies are indexed. Tool output, file contents, thinking and internal notices are left out.
- **Secrets are masked** before anything is stored: API keys, tokens, passwords in `key=value` form, bearer tokens, private keys and credentials in URLs.
- **Stays on your machine.** No network access, no hooks, no background process. The index is readable by your user only.
- **Nothing is injected.** Claude searches only when your question calls for it, and treats the results as information, not instructions.
- **No dependencies.** One Python file using the standard library (Python 3.8+ with SQLite FTS5, which standard builds include).

## Install

In Claude Code:

```
/plugin marketplace add Ein-nor/chat-memory
/plugin install chat-memory@chat-memory
```

On Claude Code 2.1.275 or later this also works in one step:

```
/plugin install chat-memory --marketplace Ein-nor/chat-memory
```

Start a new session afterwards. Then just ask about an earlier conversation.

**Without the plugin system:** copy the `skills/chat-memory` folder to `~/.claude/skills/chat-memory`.

## Using it yourself

The skill runs the script for you, but you can also use it directly:

```sh
python3 skills/chat-memory/chat_memory.py search docker compose        # all words, falls back to any
python3 skills/chat-memory/chat_memory.py search migration --since 2026-01-01
python3 skills/chat-memory/chat_memory.py show 3f2a91c0 --match compose  # read a session around a word
python3 skills/chat-memory/chat_memory.py sessions                      # recent sessions with titles
python3 skills/chat-memory/chat_memory.py forget 3f2a91c0               # drop a session from the index
python3 skills/chat-memory/chat_memory.py stats
python3 skills/chat-memory/chat_memory.py reindex                       # rebuild from scratch
```

The index updates itself on every command, so there is nothing to schedule.

## What it reads and writes

| | |
|---|---|
| Reads | `~/.claude/projects/*/*.jsonl` (or `$CLAUDE_CONFIG_DIR/projects`). Subagent transcripts are skipped. Claude Code's files are never changed. |
| Writes | One SQLite file, mode `600`, in a folder with mode `700`: |
| macOS | `~/Library/Application Support/chat-memory/index.db` |
| Linux | `$XDG_DATA_HOME/chat-memory/index.db` (default `~/.local/share/...`) |
| Windows | `%LOCALAPPDATA%\chat-memory\index.db` |

Set `CHAT_MEMORY_DATA_DIR` to keep the index somewhere else. Avoid folders that sync to the cloud.

A rewound conversation is stored by Claude Code as a new file that repeats the earlier messages. chat-memory counts each message once.

## Limits

- Secret masking works on patterns. It catches the common formats, not every possible secret, so the index is still protected with file permissions. Use `forget` for a session you don't want searchable.
- Search is keyword-based (SQLite FTS5), not semantic. Umlauts and accents are ignored when matching, so `andern` finds `ändern`.
- Only sessions that still exist in `~/.claude/projects` are indexed. Claude Code deletes old transcripts after a while (see `cleanupPeriodDays` in its settings).

## Uninstall

```
/plugin uninstall chat-memory@chat-memory
```

Then delete the index folder listed above.

## Development

```sh
python3 -m unittest discover -s tests
claude plugin validate .
```

## License

[MIT](LICENSE)
