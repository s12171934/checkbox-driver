"""Which tickets may start now, and on what base.

Rules (티켓 체크박스 커밋 리뷰 방식 · 같은 저장소 체인):
- A ticket starts when every DAG predecessor (ticket-index 「선행」) is verified — chain order alone never blocks it.
  `start_waived` lists predecessors a ticket does not wait for.
- Its base is the verified head of the nearest verified ticket before it in its repo chain, else the repo base.
  It never starts on top of an in-progress branch.
- verified tickets must be a prefix of each chain; a gap stops the scheduler for that chain.
- Held tickets (`hold`) start only through `cbd launch`.
- At most `max_parallel` tickets run at once; a ticket launched less than `launch_cooldown_sec` ago is not relaunched;
  a ticket whose driver died is relaunched up to `max_restarts` times.

Every ticket gets one Candidate, selected or not, with the reason — the canvas script node and `cbd select` print them.
"""

from cbd.state import (ACTIVE_STATUSES, BLOCKED, BOX_DEFERRED, BOX_WAITING, DONE_STATUSES, MANUAL, PLANNED,
                       REBASE_PENDING, seconds_since)


class Candidate:
    def __init__(self, package, selected, reason, label="", base=None, restart=False):
        self.package = package
        self.selected = selected
        self.reason = reason
        self.label = label
        self.base = base          # {"ref", "sha"?, "from"} when selected
        self.restart = restart

    def as_dict(self):
        d = {"item": self.package, "selected": self.selected, "label": self.label, "reason": self.reason}
        if self.base:
            d["base"] = self.base
        if self.restart:
            d["restart"] = True
        return d


def base_for(design, states, cfg, package):
    """(base, problem). base = {"ref", "sha", "from"}; problem is a reason string when no valid base exists."""
    t = design.tickets[package]
    prev = t.chain_prev
    while prev:
        st = states.get(prev, {})
        if st.get("status") in DONE_STATUSES and st.get("verified_head"):
            return {"ref": st.get("branch") or design.tickets[prev].branch, "sha": st["verified_head"], "from": prev}, None
        prev = design.tickets[prev].chain_prev if prev in design.tickets else None
    chain_head = design.chains.get(t.repo, [package])[0] if t.repo else package
    base = design.tickets[chain_head].base if chain_head in design.tickets else None
    repo_cfg = cfg["repos"].get(t.repo or "", {})
    base = repo_cfg.get("base") or base
    if not base:
        return None, f"저장소 베이스를 모름 ({t.repo}) — config repos.{t.repo}.base"
    return {"ref": base, "sha": None, "from": None}, None


def chain_gaps(design, states):
    """Repos whose verified tickets are not a prefix of the chain -> description."""
    out = {}
    for repo, chain in design.chains.items():
        seen_open = None
        for pkg in chain:
            done = states.get(pkg, {}).get("status") in DONE_STATUSES
            if not done and seen_open is None:
                seen_open = pkg
            elif done and seen_open is not None:
                out[repo] = f"{pkg} 가 verified 인데 체인 앞 {seen_open} 는 아니다"
                break
    return out


def select(design, states, cfg, alive=frozenset(), forced=()):
    """-> [Candidate] in design order. `alive` = live driver terminal handles; `forced` = packages `cbd launch` asked for."""
    limit = int(cfg["max_parallel"])
    running = [p for p, s in states.items() if s.get("status") in ACTIVE_STATUSES
               and s.get("driver_terminal_handle") in alive]
    slots = limit - len(running)
    gaps = chain_gaps(design, states)
    out = []
    for pkg in design.order:
        t, st = design.tickets[pkg], states.get(pkg, {})
        status = st.get("status", PLANNED)
        label = f"{pkg} {t.title}".strip()
        if not t.repo:
            out.append(Candidate(pkg, False, "저장소 없음 — 드라이버가 몰지 않는 티켓(감사 등)", label))
            continue
        if status in DONE_STATUSES:
            out.append(Candidate(pkg, False, "완료" if status != REBASE_PENDING else "리베이스 대기", label))
            continue
        if status == MANUAL:
            out.append(Candidate(pkg, False, "수동 진행", label))
            continue
        if status == BLOCKED:
            exc = (st.get("exceptions") or [{}])[-1]
            out.append(Candidate(pkg, False, f"멈춤: [{exc.get('kind', '?')}] {exc.get('message', '')}".strip(), label))
            continue
        if status == BOX_DEFERRED:
            out.append(Candidate(pkg, False, "미룬 체크박스만 남음", label))
            continue
        restart = False
        if status in ACTIVE_STATUSES:
            if st.get("driver_terminal_handle") in alive:
                out.append(Candidate(pkg, False, "실행 중", label))
                continue
            if int(st.get("restarts", 0)) >= int(cfg["max_restarts"]):
                out.append(Candidate(pkg, False, f"재기동 {cfg['max_restarts']}회 초과 — 사람 확인", label))
                continue
            restart = True
        if t.repo in gaps:
            out.append(Candidate(pkg, False, f"체인 결함: {gaps[t.repo]}", label))
            continue
        if pkg in cfg["hold"] and pkg not in forced:
            out.append(Candidate(pkg, False, f"보류: {cfg['hold'][pkg]}", label))
            continue
        waived = set(cfg["start_waived"].get(pkg, []))
        waiting = [d for d in t.predecessors if d not in waived and states.get(d, {}).get("status") not in DONE_STATUSES]
        if waiting and pkg not in forced and status != BOX_WAITING:
            out.append(Candidate(pkg, False, "선행 대기: " + ", ".join(waiting), label))
            continue
        if status == BOX_WAITING and not box_ready(cfg, states, pkg, st):
            out.append(Candidate(pkg, False, "체크박스 선행 대기: " + st.get("waiting_on", ""), label))
            continue
        if seconds_since(st.get("launched_at")) < int(cfg["launch_cooldown_sec"]) and pkg not in forced:
            out.append(Candidate(pkg, False, "기동 직후", label))
            continue
        if slots <= 0 and pkg not in forced:
            out.append(Candidate(pkg, False, f"기동 대기 (동시 상한 {limit})", label))
            continue
        base, problem = (st.get("base"), None) if st.get("worktree") else base_for(design, states, cfg, pkg)
        if problem:
            out.append(Candidate(pkg, False, problem, label))
            continue
        why = "재기동" if restart else ("체크박스 선행 충족" if status == BOX_WAITING else "선행 충족")
        out.append(Candidate(pkg, True, why, label, base, restart))
        slots -= 1
    return out


def box_ready(cfg, states, package, st):
    """A box_waiting ticket may resume when its waited-on packages are verified."""
    need = [x for x in (st.get("waiting_on") or "").split(",") if x.strip()]
    return all(states.get(x.strip(), {}).get("status") in DONE_STATUSES for x in need)
