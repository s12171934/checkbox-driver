"""How each checkbox is carried out, and the sessions (Orca tasks) it needs.

- driver box: no LLM, no review. The driver itself makes the commit.
  - `verify` of a non-UI ticket: run the completion TCs (and the type check when configured), commit --allow-empty.
  - `chore` that pins the front-common submodule to the producer's verified head (title mentions 핀/pin).
- agent box: everything else — implement (staged, not committed) -> reviewers in parallel -> judge in the
  implementing terminal, which commits. UI tickets' verify is an agent box (it includes the one-scene comparison).
  An agent `verify` whose implementer staged nothing (tests only) skips reviewers and judge; the driver commits the
  draft --allow-empty.

Ticket integration review (체크박스를 넘는 검증 §3): after the design's last checkbox, the reviewers read the whole
branch once against the ticket's 검증 목표 ("<PKG>#통합 리뷰 <이름>", box INTEGRATION). Findings add one fix checkbox
(fix_source "integration") that runs as an agent box after the review.

Session keys are the Orca task titles and the plan.json `key`: "<PKG>#<n> <역할> <이름>", which the dashboard
matches by title when a plan row has no task id yet.
"""

import re

DRIVER_VERIFY, DRIVER_PIN, AGENT = "driver-verify", "driver-pin", "agent"
INTEGRATION = "통합"  # the box value of the integration review's sessions and record


def kind(cfg, ticket, box):
    key = cfg.box_key(ticket.package, box.index)
    if key in cfg["agent_box_overrides"] or box.prefix not in cfg["driver_box_prefixes"]:
        return AGENT
    if box.prefix == "verify" and not cfg.is_ui(ticket.package):
        return DRIVER_VERIFY
    if box.prefix == "chore" and ticket.pin and re.search(r"핀|\bpin\b", box.title, re.I):
        return DRIVER_PIN
    return AGENT


def impl_name(cfg):
    return cfg["impl"].get("name") or cfg["impl"]["agent"]


def sessions(cfg, ticket, box):
    """[{key, kind, agent, model, deps(keys)}] for one checkbox, in start order."""
    pkg, n = ticket.package, box.index
    k = kind(cfg, ticket, box)
    if k != AGENT:
        return [{"key": f"{pkg}#{n} 드라이버 {box.prefix}", "kind": "driver", "agent": "", "model": "", "deps": []}]
    impl = cfg["impl"]
    rows = [{"key": f"{pkg}#{n} 구현 {impl_name(cfg)}", "kind": "impl", "agent": impl["agent"], "model": impl["model"],
             "deps": []}]
    for r in cfg["reviewers"]:
        rows.append({"key": f"{pkg}#{n} 리뷰 {r['name']}", "kind": f"review-{r['name']}", "agent": r["agent"],
                     "model": r["model"], "deps": [rows[0]["key"]]})
    rows.append({"key": f"{pkg}#{n} 판정 {impl_name(cfg)}", "kind": "judge", "agent": impl["agent"],
                 "model": impl["model"], "deps": [r["key"] for r in rows[1:]] or [rows[0]["key"]]})
    return rows


def integration_enabled(cfg):
    return bool(cfg.get("integration_review", True) and cfg["reviewers"])


def integration_sessions(cfg, ticket):
    return [{"key": f"{ticket.package}#{INTEGRATION} 리뷰 {r['name']}", "kind": f"review-{r['name']}",
             "agent": r["agent"], "model": r["model"], "deps": []} for r in cfg["reviewers"]]


def ticket_sessions(cfg, ticket, boxes=None):
    """All sessions of a ticket, each checkbox's first session depending on the previous checkbox's last one(s).
    The integration review comes after the design's checkboxes and before the fix checkbox it may add."""
    out, last = [], []
    boxes = list(boxes if boxes is not None else ticket.boxes)
    at = next((i for i, b in enumerate(boxes) if getattr(b, "fix_source", "") == "integration"), len(boxes))
    seq = boxes[:at] + ([INTEGRATION] if integration_enabled(cfg) else []) + boxes[at:]
    for box in seq:
        if box == INTEGRATION:
            rows = integration_sessions(cfg, ticket)
            for r in rows:
                r.update(deps=list(last), box=INTEGRATION, title="티켓 통합 리뷰")
            out += rows
            last = [r["key"] for r in rows]
            continue
        rows = sessions(cfg, ticket, box)
        if last and not rows[0]["deps"]:
            rows[0]["deps"] = list(last)
        for r in rows:
            r["box"] = box.index
            r["title"] = box.title
        out += rows
        last = [rows[-1]["key"]]
    return out
