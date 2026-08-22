# omarchy-agent-collectors

Extensible usage collectors for AI coding agents on [Omarchy](https://omarchy.org).
One engine, many drop-in adapters. Writes stock-contract records into
`~/.local/state/omarchy/agents/usage/`, where Omarchy's built-in
`omarchy.agents` bar panel picks them up automatically — no UI code involved.

Out of the box: **Pi** (`~/.pi/agent/sessions`) and **OpenCode**
(`~/.local/share/opencode/opencode.db`). Anything else is one manifest away.

## Install

```bash
omarchy plugin add https://github.com/rohaquinlop/omarchy-agent-collectors.git --enable
```

The service runs the engine at shell start and every 15 minutes. Records for
every detected agent appear within seconds; the Agents panel grows a chip per
agent (`h`/`l` to switch). Remove with `omarchy plugin remove roha.agent-collectors`.

## How it works

```
adapters/<id>/manifest.json   declarative source + field map  ┐
~/.config/omarchy/agent-collectors/adapters/<id>/…            ├→ engine → ~/.local/state/omarchy/agents/usage/<id>.json
(optional) collect/limits hooks                                ┘
```

Per run the engine:

1. Discovers adapters — built-in `adapters/`, then the user dir (same id = user wins).
2. Filters: skips an id when an official `omarchy-agent-usage-<id>` collector
   exists (superseded), when disabled via widget settings, or when its
   `detect` paths are missing.
3. Collects canonical events `{ts, session, model, kind, input, output, cacheRead, cacheWrite}`.
4. Aggregates today / last-7-days / all-time / per-model token buckets.
5. Writes `<id>.json` atomically (temp + rename, mode 600). One failing adapter never blocks others.

State (per-file parse caches) lives in `~/.cache/omarchy/agent-collectors/state.json`;
unchanged sources are not re-parsed. Delete it (or pass `--force`) for a full rescan.

### CLI

```bash
~/.config/omarchy/plugins/roha.agent-collectors/bin/agent-collectors --validate   # list adapters
... --force                       # full rescan
... pi                            # only this adapter
... --except opencode             # everything but
```

## Widget settings honored

If you disable a provider in the Agents widget settings
(`providers.<id>.enabled: false` under the `omarchy.agents` bar entry), the
engine stops writing its record and the panel drops the tab.

## Writing an adapter

Create `~/.config/omarchy/agent-collectors/adapters/<id>/manifest.json`:

```json
{
  "schemaVersion": 1,
  "id": "gemini",
  "name": "Gemini",
  "detect": [{ "path": "~/.gemini" }],
  "sources": [{
    "format": "jsonl-lines",
    "glob": "~/.gemini/tmp/**/*.chats.json",
    "kindPath": "type", "kinds": ["message"],
    "rolePath": "message.role", "promptRole": "user", "completionRole": "assistant",
    "timestampPath": "timestamp", "modelPath": "message.model",
    "tokens": { "input": "message.usage.input", "output": "message.usage.output" }
  }]
}
```

- Formats: `jsonl-lines` (glob + per-line field map) or `sqlite-query`
  (read-only database + query + column map; `timestampUnit: "ms"` if needed).
- Dot paths index nested JSON (`message.usage.input`).
- Prompts are events whose role matches `promptRole`; completions match
  `completionRole` and carry tokens.
- `sessionIdPath` optional — defaults to the file stem (one session per file).

For anything the field map can't express, ship executables next to the
manifest and reference them:

```json
{ "collect": "collect.sh", "limits": "limits.sh" }
```

- `collect` prints one canonical-event JSON object per line on stdout.
- `limits` prints a limits-array JSON on stdout; exit 1 means "no data", not failure.
- Both run with the adapter directory as cwd; `FORCE=1` is set on forced runs.

Validate your setup with `bin/agent-collectors --validate`. A broken adapter
is skipped with a warning; it can never take other agents down.

## Notes

- Rate-limit meters only appear for adapters with a working `limits` hook;
  Pi has no usage endpoint, so its tab shows local stats only.
- The stock panel resolves provider icons from its own read-only assets
  directory; unknown agents render the standard bar glyph unless/until
  marks land upstream.
- Tests: `python3 -m unittest discover -s tests`.

## License

MIT
