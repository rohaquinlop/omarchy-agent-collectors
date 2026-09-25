# grok adapter

Custom agent-collectors adapter that shows **SuperGrok** weekly usage in the
Omarchy agents panel.

Does **not** install `calmasacow.grok-usage` — that plugin replaces the whole
panel. This adapter only writes `~/.local/state/omarchy/agents/usage/grok.json`.

`$GROK_HOME` is honoured everywhere it matters (the auth file, the session
store, and the `detect` gate — the last as a *command* gate, because a path
gate expands only `~`).

## Source of truth

Public collector used by the community plugins (not reverse-engineered grok.com):

- `GET https://cli-chat-proxy.grok.com/v1/billing?format=credits`
- Auth: SuperGrok OIDC session in `~/.grok/auth.json` (`grok login`)
- Docs: https://github.com/calmasacow/omarchy-grok-usage

`creditUsagePercent` is 0–100 and is converted to the panel's 0–1 fraction.
Product rows (Build / Chat / Imagine / Voice) are extra meters on the same
weekly pool. Reset timestamps come from `currentPeriod.end`.

## Verify

```bash
~/.config/omarchy/plugins/rohaquinlop.agent-collectors/bin/agent-collectors --validate
~/.config/omarchy/agent-collectors/adapters/grok/limits.sh
python3 -c "import json; print(json.load(open('$HOME/.local/state/omarchy/agents/usage/grok.json'))['limits'])"
```


## Local token history (collect.sh)

The billing endpoint behind `limits.sh` reports the weekly allowance and nothing
about tokens, so `collect.sh` fills the panel's TOKENS BY DAY / TOKENS BY MODEL
from what this machine can see of the same subscription:

- the **Grok CLI's own accounting** (`~/.grok/sessions/*/*/updates.jsonl`,
  `turn_completed` → `params.update.usage`, one event per `modelUsage` entry;
  its `timestamp` is already epoch seconds while the `_meta.agentTimestampMs`
  next to it is milliseconds — never use per-chunk `totalTokens`, it is not a
  turn total);
- **Pi** sessions under a grok provider (`aperture-grok`, which runs on the
  same SuperGrok credentials), attributed per message via `model_change`;
- **opencode** messages qualified `grok-sub/...` only — `opencode-go/...` is a
  different plan;
- **prompts** from `~/.grok/sessions/*/prompt_history.jsonl`.

Local only: another machine or the Grok web app cannot appear, so these totals
do not match the account's own numbers. Like the ollama hook it bounds itself
(14 days, 40k lines / 12 MB, oldest dropped with a stderr warning) because the
engine's stdout cap and its 50k-fingerprint cap both fail silently, and past
the latter a re-emitted event is counted twice.

```bash
FORCE=1 collect.sh > /tmp/grok-events.jsonl     # inspect the event stream
python3 ~/.config/omarchy/agent-collectors/adapters/grok/collect_test.py
```
