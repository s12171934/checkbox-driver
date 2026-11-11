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
}

REQUIRED = ("project", "design_root", "repos")


class Config:
    def __init__(self, key, home=None):
        self.key = key
        self.home = os.path.expanduser(home or os.environ.get("CBD_HOME") or "~/.checkbox-driver")
        self.dir = os.path.join(self.home, key)
        self.path = os.path.join(self.dir, "config.json")
        raw = read_json(self.path)
        if raw is None:
            raise SystemExit(f"{self.path} 없음 또는 JSON 오류 — 먼저 cbd {key} init")
        self.raw = {**DEFAULTS, **raw}
        missing = [k for k in REQUIRED if not self.raw.get(k)]
        if missing:
            raise SystemExit(f"{self.path} 에 {', '.join(missing)} 가 없다")
        self.raw["design_root"] = os.path.expanduser(self.raw["design_root"])

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
