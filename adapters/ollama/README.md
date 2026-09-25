# ollama adapter

Custom agent-collectors adapter that shows **Ollama Cloud** usage in the
Omarchy agents panel: plan label, Session (5h) and Weekly (7d) rate-limit
meters, per-model request counts, and a learned reset countdown.

## How it works

- `manifest.json` — declarative adapter: `detect` gates on Ollama API-key
  presence (`OLLAMA_API_KEY` in the environment, else a `KEY=value` file:
  `$OLLAMA_USAGE_ENV` — a colon-separated list, like `PATH` — or by default
  `~/.config/omarchy/agents/ollama.env`, then a shared
  `~/.config/agent-secrets/.env`);
  `collect.sh` emits zero events (usage is server-side, there is no local
  event stream to read); `limits.sh` emits the limits array. `tierLabel` is
  static in the manifest by contract, and `limits.sh` refreshes that one key
  from `POST /api/me` when the plan changes.
- `limits.sh` — calls `GET https://ollama.com/api/usage` with Bearer auth.
  - **Meters:** the fraction counters (`1.0` = 100%) map straight onto the
    panel's `percent` field, which uses the same unit.
  - **Windows are read, not assumed:** paid accounts answer with session/weekly,
  free ones with `monthly` alone, so the hook renders whatever the response
  carries. Only the rolling windows are learned; a monthly pool resets on a
  calendar day (ollama.com/pricing: "on the Free plan, usage resets monthly
  from the date you signed up", paid plans from the subscription start), so its
  reset is derived from `/api/me` `CreatedAt` and marked estimated rather than
  reported.
- **More than one account:** the engine writes one record per adapter, so a
  second account is a second adapter — a directory whose `limits.sh` exports
  `OLLAMA_USAGE_VAR` (which variable holds that account's key) and
  `OLLAMA_USAGE_ACCOUNT` (a stable name) and execs this hook. Both the failure
  cache and the learned samples are keyed by that name, so accounts never read
  each other's phase; the shim should also export `OLLAMA_USAGE_ADAPTER_DIR` so
  the plan label lands in its own manifest.
- **Per-model requests:** the same response carries
    `limits.<window>.models[] = {name, request_count}`. The stock record
    contract has no field for them, so they ride on the limit row as the
    extra key `requestModels`. The engine passes limit rows through verbatim
    and the stock panel ignores keys it does not know; the panel fork
    ([`patrickpassosb/omarchy-agents-plus`](https://github.com/patrickpassosb/omarchy-agents-plus))
    renders them as
    **MODELS USED THIS WEEK**.
  - **Reset countdown:** the API returns no reset timestamp, so it comes from
    the provider in one of three ways, in order of quality:
    1. **Seeded** — the ollama.com dashboard states it ("Resets in 3 hours"
       / "Resets in 3 days"). Feed it in and the phase is known at once:
       ```bash
       limits.sh --seed session=3h weekly=3d
       ```
       Whole hours/days only, so it shows as `~` and gets a wider grace
       (`GRACE_SEED_S`) before being treated as contradicted; the first
       observed boundary replaces it with a precise anchor.
    2. **Learned**, as described next.
    3. **Bounded** — before either, a true upper bound (`Resets in ≤4h 59m`). One sample per run is folded into
    `~/.cache/omarchy/ollama-usage/state.json` (flock-guarded); a sample that
    drops to ~0 marks a window boundary; boundary + the documented period
    (5h / 7d) predicts the next reset, published as `resetsAt` with
    `resetsEstimated: true` — the fork then shows `Resets in ~2h 9m`. The
    estimate is withdrawn when a period passes without the predicted drop,
    when a window restarts while nothing was running, or when a drop cannot be
    placed in time (suspend, long idle) — and a withdrawn phase takes its
    anchor with it, so a later unpinnable drop cannot revive it. Before the
    first boundary the chip is not blank: usage exists, so the window started
    at or before the first sample that showed it and the reset cannot be later
    than `firstSeen + period` — published as an upper bound
    (`resetsProvisional`, rendered `Resets in ≤4h 59m`, true but loose).
    `limits.sh --status` prints the learned state.
- Never prints the API key.

## What is deliberately not here

- **Extra-usage balance / prepaid credits.** Every guessed `/api/*` path
  (balance, credits, billing, account, plan, subscription, extra-usage) 404s;
  `activity.cost` is extra usage *spent* (`0.00000` here). The number exists
  only on the signed-in settings page, and the engine's `build_record()` has
  no `balance` field for a hook to fill, so it is not portable without a
  browser session and an engine change. Not worth it for an account that has
  no prepaid credits in play.
- **Token stats by day/model, from the account.** Ollama's account API reports
  request counts, not tokens or days. `collect.sh` therefore fills those two
  panel sections from *this machine's* agent logs instead: opencode messages
  whose model id is provider-qualified (`ollama-cloud/...`) and Pi sessions,
  attributing each message to the provider in force (`model_change` events,
  since Pi does not record the provider per message). Model ids lose the
  `ollama-cloud/` qualification so they line up with the account's names.

  They are **local tokens**: usage from another machine, another tool, or a
  browser cannot appear, so they will not agree in size with the account's
  request counts — they answer a different question ("what did my agents send
  through Ollama?").

## Verify

```bash
~/.config/omarchy/plugins/rohaquinlop.agent-collectors/bin/agent-collectors --validate
~/.config/omarchy/agent-collectors/adapters/ollama/limits.sh              # limits array
~/.config/omarchy/agent-collectors/adapters/ollama/limits.sh --status     # learned state
python3 -m json.tool ~/.local/state/omarchy/agents/usage/ollama.json
python3 ~/.config/omarchy/agent-collectors/adapters/ollama/limits_test.py   # learning rules
```

The last one is the regression suite for the reset-phase heuristic (18 checks:
pinned drop, contradicted cadence, suspend, idle restart, fresh state, bound
direction, anchor revival, seed/expiry/conversion). Run it after any change to `learn_window()` — a
wrong countdown is worse than none, and two bugs in that function were found
by these cases, not by reading it.

The engine runs every 15 minutes and on panel refresh; the panel picks up the
record change without a restart (QML changes do need
`omarchy restart shell`). Disable via widget settings
(`providers.ollama.enabled: false`).

Note: the "~" marker on a learned countdown is rendered by the panel fork. The
stock `omarchy.agents` panel ignores `resetsEstimated` and would show the same
countdown without it.
