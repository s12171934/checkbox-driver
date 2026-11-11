"""Usage gates (티켓 체크박스 커밋 리뷰 사용량 게이트).

- Two gates: before an implementation starts and before a review round. Both wait when blocked; neither drops nor
  swaps a reviewer. Threshold `usage.threshold` (90 %).
- A pool is blocked when its reading is >= threshold and the reset is still ahead.
- A failed reading never stops work: reuse the last successful reading regardless of age (0 % when its reset has
  passed); with no reading ever, proceed and record that.
- Readings are cached `usage.cache_sec` seconds in state/usage-<pool>.json; a failed read is retried once.

A pool reader is a command in config `usage.commands.<pool>` (argument list) printing JSON. The reader accepts
{"utilization": n, "resets_at": iso} or the shapes of claude-usage-check (`five_hour.utilization`, `resets_at`)
and of Codex rate-limit snapshots (`rate_limits.primary.used_percent` …), taking the highest window.
"""

import datetime
import json
import os
import time

from cbd.util import now_ts, read_json, run, write_json


def _windows(node, out):
    if isinstance(node, dict):
        u = next((node[k] for k in ("utilization", "used_percent", "percent") if isinstance(node.get(k), (int, float))), None)
        if u is not None and not ((node.get("scope") or {}).get("model")):  # model-only limits do not block the pool
            reset = node.get("resets_at") or node.get("reset_at") or node.get("resets_in_seconds")
            out.append((float(u), reset))
        for v in node.values():
            _windows(v, out)
    elif isinstance(node, list):
        for v in node:
            _windows(v, out)
    return out


def _reset_ts(reset):
    if isinstance(reset, (int, float)):
        return now_ts() + reset if reset < 10 ** 9 else float(reset)
    try:
        return datetime.datetime.fromisoformat(str(reset).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_reading(data):
    """-> (percent, reset_ts or None) of the most used window."""
    wins = _windows(data, [])
    if not wins:
        return None
    pct, reset = max(wins, key=lambda w: (w[0], w[1] is not None))
    return pct, _reset_ts(reset) if reset is not None else None


class Gate:
    def __init__(self, cfg, store, log):
        self.cfg, self.store, self.log = cfg, store, log
        u = cfg["usage"]
        self.threshold = float(u.get("threshold", 90))
        self.cache = int(u.get("cache_sec", 300))
        self.commands = u.get("commands") or {}

    def _path(self, pool):
        return self.store.p(f"usage-{pool}.json")

    def read(self, pool):
        """-> (percent, reset_ts, source) where source is fresh / cached / last / none."""
        saved = read_json(self._path(pool), {})
        if saved.get("at") and now_ts() - saved["at"] < self.cache:
            return saved["pct"], saved.get("reset"), "cached"
        cmd = [os.path.expanduser(c) for c in self.commands.get(pool) or []]
        if cmd:
            for _ in range(2):
                try:
                    r = run(cmd, timeout=60)
                    reading = parse_reading(json.loads(r.stdout)) if r.returncode == 0 else None
                except (ValueError, OSError, Exception):
                    reading = None
                if reading:
                    write_json(self._path(pool), {"at": now_ts(), "pct": reading[0], "reset": reading[1]})
                    return reading[0], reading[1], "fresh"
                time.sleep(5)
        if saved.get("pct") is not None:
            if saved.get("reset") and saved["reset"] < now_ts():
                return 0.0, None, "last"
            return saved["pct"], saved.get("reset"), "last"
        return None, None, "none"

    def blocked(self, pool):
        """-> (blocked, wait_seconds, note)."""
        pct, reset, src = self.read(pool)
        if pct is None:
            return False, 0, f"{pool} 사용량 판독 없음 — 진행"
        if pct < self.threshold:
            return False, 0, f"{pool} {pct:.0f}% ({src})"
        wait = max(60, (reset - now_ts() + 60) if reset else 600)
        return True, min(wait, 3600), f"{pool} {pct:.0f}% ≥ {self.threshold:.0f}% ({src}) — 리셋까지 대기"

    def wait(self, pools, why):
        """Block until every pool in `pools` is under the threshold. Returns the notes (for the vault record)."""
        notes, waited = [], False
        while True:
            states = [(p, *self.blocked(p)) for p in pools]
            stuck = [(p, w, n) for p, b, w, n in states if b]
            if not stuck:
                notes += [n for _, _, _, n in states]
                if waited:
                    notes.append("게이트 대기 있었음")
                return notes
            waited = True
            wait = max(w for _, w, _ in stuck)
            self.log(f"{why} 게이트 대기 {int(wait)}초 — " + " · ".join(n for _, _, n in stuck))
            time.sleep(wait)
            for p, _, _ in stuck:  # force a fresh reading after the wait
                saved = read_json(self._path(p), {})
                if saved:
                    saved["at"] = 0
                    write_json(self._path(p), saved)
