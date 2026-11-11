"""cbd <project> <command> — the checkbox driver's command line.

  init      --design-root <dir> --project <name>   write <home>/<project>/config.json from the template
  check                                            read the design, print tickets, chains and warnings
  select    [--json]                               which tickets may start now (jsonl: one candidate per line)
  plan                                             write state/plan.json (dashboard) and state/state.md
  status                                           tickets, checkboxes, open exceptions and questions
  schedule  [--once [--json]] [--interval 60]      launch selected tickets in their own Orca terminals, every interval
  launch    <PKG>                                  launch one ticket now (held / blocked / waiting predecessors too)
  run       <PKG>                                  the ticket driver itself (runs inside the launched terminal)
  approve   <PKG> <n>                              human approval for checkbox n
  resolve   <PKG>                                  close a ticket's open exceptions so the scheduler may relaunch it
  defer     <PKG> <n>                              skip checkbox n for now (the ticket stops on box_deferred at the end)
"""

import argparse
import json
import os
import shlex
import sys
import time

from cbd import orca
from cbd import plan as P
from cbd.config import Config
from cbd.design import Design
from cbd.select import select
from cbd.state import ACTIVE_STATUSES, BLOCKED, DONE_STATUSES, PLANNED, STARTED, Store
from cbd.util import DriverError, now_iso, write_json

CBD = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "bin", "cbd"))

TEMPLATE = {
    "project": "<PRD-xx 이름>",
    "design_root": "<설계 워크트리 안의 feature-designs/[PRD-xx]-... 절대경로>",
    "max_parallel": 12,
    "hold": {},
    "start_waived": {},
    "box_prereqs": {},
    "approval_boxes": [],
    "agent_box_overrides": [],
    "ui_tickets": ["F*", "D*", "P*"],
    "repos": {
        "<저장소 이름 (ticket-index 의 저장소 열)>": {
            "orca_repo": "<Orca 에 등록된 저장소 이름>",
            "base": "<체인 첫 티켓의 베이스 — 비우면 ticket-index 값>",
            "install": [],
            "test_cmd": ["<테스트 실행 명령>", "{tcs}"],
            "typecheck_cmd": [],
            "pin_path": "<핀 chore 가 올리는 서브모듈 경로 (없으면 지움)>",
        }
    },
    "jira": {"site": "", "email": "", "token_keychain_service": "", "progress_status": "진행 중", "done_status": "완료"},
    "vault": {"records_dir": ""},
    "driver_worktree": "<드라이버 터미널을 열 워크트리 경로 — 보통 설계 워크트리>",
}


def load(key):
    cfg = Config(key)
    design = Design(cfg["design_root"], cfg["tc_pattern"])
    return cfg, design, Store(cfg)


def cmd_init(a):
    home = os.path.expanduser(os.environ.get("CBD_HOME") or "~/.checkbox-driver")
    path = os.path.join(home, a.key, "config.json")
    if os.path.exists(path):
        raise SystemExit(f"{path} 가 이미 있다")
    cfg = dict(TEMPLATE, project=a.project or a.key, design_root=a.design_root or TEMPLATE["design_root"])
    write_json(path, cfg)
    print(f"{path} 를 만들었다 — repos·jira 를 채운 뒤 cbd {a.key} check")


def cmd_check(a):
    cfg, design, store = load(a.key)
    print(f"설계 {design.root}: 티켓 {len(design.tickets)} · 체크박스 {sum(len(t.boxes) for t in design.tickets.values())}")
    for repo, chain in design.chains.items():
        known = "" if repo in cfg["repos"] else "   ← config repos 에 없음"
        print(f"  체인 {repo}: {' → '.join(chain)}{known}")
    from cbd import boxes as B
    for pkg in design.order:
        t = design.tickets[pkg]
        kinds = [B.kind(cfg, t, b) for b in t.boxes]
        drv = sum(1 for k in kinds if k != B.AGENT)
        print(f"  {pkg:4} {t.jira or '-':10} {t.branch or '(브랜치 없음)':40} 선행 {','.join(t.predecessors) or '-':12} "
              f"체크박스 {len(t.boxes)} (드라이버 {drv}) TC {len(t.tcs)}")
    for w in design.warnings:
        print(f"  ! {w}")
    return 1 if design.warnings else 0


def cmd_select(a):
    cfg, design, store = load(a.key)
    cands = select(design, store.all(design.order), cfg, orca.live_handles(), forced=set(a.force or []))
    for c in cands:
        if a.json:
            print(json.dumps(c.as_dict(), ensure_ascii=False))
        else:
            print(f"{'●' if c.selected else '·'} {c.package:4} {c.reason}")


def cmd_plan(a):
    cfg, design, store = load(a.key)
    P.write(cfg, design, store, orca.live_handles())
    print(store.p("plan.json"))


def cmd_status(a):
    cfg, design, store = load(a.key)
    alive = orca.live_handles()
    for pkg in design.order:
        st = store.ticket(pkg)
        bx = st.get("checkboxes") or []
        done = sum(1 for c in bx if c.get("phase") == "done")
        cur = next((c for c in bx if c.get("phase") not in ("done", "deferred")), None)
        live = "●" if st.get("driver_terminal_handle") in alive else " "
        line = f"{live} {pkg:4} {st.get('status', PLANNED):14} {done}/{len(bx) or len(design.tickets[pkg].boxes)}"
        if cur:
            line += f"  #{cur['index']} {', '.join(sorted((cur.get('done') or {}).keys())) or cur.get('phase')}"
        print(line)
        for e in st.get("exceptions") or []:
            if not e.get("resolved"):
                print(f"      멈춤 [{e['kind']}] {e['message']}")
        for q in st.get("questions") or []:
            if not q.get("answered"):
                print(f"      질문 {q['path']}")


def launch(cfg, store, pkg, restart=False):
    """Open the ticket driver in its own Orca terminal (the Run is bound to that terminal)."""
    st = store.ticket(pkg)
    where = cfg.get("driver_worktree") or cfg["design_root"]
    cmd = shlex.join([sys.executable, CBD, cfg.key, "run", pkg])
    handle = orca.terminal_create(f"path:{where}", f"{cfg['project']} {pkg}", cmd)
    st.update(driver_terminal_handle=handle, launched_at=now_iso(), status=STARTED if st.get("status") in
              (PLANNED, "ready", None) else st.get("status"))
    if restart:
        st["restarts"] = int(st.get("restarts", 0)) + 1
    store.save(st)
    sched = store.scheduler()
    hist = sched.get("driver_history") or []
    store.save_scheduler({"driver_history": (hist + [handle])[-200:]})
    store.event(pkg, f"드라이버 기동 {handle}{' (재기동)' if restart else ''}")
    return handle


def schedule_pass(cfg, design, store, forced=(), report=None):
    """One scheduler round. `report(candidate_dict)` gets every candidate, selected = launched in this round."""
    states = store.all(design.order)
    alive = orca.live_handles()
    launched = []
    for c in select(design, states, cfg, alive, forced=forced):
        row = c.as_dict()
        if c.selected:
            try:
                h = launch(cfg, store, c.package, c.restart)
                launched.append((c.package, h))
                row["terminal"] = h
            except DriverError as e:
                row.update(selected=False, reason=f"기동 실패: {e.message}")
        if report:
            report(row)
    for pkg, st in states.items():  # a finished ticket's driver terminal is closed
        h = st.get("driver_terminal_handle")
        if st.get("status") in DONE_STATUSES and h in alive and not st.get("terminal_closed"):
            orca.terminal_close(h)
            st["terminal_closed"] = now_iso()
            store.save(st)
    P.write(cfg, design, store, orca.live_handles())
    return launched


def cmd_schedule(a):
    cfg, design, store = load(a.key)
    store.save_scheduler({"scheduler_terminal": os.environ.get("ORCA_TERMINAL_HANDLE", ""),
                          **({"coordinator_session": a.coordinator_session} if a.coordinator_session else {})})
    while True:
        try:
            design = Design(cfg["design_root"], cfg["tc_pattern"])  # the design may gain checkboxes between passes
            if a.json:
                schedule_pass(cfg, design, store, report=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True))
            else:
                for pkg, h in schedule_pass(cfg, design, store):
                    print(f"{now_iso()} 기동 {pkg} {h}", flush=True)
        except DriverError as e:
            store.event(None, f"스케줄 실패 [{e.kind}] {e.message}")
            print(f"{now_iso()} 스케줄 실패 {e}", flush=True, file=sys.stderr if a.json else sys.stdout)
            if a.once:
                return 2
        if a.once:
            return
        time.sleep(a.interval)


def cmd_launch(a):
    cfg, design, store = load(a.key)
    st = store.ticket(a.package)
    if st.get("status") == BLOCKED:
        raise SystemExit(f"{a.package} 는 멈춤 상태다 — 원인을 해소한 뒤 cbd {a.key} resolve {a.package}")
    if st.get("status") in ACTIVE_STATUSES and st.get("driver_terminal_handle") in orca.live_handles():
        raise SystemExit(f"{a.package} 드라이버가 이미 돌고 있다 ({st['driver_terminal_handle']})")
    c = next(c for c in select(design, store.all(design.order), cfg, orca.live_handles(), forced={a.package})
             if c.package == a.package)
    if not c.selected:
        raise SystemExit(f"{a.package} 를 띄울 수 없다: {c.reason}")
    print(launch(cfg, store, a.package, c.restart))


def cmd_run(a):
    from cbd.ticket import TicketDriver
    cfg, design, store = load(a.key)
    return TicketDriver(cfg, design, store, a.package).run()


def cmd_approve(a):
    cfg, design, store = load(a.key)
    store.approve(a.package, a.n, by=os.environ.get("ORCA_TERMINAL_HANDLE") or "user")
    store.event(a.package, f"#{a.n} 승인")
    print(f"{a.package}#{a.n} 승인 — 멈춰 있었다면 cbd {a.key} resolve {a.package}")


def cmd_resolve(a):
    cfg, design, store = load(a.key)
    st = store.ticket(a.package)
    for e in st.get("exceptions") or []:
        if not e.get("resolved"):
            e["resolved"] = now_iso()
    if st.get("status") == BLOCKED:
        st["status"] = STARTED if st.get("worktree") else PLANNED
        st["launched_at"] = None
    store.save(st)
    store.event(a.package, "예외 해소 — 재기동 가능")
    print(f"{a.package}: {st['status']}")


def cmd_defer(a):
    cfg, design, store = load(a.key)
    st = store.ticket(a.package)
    for c in st.get("checkboxes") or []:
        if c["index"] == a.n:
            c["phase"] = "deferred"
            store.save(st)
            store.event(a.package, f"#{a.n} 미룸")
            return
    raise SystemExit(f"{a.package}#{a.n} 가 없다 (착수 전 티켓은 미룰 수 없다)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cbd", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("key", help="프로젝트 키 (<home>/<key>/config.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init")
    s.add_argument("--design-root")
    s.add_argument("--project")
    sub.add_parser("check")
    s = sub.add_parser("select")
    s.add_argument("--json", action="store_true")
    s.add_argument("--force", action="append")
    sub.add_parser("plan")
    sub.add_parser("status")
    s = sub.add_parser("schedule")
    s.add_argument("--once", action="store_true")
    s.add_argument("--interval", type=int, default=60)
    s.add_argument("--coordinator-session")
    s.add_argument("--json", action="store_true", help="--once 와 함께: 후보마다 JSON 한 줄 (selected = 이번에 기동함)")
    for name in ("launch", "run", "resolve"):
        sub.add_parser(name).add_argument("package")
    for name in ("approve", "defer"):
        s = sub.add_parser(name)
        s.add_argument("package")
        s.add_argument("n", type=int)
    a = ap.parse_args(argv)
    fn = globals()[f"cmd_{a.cmd}"]
    try:
        return fn(a) or 0
    except DriverError as e:
        print(f"실패 {e}", file=sys.stderr)
        return 2
