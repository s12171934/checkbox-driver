"""state/plan.json (agent dashboard `external_plans` contract) and state/state.md (the human/top-coordinator view).

plan.json fields follow agent-dashboard server/adash/features/plans/external.py and frontend/app.js:
top: project, driver, generated_at, design_root, design_commit, coordinator_session, coordinator_terminal,
     scheduler_terminal, tickets[], extra_worktrees[]
ticket: package, jira, repo, predecessors, chain_position, chain_length, hold, max_parallel, status,
        driver_terminal, driver_terminal_handle, run_id, boxes_total, boxes_done, sessions[], worktree{}, tc_plan[]
session: key, box, kind, agent, model, status (planned·created·spawned·done), task_id, deps, title
"""

from cbd import boxes as B
from cbd.design import Box
from cbd.select import select
from cbd.state import DONE_STATUSES
from cbd.util import git, now_iso, write_json, write_text


def ticket_boxes(design, st, pkg):
    """The checkboxes as the ticket now has them: the driver's list once started (fix boxes may be added), else the design's."""
    if st.get("checkboxes"):
        return [Box(c["index"], c["title"], c.get("phase") == "done") for c in st["checkboxes"]]
    return design.tickets[pkg].boxes


def session_status(st, row):
    c = next((c for c in st.get("checkboxes") or [] if c["index"] == row["box"]), None)
    if not c:
        return "planned"
    if c.get("phase") == "done":
        return "done"
    if c.get("phase") == "deferred":
        return "deferred"
    task = (c.get("tasks") or {}).get(row["kind"])
    if (c.get("done") or {}).get(row["kind"]):
        return "done"
    if (c.get("dispatch") or {}).get(row["kind"]):
        return "spawned"
    return "created" if task else "planned"


def build(cfg, design, store, alive=frozenset()):
    states = store.all(design.order)
    cands = {c.package: c for c in select(design, states, cfg, alive)}
    sched = store.scheduler()
    try:
        design_commit = git(design.root, "rev-parse", "HEAD", check=True)
    except Exception:
        design_commit = cfg.get("design_commit") or ""
    tickets = []
    for pkg in design.order:
        t, st = design.tickets[pkg], states[pkg]
        bx = ticket_boxes(design, st, pkg)
        rows = []
        for r in B.ticket_sessions(cfg, t, bx):
            c = next((c for c in st.get("checkboxes") or [] if c["index"] == r["box"]), {})
            row = {"key": r["key"], "box": r["box"], "kind": r["kind"], "agent": r["agent"], "model": r["model"],
                   "status": session_status(st, r), "deps": r["deps"], "title": r["title"]}
            tid = (c.get("tasks") or {}).get(r["kind"])
            if tid:
                row["task_id"] = tid
            if c.get("fix_source"):
                row["fix_source"] = c["fix_source"]
            rows.append(row)
        status = st.get("status", "planned")
        if status == "planned" and cands.get(pkg) and cands[pkg].selected:
            status = "ready"
        wt = st.get("worktree")
        tickets.append({
            "package": pkg, "jira": t.jira, "repo": t.repo, "predecessors": t.predecessors,
            "chain_position": t.chain_position, "chain_length": t.chain_length,
            **({"hold": cfg["hold"][pkg]} if pkg in cfg["hold"] else {}),
            "max_parallel": cfg["max_parallel"], "status": status,
            "driver_terminal": f"{cfg['project']} {pkg}" if st.get("driver_terminal_handle") else "",
            "driver_terminal_handle": st.get("driver_terminal_handle") or "",
            "run_id": st.get("run_id") or "",
            "boxes_total": len(bx), "boxes_done": sum(1 for c in st.get("checkboxes") or [] if c.get("phase") == "done"),
            "sessions": rows,
            "worktree": {"repo": t.repo, "base_ref": (st.get("base") or {}).get("ref") or t.base or "",
                         "branch": st.get("branch") or t.branch,
                         "trigger": {"predecessors": t.predecessors, "chain_previous": t.chain_prev,
                                     "rule": "선행 verified · 가장 가까운 verified 체인 헤드 위"},
                         "status": "created" if wt else "planned", "path": wt or ""},
            "tc_plan": [{"tc": tc, "box": None} for tc in t.tcs],
            "selection": cands[pkg].reason if pkg in cands else "",
        })
    plan = {"project": cfg["project"], "driver": "checkbox-driver", "generated_at": now_iso(),
            "design_root": design.root, "design_commit": design_commit,
            "coordinator_session": sched.get("coordinator_session") or "",
            "coordinator_terminal": sched.get("coordinator_terminal") or "",
            "scheduler_terminal": sched.get("scheduler_terminal") or "",
            "tickets": tickets, "extra_worktrees": []}
    return plan


def write(cfg, design, store, alive=frozenset()):
    plan = build(cfg, design, store, alive)
    write_json(store.p("plan.json"), plan)
    write_text(store.p("state.md"), render_md(cfg, design, store, plan))
    return plan


def render_md(cfg, design, store, plan):
    """One page a person or the top coordinator reads to decide: launch / approve / answer / resolve."""
    lines = [f"# {cfg['project']} 구현 상태", "", f"- 갱신: {plan['generated_at']}",
             f"- 설계: `{design.root}` @ `{plan['design_commit'][:10]}`", ""]
    open_exc, waiting_q = [], []
    lines += ["## 티켓", "", "| 티켓 | Jira | 상태 | 체크박스 | 선별 | 워크트리 |", "| --- | --- | --- | --- | --- | --- |"]
    for t in plan["tickets"]:
        st = store.ticket(t["package"])
        for e in st.get("exceptions") or []:
            if not e.get("resolved"):
                open_exc.append(f"- {t['package']} [{e['kind']}] {e['message']} ({e['at']})")
        for q in st.get("questions") or []:
            if not q.get("answered"):
                waiting_q.append(f"- {t['package']} 질문 — `{q['path']}` (답: 같은 폴더 `{q['msg_id']}.md`)")
        lines.append(f"| {t['package']} | {t['jira']} | {t['status']} | {t['boxes_done']}/{t['boxes_total']} | "
                     f"{t['selection']} | {t['worktree']['path'] or '—'} |")
    lines += ["", "## 미해결 예외", ""] + (open_exc or ["- 없음"])
    lines += ["", "## 질문 대기", ""] + (waiting_q or ["- 없음"])
    lines += ["", "## 최근 이벤트", ""] + [f"- {e['at']} {e['package'] or ''} {e['text']}" for e in store.events()[-15:]]
    lines += ["", "## 할 수 있는 것", "",
              f"- 보류·멈춘 티켓 띄우기: `cbd {cfg.key} launch <PKG>`",
              f"- 사람 승인: `cbd {cfg.key} approve <PKG> <번호>`",
              f"- 예외 해소 뒤 재개: `cbd {cfg.key} resolve <PKG>` 다음 `launch`", ""]
    return "\n".join(lines)


def verified_count(states):
    return sum(1 for s in states.values() if s.get("status") in DONE_STATUSES)
