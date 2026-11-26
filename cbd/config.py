"""Project settings: everything that differed between PRD drivers lives in <home>/<project>/config.json."""

import fnmatch
import os

from cbd.util import read_json

DEFAULTS = {
    "max_parallel": 12,
    "launch_cooldown_sec": 300,
    "max_restarts": 3,
    "hold": {},                     # package -> reason: never launched by the scheduler, only by `launch`
    "start_waived": {},             # package -> [predecessors the start does not wait for]
    "box_prereqs": {},              # "PKG#n" -> [packages that must be verified before that checkbox]
    "approval_boxes": [],           # "PKG#n" checkboxes that need `cbd approve` before they run
    "agent_box_overrides": [],      # "PKG#n" checkboxes run by an agent although their prefix is a driver prefix
    "driver_box_prefixes": ["verify", "chore", "docs"],
    "ui_tickets": [],               # glob patterns of packages whose verify is an agent box and runs the type check
    "impl": {"agent": "claude", "model": "claude-opus-5-5", "effort": "high"},
    "reviewers": [
        {"name": "codex", "agent": "codex", "model": "gpt-6.1-sol", "effort": "high", "pool": "codex"},
        {"name": "claude", "agent": "claude", "model": "claude-opus-5-5", "effort": "high", "pool": "claude"},
    ],
    "usage": {"threshold": 90, "cache_sec": 300, "commands": {
        "claude": ["python3", "~/.claude/skills/claude-usage-check/scripts/usage.py", "--no-save", "--raw"]}},
    "tc_pattern": r"TC-[A-Z0-9]+-\d+",
    "repos": {},
    "jira": {},
    "mail_wait_ms": 120000,
    "quiet_limit_sec": 7200,
    "integration_review": True,     # ticket integration review after the last checkbox (needs reviewers)
    "ui_snapshots": ["snapshots/{pkg}", "design-snapshots/{pkg}"],  # design-root dirs of exported Figma frames
    "integration_fix_title": "fix: 티켓 통합 리뷰 지적 반영",
    "cumulative_check": {"enabled": True, "reviewer": "codex"},  # GPT check of base..commit after every commit
    "alerts": True,                 # append stops and worker questions to state/alerts.jsonl
    "auto_resolve": {"after_sec": 900, "max": 3},  # scheduler reopens a retryable stop after_sec later, max times
}

REQUIRED = ("project", "design_root", "repos")


class Config:
    def __init__(self, key, home=None):
        self.key = key
        self.home = os.path.expanduser(home or os.environ.get("CBD_HOME") or "~/.checkbox-driver")
        self.dir = os.path.join(self.home, key)
        self.path = os.path.join(self.dir, "config.json")
        try:
            self.raw = self._read()
        except ValueError as e:
            raise SystemExit(f"{e} — 먼저 cbd {key} init" if "JSON" in str(e) else str(e))

    def _read(self):
        raw = read_json(self.path)
        if raw is None:
            raise ValueError(f"{self.path} 없음 또는 JSON 오류")
        merged = {**DEFAULTS, **raw}
        missing = [k for k in REQUIRED if not merged.get(k)]
        if missing:
            raise ValueError(f"{self.path} 에 {', '.join(missing)} 가 없다")
        merged["design_root"] = os.path.expanduser(merged["design_root"])
        return merged

    def reload(self):
        """Re-read config.json in place, so a long-running scheduler or driver sees edits made after it started.
        A broken file keeps the current settings; the error text is returned (None when the reload took)."""
        try:
            self.raw = self._read()
        except ValueError as e:
            return str(e)
        return None

    def __getitem__(self, k):
        return self.raw[k]

    def get(self, k, default=None):
        return self.raw.get(k, default)

    def p(self, *parts):
        return os.path.join(self.dir, *parts)

    @property
    def state_dir(self):
        return self.p("state")

    @property
    def work_dir(self):
        return os.path.expanduser(self.raw.get("work_dir") or self.p("work"))

    def repo(self, name):
        r = self.raw["repos"].get(name)
        if r is None:
            raise SystemExit(f"config.json repos 에 {name} 가 없다")
        return r

    def is_ui(self, package):
        return any(fnmatch.fnmatchcase(package, pat) for pat in self.raw["ui_tickets"])

    def box_key(self, package, n):
        return f"{package}#{n}"
