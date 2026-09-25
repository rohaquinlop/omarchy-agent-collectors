#!/usr/bin/python3
"""Regression tests for the reset-phase learning in limits.sh.

The heuristic decides whether to publish a countdown at all, and a wrong
countdown is worse than none, so each rule gets a case here. Run it after any
change to learn_window():

    python3 ~/.config/omarchy/agent-collectors/adapters/ollama/limits_test.py
"""
from __future__ import annotations

import importlib.machinery
import pathlib
import importlib.util
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_loader(
    "limits_hook", importlib.machinery.SourceFileLoader("limits_hook", os.path.join(HERE, "limits.sh"))
)
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)

T0 = 1_700_000_000.0
H = 3600.0
FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


def step(win: dict, value: float, at: float, name: str = "session"):
    return L.learn_window(win.setdefault(name, {}), name, value, at)


def resets_at(iso: str) -> float | None:
    return datetime.fromisoformat(iso).timestamp() if iso else None


def case_transitions() -> None:
    """One sample against a prepared state, per rule."""

    def one(label, win, value, at, name="session"):
        return step({name: dict(win)}, value, at, name)

    # A drop to ~0 with samples close together pins the boundary.
    _, est, prov = one("pinned drop anchors", {"value": 0.30, "atMs": int((T0 - 900) * 1000)}, 0.02, T0)
    check("pinned drop publishes a learned countdown", (est, prov), (True, False))
    # A period passing with the counter still high disproves the cadence.
    iso, est, prov = one(
        "cadence contradicted",
        {"value": 0.40, "atMs": int((T0 - 7200) * 1000), "anchorS": T0 - 7 * H,
         "boundaryMs": int((T0 - 2 * H) * 1000), "active": True},
        0.41, T0,
    )
    check("contradicted cadence falls back to a bound, not the estimate",
          (est, prov, resets_at(iso)), (False, True, T0 + 5 * H))
    # The predicted boundary inside a long gap carries the phase across a suspend.
    _, est, _ = one(
        "suspend, drop matches predicted boundary",
        {"value": 0.30, "atMs": int((T0 - 9 * H) * 1000), "anchorS": T0 - 14 * H,
         "boundaryMs": int((T0 - 9 * H) * 1000), "active": True},
        0.02, T0,
    )
    check("suspend keeps a corroborated phase", est, True)
    # Nothing was running when the window restarted: the phase is unverifiable.
    iso, est, prov = one(
        "idle restart then resumption",
        {"value": 0.0, "atMs": int((T0 - 900) * 1000), "anchorS": T0 - 5 * H,
         "boundaryMs": int((T0 - 5 * H) * 1000), "active": True},
        0.05, T0,
    )
    check("idle restart withdraws the estimate for a bound",
          (est, prov, resets_at(iso)), (False, True, T0 + 5 * H))
    # Fresh state: a bound, never a pretend estimate.
    _, est, prov = one("fresh state", {}, 0.10, T0)
    check("fresh state publishes only a bound", (est, prov), (False, True))


def case_bound_direction() -> None:
    """Every provisional bound must be an upper bound on the wait."""
    win: dict = {}
    step(win, 0.20, T0)
    step(win, 0.0, T0 + H)
    iso, est, prov = step(win, 0.03, T0 + 4500)
    check("resume-from-empty publishes a bound", (est, prov), (False, True))
    check("bound is now + period, not the earlier zero sample", resets_at(iso), T0 + 4500 + 5 * H)


def case_anchor_survives_until_contradicted() -> None:
    """Continuous use across clock boundaries keeps one running countdown."""
    win: dict = {}
    for i in range(15):
        step(win, round(0.02 * (i + 1), 4), T0 + i * 900)
    seen = []
    for at, value in ((T0 + 4 * H, 0.0), (T0 + 4 * H + 900, 0.01), (T0 + 9 * H, 0.0),
                      (T0 + 9 * H + 900, 0.01), (T0 + 14 * H, 0.0)):
        iso, est, prov = step(win, value, at)
        if est:
            seen.append((round(resets_at(iso) - T0), prov))
    check("countdown tracked three boundaries", seen,
          [(9 * H, False), (9 * H, False), (14 * H, False), (14 * H, False), (19 * H, False)])


def case_seed_from_dashboard() -> None:
    """A countdown stated by the dashboard anchors the phase, coarsely."""
    win: dict = {}
    L.seed_window(win, "session", 3 * H, T0)
    check("seed records a seeded anchor", (win["seeded"], win["active"]), (True, True))
    iso, est, prov = L.learn_window(win, "session", 0.147, T0 + 300)
    check("seed publishes an estimate, not a bound",
          (est, prov, resets_at(iso)), (True, False, T0 + 3 * H))
    # The stated phase is coarse (whole hours), so a boundary up to the seeded
    # grace late is still borne out instead of punished.
    iso, est, prov = L.learn_window(win, "session", 0.10, T0 + 3 * H + 600)
    check("boundary inside the seeded grace keeps the estimate", est, True)
    # A pinned drop replaces the statement with an observation, precisely.
    L.learn_window(win, "session", 0.30, T0 + 4 * H)
    iso, est, prov = L.learn_window(win, "session", 0.01, T0 + 4 * H + 900)
    check("pinned drop converts the seed into a learned anchor",
          (est, prov, win["seeded"], resets_at(iso)), (True, False, False, T0 + 4 * H + 900 + 5 * H))
    # A statement that never pans out is dropped after its boundary plus grace.
    win2: dict = {}
    L.seed_window(win2, "weekly", 3 * 86400.0, T0)
    L.learn_window(win2, "weekly", 0.65, T0 + 60)
    iso, est, prov = L.learn_window(win2, "weekly", 0.66, T0 + 3 * 86400.0 + 13 * H)
    check("stale seed is dropped", (win2["seeded"], win2["active"]), (False, False))


def case_withdrawn_anchor_is_not_revived() -> None:
    """A phase that failed must not come back through a later unpinnable drop."""
    for label, seq in (
        ("after a contradicted cadence",
         [(0.30, T0 - 900), (0.02, T0), (0.40, T0 + 6 * H), (0.02, T0 + 20 * H)]),
        ("after an idle restart",
         [(0.30, T0 - 900), (0.02, T0), (0.0, T0 + 3 * H), (0.05, T0 + 3 * H + 900),
          (0.02, T0 + 20 * H)]),
    ):
        win: dict = {}
        last = None
        for value, at in seq:
            last = step(win, value, at)
        iso, est, prov = last
        check(f"{label}: anchor was cleared", win["session"]["anchorS"], None)
        check(f"{label}: bound is the fresh one, not the old anchor",
              (resets_at(iso), est, prov), (T0 + 20 * H + 5 * H, False, True))


def case_detect_gate() -> None:
    """The manifest's detect command must find a key wherever limits.sh does.

    A gate that misses it disables the adapter silently (the engine skips a
    failed detect), so this pins every location the lookup honours.
    """
    import json as _json
    import os as _os
    import subprocess
    import tempfile

    manifest = _json.loads((pathlib.Path(HERE) / "manifest.json").read_text())
    command = manifest["detect"][0]["command"]

    def exits(home: str, env: dict) -> int:
        base = {"PATH": _os.environ["PATH"], "HOME": home}
        base.update(env)
        return subprocess.run(command, shell=True, env=base, capture_output=True).returncode

    with tempfile.TemporaryDirectory() as tmp:
        config = pathlib.Path(tmp, "cfg", "omarchy", "agents")
        config.mkdir(parents=True)
        (config / "ollama.env").write_text("OLLAMA_API_KEY=from-xdg\n")
        check("detect: XDG config file", exits(tmp, {"XDG_CONFIG_HOME": str(pathlib.Path(tmp, "cfg"))}), 0)
        check("detect: nothing anywhere is a skip", exits(tmp, {"XDG_CONFIG_HOME": str(pathlib.Path(tmp, "empty"))}) != 0, True)

    with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
        (pathlib.Path(one, "a.env")).write_text("OTHER=1\n")
        (pathlib.Path(two, "b.env")).write_text("OLLAMA_API_KEY=from-list\n")
        listed = f"{pathlib.Path(one, 'a.env')}:{pathlib.Path(two, 'b.env')}"
        check(
            "detect: colon-separated OLLAMA_USAGE_ENV list",
            exits(one, {"OLLAMA_USAGE_ENV": listed}),
            0,
        )
        check("detect: env var wins", exits(one, {"OLLAMA_API_KEY": "x"}), 0)


def main() -> int:
    for case in (case_transitions, case_bound_direction,
                 case_anchor_survives_until_contradicted,
                 case_withdrawn_anchor_is_not_revived, case_seed_from_dashboard,
                 case_detect_gate):
        print(case.__name__)
        case()
    print(f"\n{'all cases pass' if not FAILURES else str(len(FAILURES)) + ' FAILURES'}")
    for failure in FAILURES:
        print(" -", failure)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
