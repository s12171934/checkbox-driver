"""Reads the design graph a project is built from.

Inputs (all under <design_root>):
- plan/commits.md       ## <PKG> sections of "- [ ] `<commit title>`" lines — one checkbox = one commit.
- plan/ticket-index.md  요약표 (티켓 · Jira? · 저장소 · 베이스 · 층 · 선행 · 체인 앞 · 핀) and per-ticket 「테스트:」 lines
                        and 「검증 목표:」 lists (what the ticket integration review checks the whole branch against).
- plan/jira-map.md      트리 표 (키 · Jira · 브랜치?) — the Jira key of each package.
- plan/order.md         완료 기준 tables and the 스택 체인 table (branch names per repo, in chain order).
- tests/*.md            TC rows (ID or TC column, optional 층 column), used to expand layer-named criteria.

Single-doc form — no plan/commits.md but <design_root>/design.md: the same tickets are read from one document.
Section numbers move between projects, so sections are found by heading words and tables by their header names:
- 구현 순서 table (패키지 · 선행 · 테스트)                   ticket order, title, predecessors, criteria fallback.
- 트리 table (티켓 · 저장소 · 브랜치 · 베이스/스택 부모 · 대상 테스트 · is blocked by) — the 티켓 cell starts with the
  Jira key; a base cell naming a package is the chain parent; a 「—」 branch marks a branchless verification ticket.
- 스택 체인 table (저장소 · 체인 「`base` ← R1 `branch` ← R2 `branch`」)   chains and branch names.
- 체크리스트 section: 「R1 — PSD-1 · `branch`」 then a ``` block of 「[ ] <commit title>」 lines (the 「[ ] 완료」 line is not a box);
  a 「검증 목표:」 list between that line and the next ticket's line is the ticket's verification goals.
- 「티켓 · 검증 목표」 table (e.g. 「티켓별 검증 목표」): one goal per ticket without a 「검증 목표:」 list.
- 테스트 케이스 section: TC rows, as tests/*.md.

A single-doc brief cannot point at per-ticket files, so read_allowance names what a ticket's agents may open besides its
own checklist and tree row: the sections its text cites (「(§11.2 F3)」), the test-case table rows of its TC IDs, its
rows of an 「인벤토리」 table, and its bold track block (a line starting 「**F4 …**」 inside a track section, up to the next
bold package block or heading) with the first paragraph of the section holding it (the rule shared by that section's
blocks) — heading names and line numbers only, the section text is not pasted into the brief.

Column positions differ between projects, so every table is read by its header names.
"""

import glob
import os
import re

from cbd.util import DriverError

DASH = {"", "—", "-", "–"}
SECTION_REF = re.compile(r"§\s*(\d+(?:\.\d+)*)(?:\s+([A-Z][A-Z0-9]*\d+[A-Za-z]?)(?![\w-]))?")
NUMBERED_HEADING = re.compile(r"^#+\s+((\d+(?:\.\d+)*)\.?\s+.*?)\s*$")
BLOCK_START = re.compile(r"^(\*\*([A-Z][A-Z0-9]*\d+[A-Za-z]?)(?=[\s`*·—(]).*?\*\*)")
LAYER_WORDS = ("unit", "integration", "storybook", "e2e", "request", "system", "component", "contract")


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def tables(text):
    """Every markdown table in text as (header cells, [row cells], heading above it)."""
    out, lines, heading = [], text.splitlines(), ""
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|\s*:?-{3}", lines[i + 1]):
            head, rows, i = _cells(line), [], i + 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            out.append((head, rows, heading))
            continue
        i += 1
    return out


def col(head, *names):
    """Index of the first header that starts with one of names, or None."""
    for n in names:
        for i, h in enumerate(head):
            if h == n or h.startswith(n + " ") or h.startswith(n + "(") or h.startswith(n):
                return i
    return None


def package_of(cell):
    """'F6 (M)' / 'S1 iBT 설정 키 검증 (A)' / '[S1](...)' -> 'F6' / 'S1' / 'S1'."""
    m = re.match(r"\[?([A-Z][A-Z0-9]*\d+[A-Za-z]?)\b", cell.strip())
    return m.group(1) if m else None


def packages_in(cell):
    if cell.strip() in DASH:
        return []
    return [p for p in (package_of(x) for x in re.split(r"[·,→/]|\s+", cell)) if p]


def is_dash(cell):
    """'—' / '— (브랜치 없음)' / '' -> True."""
    cell = strip_md(cell)
    return not cell or cell[0] in "—-–"


def section(text, words):
    """(heading, body) of the first heading containing words, up to the next heading of the same or higher level."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        h = re.match(r"^(#+)\s+(.*)$", line)
        if not h or words not in h.group(2):
            continue
        level, body = len(h.group(1)), []
        for nxt in lines[i + 1:]:
            n = re.match(r"^(#+)\s", nxt)
            if n and len(n.group(1)) <= level:
                break
            body.append(nxt)
        return h.group(2).strip(), "".join(body)
    return "", ""


GOALS_RE = re.compile(r"^(\s*)(- |\* )?\**검증 목표\**\s*[:：]\s*(.*)$")
ITEM_RE = re.compile(r"^(\s*)(?:[-*]|\d+[.)])\s+(.+?)\s*$")


def goals_in(text):
    """The 「검증 목표:」 list of a ticket's text -> [goal]. An inline remainder is the one goal (the lines after it are
    other fields); otherwise the list items under the line — indented ones when the marker is itself a list item,
    top-level ones too when it is not."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = GOALS_RE.match(line)
        if not m:
            continue
        indent, bullet = len(m.group(1)), bool(m.group(2))
        inline = m.group(3).strip()
        if inline:
            return [] if inline in DASH else [inline]
        out = []
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                if out:
                    break
                continue
            it = ITEM_RE.match(nxt)
            if not it or (bullet and len(it.group(1)) <= indent):
                break
            out.append(it.group(2))
        return out
    return []


CONTRACT_RE = re.compile(r"^\s*(?:[-*]\s+)?\**실서버 대조\**\s*[:：]\s*(.+?)\s*$")


def contract_in(text):
    """A 「실서버 대조: R1 — M4 · M5 (§7)」 line -> {"producers": [R1], "apis": "M4 · M5 (§7)"} (None without one).
    The packages before the dash are the producers whose verified head is checked; the rest names the APIs."""
    for line in text.splitlines():
        m = CONTRACT_RE.match(line)
        if not m or m.group(1).strip() in DASH:
            continue
        parts = re.split(r"\s+(?:—|--|-)\s+", m.group(1), maxsplit=1)
        head, apis = parts[0], (parts[1] if len(parts) > 1 else "")
        return {"producers": packages_in(head), "apis": (apis or m.group(1)).strip()}
    return None


def strip_md(cell):
    cell = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)
    return cell.replace("`", "").strip()


def expand_tcs(text, pattern):
    """'TC-A-01~TC-A-03 · TC-B-07' -> ['TC-A-01', 'TC-A-02', 'TC-A-03', 'TC-B-07'] (ranges include both ends)."""
    out = []
    rng = re.compile(rf"({pattern})\s*~\s*({pattern})")
    pos = 0
    for m in rng.finditer(text):
        out += re.findall(pattern, text[pos:m.start()])
        a, b = m.group(1), m.group(2)
        pa, pb = re.match(r"(.*?)(\d+)$", a), re.match(r"(.*?)(\d+)$", b)
        if pa and pb and pa.group(1) == pb.group(1):
            width = len(pa.group(2))
            out += [f"{pa.group(1)}{n:0{width}d}" for n in range(int(pa.group(2)), int(pb.group(2)) + 1)]
        else:
            out += [a, b]
        pos = m.end()
    out += re.findall(pattern, text[pos:])
    seen = set()
    return [t for t in out if not (t in seen or seen.add(t))]


class Box:
    def __init__(self, index, title, checked, note=""):
        self.index = index        # 1-based checkbox number within the package
        self.title = title        # the commit title, verbatim
        self.checked = checked
        self.note = note          # text after the closing backtick, e.g. '(데스크톱)'
        self.fix_source = ""      # 'integration' for the fix checkbox the ticket integration review added

    @property
    def prefix(self):
        m = re.match(r"([a-z]+)[(:!]", self.title)
        return m.group(1) if m else ""

    def __repr__(self):
        return f"Box({self.index}, {self.title[:40]!r})"


class Ticket:
    def __init__(self, package):
        self.package = package
        self.title = ""
        self.jira = ""
        self.repo = None
        self.base = None          # repo base ref when the ticket starts a chain, else None
        self.chain_prev = None
        self.predecessors = []
        self.pin = None
        self.branch = ""
        self.boxes = []
        self.criteria = ""        # 완료 기준 text from order.md
        self.index_section = ""   # the ticket's ### section of ticket-index.md (읽을 노드 목록) — goes into briefs
        self.index_tests = ""     # its 「테스트:」 line, the fallback when order.md has no 완료 기준
        self.goals = []           # 「검증 목표:」 items — what the ticket integration review checks the branch against
        self.contract = None      # 「실서버 대조:」 {"producers", "apis"} — the consumer start check (체크박스를 넘는 검증 §1)
        self.tcs = []
        self.verification_only = False  # single-doc 「—」 branch: a checklist of checks, not commits — never driven
        self.chain_position = 0
        self.chain_length = 0

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if k != "boxes"} | {"boxes": [b.title for b in self.boxes]}


class Design:
    def __init__(self, root, tc_pattern=r"TC-[A-Z0-9]+-\d+"):
        self.root = root
        self.tc_pattern = tc_pattern
        self.single_doc = not os.path.isfile(self.p("plan", "commits.md")) and os.path.isfile(self.p("design.md"))
        if not self.single_doc and not os.path.isfile(self.p("plan", "commits.md")):
            raise DriverError("착수 시트", f"설계 루트에 plan/commits.md 도 design.md 도 없다: {root}")
        self.tickets = {}
        self.order = []
        self.chains = {}           # repo -> [package] in chain order
        self.warnings = []
        self.notices = []          # printed by check without failing it (a ticket with no 검증 목표)
        self._tc_rows = None
        self._headings = None
        self._blocks = None
        if self.single_doc:
            self._read_single_doc()
        else:
            self._read_commits()
            self._read_index()
            self._read_jira_map()
            self._read_order()
        self._finish()

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def text(self, *parts):
        try:
            with open(self.p(*parts), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def ticket(self, package):
        if package not in self.tickets:
            self.tickets[package] = Ticket(package)
            self.order.append(package)
        return self.tickets[package]

    # ---- plan/commits.md
    def _read_commits(self):
        current = None
        for line in self.text("plan", "commits.md").splitlines():
            h = re.match(r"^##\s+(\S+)", line)
            if h:
                pkg = package_of(h.group(1))
                current = self.ticket(pkg) if pkg else None
                continue
            m = re.match(r"^- \[([ xX])\]\s+`(.+)`([^`]*)$", line)
            if m and current is not None:
                current.boxes.append(Box(len(current.boxes) + 1, m.group(2), m.group(1) != " ", m.group(3).strip()))

    # ---- design.md (single-doc form)
    def _read_single_doc(self):
        doc = self.text("design.md")
        order_heading, order_body = section(doc, "구현 순서")
        for head, rows, _ in tables(order_body):
            pi, di, ti = col(head, "패키지"), col(head, "선행"), col(head, "테스트", "완료 기준")
            if pi is None or di is None:
                continue
            for r in rows:
                pkg = package_of(r[pi]) if pi < len(r) else None
                if not pkg:
                    continue
                t = self.ticket(pkg)
                t.predecessors = packages_in(r[di]) if di < len(r) else []
                if ti is not None and ti < len(r) and not is_dash(r[ti]):
                    t.criteria = r[ti]
            break
        tree_rows = self._read_tree(doc)
        for pkg in self.order:
            if pkg not in tree_rows:
                self.warnings.append(f"{pkg}: 티켓 트리 표에 행이 없다")
        self._read_chain_table(doc)
        blocks, list_heading = self._read_checklists(doc)
        self._read_goal_table(doc)
        self._read_pins_from_boxes()
        for pkg, t in self.tickets.items():
            parts = [blocks.get(pkg, "")]
            if pkg in tree_rows:
                parts.append(tree_rows[pkg])
            src = " · ".join(f"「{h}」" for h in (list_heading, tree_rows.get("", ""), order_heading) if h)
            parts.append(f"정본: design.md — {src}")
            t.index_section = "\n\n".join(p for p in parts if p)
        tree_rows.pop("", None)

    def _read_pins_from_boxes(self):
        """single-doc has no 핀 column: a `chore` box 「…핀 → F2 브랜치 헤드」 names the producer whose verified head it pins."""
        for t in self.tickets.values():
            for b in t.boxes:
                m = re.search(r"(?:핀|\bpin\b)\s*(?:→|->)\s*(.+)$", b.title, re.I)
                if b.prefix != "chore" or not m:
                    continue
                pins = [p for p in packages_in(m.group(1)) if p in self.tickets and p != t.package]
                if pins and not t.pin:
                    t.pin = pins[0]

    def _read_goal_table(self, doc):
        """A 「티켓 · 검증 목표」 table (e.g. 「티켓별 검증 목표」): its cell is the goal of a ticket whose checklist has no
        「검증 목표:」 list. The 티켓 cell may start with the Jira key, as in the tree table."""
        for head, rows, _ in tables(doc):
            ci, gi = col(head, "티켓"), col(head, "검증 목표")
            if ci != 0 or gi is None:
                continue
            for r in rows:
                pkg = next((p for p in packages_in(strip_md(r[ci])) if p in self.tickets), None) if r else None
                if pkg in self.tickets and gi < len(r) and not is_dash(r[gi]) and not self.tickets[pkg].goals:
                    self.tickets[pkg].goals = [strip_md(r[gi])]

    def _read_tree(self, doc):
        """Rows of the 티켓 트리 table -> {package: 'heading + header + raw row'}, '' -> the table heading."""
        out = {}
        for head, rows, heading in tables(doc):
            ci, ri, bi = col(head, "티켓"), col(head, "저장소"), col(head, "브랜치")
            pi, ti, di = col(head, "베이스", "스택 부모"), col(head, "대상 테스트", "테스트"), col(head, "is blocked by", "선행")
            if ci != 0 or None in (ri, bi, di):
                continue
            out[""] = heading
            header = "| " + " | ".join(head) + " |"
            for r in rows:
                get = lambda i: r[i] if i is not None and i < len(r) else ""
                m = re.match(r"([A-Z][A-Z0-9]+-\d+)\s+(.*)$", strip_md(r[0]))
                jira, rest = (m.group(1), m.group(2)) if m else ("", strip_md(r[0]))
                pkg = package_of(rest)
                if not pkg:
                    continue
                t = self.ticket(pkg)
                t.jira = jira or t.jira
                t.title = re.sub(r"\s*\(§[^)]*\)\s*$", "", rest[len(pkg):]).strip() or t.title
                t.repo = None if is_dash(get(ri)) else strip_md(get(ri))
                if is_dash(get(bi)):
                    t.branch, t.verification_only = "", True
                else:
                    t.branch = strip_md(get(bi))
                parent = "" if is_dash(get(pi)) else strip_md(get(pi))
                parent_pkg = packages_in(parent)
                if parent_pkg:
                    t.chain_prev = parent_pkg[0]
                elif parent:
                    t.base = parent
                if ti is not None:
                    t.criteria = "" if is_dash(get(ti)) else get(ti)
                t.predecessors = packages_in(get(di))
                out[pkg] = f"{heading}:\n{header}\n| " + " | ".join(r) + " |"
            break
        return out

    def _read_chain_table(self, doc):
        for head, rows, _ in tables(doc):
            ri, chi = col(head, "저장소"), col(head, "체인")
            if ri is None or chi is None or col(head, "티켓") is not None:
                continue
            for r in rows:
                repo, cell = strip_md(r[ri]), r[chi] if chi < len(r) else ""
                segments = [x.strip() for x in re.split(r"←", cell)] if "←" in cell else [cell]
                chain, base = [], None
                for seg in segments:
                    pkg = package_of(strip_md(seg))
                    if not pkg:
                        base = base or strip_md(seg) or None
                        continue
                    chain.append(pkg)
                    t = self.ticket(pkg)
                    t.repo = t.repo or repo
                    bm = re.search(r"`([^`]+)`", seg)
                    if bm and not t.branch:
                        t.branch = bm.group(1)
                    elif bm and bm.group(1) != t.branch:
                        self.warnings.append(f"{pkg}: 트리 표 브랜치 {t.branch} 와 스택 체인 {bm.group(1)} 가 다르다")
                if chain:
                    self.chains[repo] = chain
                    head_ticket = self.tickets[chain[0]]
                    head_ticket.base = head_ticket.base or base
            break

    def _read_checklists(self, doc):
        """Boxes from the 체크리스트 section -> ({package: raw block}, heading)."""
        heading, body = section(doc, "체크리스트")
        blocks, current, start, fence, starts = {}, None, 0, False, []
        lines = body.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("```"):
                fence = not fence
                if not fence and current:
                    blocks[current] = "\n".join(lines[start:i + 1]).strip()
                    current = None
                continue
            if fence:
                m = re.match(r"^\s*(?:- )?\[([ xX])\]\s+(.+?)\s*$", line)
                if m and current and m.group(2) != "완료":
                    t = self.tickets[current]
                    t.boxes.append(Box(len(t.boxes) + 1, m.group(2), m.group(1) != " "))
                continue
            h = re.match(r"^([A-Z][A-Z0-9]*\d+[A-Za-z]?)\s+—\s+", line)
            if h:
                current, start = h.group(1), i
                starts.append((current, i))
                if current not in self.tickets:
                    self.warnings.append(f"{current}: 체크리스트에 있지만 구현 순서·트리 표에 없다")
                    self.ticket(current)
        for k, (pkg, s) in enumerate(starts):  # 검증 목표 lists sit between a ticket's line and the next one's
            region = lines[s + 1:starts[k + 1][1] if k + 1 < len(starts) else len(lines)]
            text = "\n".join(l for l in region if not l.strip().startswith("```") and not re.match(r"^\s*\[[ xX]\]", l))
            goals = goals_in(text)
            if goals:
                self.tickets[pkg].goals = goals
            self.tickets[pkg].contract = contract_in(text) or self.tickets[pkg].contract
        return blocks, heading

    # ---- plan/ticket-index.md
    def _read_index(self):
        text = self.text("plan", "ticket-index.md")
        for head, rows, _ in tables(text):
            if col(head, "티켓") != 0 or col(head, "선행") is None:
                continue
            ci = {k: col(head, *names) for k, names in {
                "jira": ("Jira",), "repo": ("저장소",), "pred": ("선행",), "prev": ("체인 앞",), "pin": ("핀",)}.items()}
            for r in rows:
                pkg = package_of(r[0])
                if not pkg:
                    continue
                t = self.ticket(pkg)
                get = lambda k: r[ci[k]] if ci[k] is not None and ci[k] < len(r) else ""
                if get("jira"):
                    m = re.search(r"[A-Z][A-Z0-9]+-\d+", get("jira"))
                    t.jira = m.group(0) if m else ""
                repo_cell = get("repo")
                if repo_cell and repo_cell not in DASH:
                    parts = [x.strip() for x in repo_cell.split("·", 1)]
                    t.repo = strip_md(parts[0]) or None
                    base = strip_md(parts[1]) if len(parts) > 1 else ""
                    if base and not re.search(r"\b위$", base):
                        t.base = base
                t.predecessors = packages_in(get("pred"))
                prev = packages_in(get("prev"))
                t.chain_prev = prev[0] if prev else None
                pins = packages_in(get("pin"))
                t.pin = pins[0] if pins else None
            break
        sections = list(re.finditer(r"^###\s+(\S+)\s*(.*)$", text, re.M))
        for i, m in enumerate(sections):
            pkg = package_of(m.group(1))
            if pkg not in self.tickets:
                continue
            t = self.tickets[pkg]
            t.title = t.title or m.group(2).strip()
            end = sections[i + 1].start() if i + 1 < len(sections) else len(text)
            body = text[m.end():end]
            t.index_section = body.strip()
            tm = re.search(r"^- 테스트:\s*(.+)$", body, re.M)
            t.index_tests = tm.group(1).strip() if tm else ""
            t.goals = goals_in(body)
            t.contract = contract_in(body)

    # ---- plan/jira-map.md
    def _read_jira_map(self):
        for head, rows, _ in tables(self.text("plan", "jira-map.md")):
            ki, ji, bi, ti = col(head, "키"), col(head, "Jira"), col(head, "브랜치"), col(head, "제목")
            if ki is None or ji is None:
                continue
            for r in rows:
                pkg = package_of(r[ki]) if ki < len(r) else None
                if not pkg or pkg not in self.tickets:
                    continue
                t = self.tickets[pkg]
                m = re.search(r"[A-Z][A-Z0-9]+-\d+", r[ji]) if ji < len(r) else None
                if m and not t.jira:
                    t.jira = m.group(0)
                if bi is not None and bi < len(r) and not t.branch:
                    t.branch = strip_md(r[bi]) if strip_md(r[bi]) not in DASH else ""
                if ti is not None and ti < len(r) and not t.title:
                    t.title = strip_md(r[ti])

    # ---- plan/order.md
    def _read_order(self):
        for head, rows, _ in tables(self.text("plan", "order.md")):
            pi, ci = col(head, "패키지"), col(head, "완료 기준")
            if pi is not None and ci is not None:
                for r in rows:
                    pkg = package_of(r[pi]) if pi < len(r) else None
                    if pkg in self.tickets and ci < len(r):
                        self.tickets[pkg].criteria = r[ci]
                continue
            ri, chi, bri = col(head, "저장소"), col(head, "체인"), col(head, "브랜치 이름")
            if ri is not None and chi is not None:
                for r in rows:
                    repo = strip_md(r[ri])
                    chain = packages_in(r[chi]) if chi < len(r) else []
                    if not chain:
                        continue
                    self.chains[repo] = chain
                    if bri is not None and bri < len(r):
                        names = [strip_md(x) for x in r[bri].split("·")]
                        for pkg, name in zip(chain, names):
                            if pkg in self.tickets and name and not self.tickets[pkg].branch:
                                self.tickets[pkg].branch = name

    def _finish(self):
        for t in self.tickets.values():
            if t.branch and t.jira:
                t.branch = t.branch.replace("<Jira>", t.jira).replace("<JiraKey>", t.jira)
            elif "<Jira>" in t.branch:
                self.warnings.append(f"{t.package}: Jira 키가 없어 브랜치 이름의 <Jira> 를 채우지 못함 ({t.branch})")
            t.tcs = self.criteria_tcs(t.criteria) if t.criteria else []
            if not t.tcs and t.index_tests:
                t.tcs = self.criteria_tcs(t.index_tests)
            if not t.boxes:
                self.warnings.append(f"{t.package}: {'체크리스트' if self.single_doc else 'commits.md'} 에 체크박스가 없다")
            elif not t.goals and not t.verification_only:
                where = "체크리스트의 티켓 줄 아래" if self.single_doc else "ticket-index 티켓 절"
                self.notices.append(f"{t.package}: 검증 목표가 없다 ({where} 「검증 목표:」) — 통합 리뷰는 기본 관점만 본다")
        # chains: order.md wins; otherwise follow 체인 앞 links from ticket-index
        if not self.chains:
            for t in self.tickets.values():
                if t.repo and not t.chain_prev:
                    chain, cur = [t.package], t.package
                    while True:
                        nxt = [x.package for x in self.tickets.values() if x.chain_prev == cur]
                        if not nxt:
                            break
                        cur = nxt[0]
                        chain.append(cur)
                    self.chains[t.repo] = chain
        for repo, chain in self.chains.items():
            for i, pkg in enumerate(chain):
                if pkg not in self.tickets:
                    continue
                t = self.tickets[pkg]
                t.chain_position, t.chain_length = i + 1, len(chain)
                if i and not t.chain_prev:
                    t.chain_prev = chain[i - 1]
                if t.repo is None:
                    t.repo = repo
        for t in self.tickets.values():
            for d in t.predecessors:
                if d not in self.tickets:
                    self.warnings.append(f"{t.package}: 선행 {d} 가 계획에 없다")
            if t.contract:
                bad = [p for p in t.contract["producers"] if p not in self.tickets or p == t.package]
                if bad or not t.contract["producers"]:
                    self.warnings.append(f"{t.package}: 실서버 대조의 생산자 {', '.join(bad) or '(없음)'} 가 계획에 없다 "
                                         "(「실서버 대조: <생산자> — <API>」)")

    # ---- tests/*.md
    def tc_rows(self):
        """{tc: {'layer', 'file', 'row'}} from every table in tests/*.md that has an ID or TC column."""
        if self._tc_rows is not None:
            return self._tc_rows
        rows = {}
        for stem, text, top in self._tc_sources():
            for head, body, heading in tables(text):
                idc = col(head, "ID", "TC")
                if idc is None:
                    continue
                lc = col(head, "층")
                hl = next((w for w in LAYER_WORDS if w in heading.lower()), "")
                if not hl and (col(head, "스토리") is not None or "스토리" in heading):
                    hl = "storybook"
                for r in body:
                    for tc in re.findall(self.tc_pattern, r[idc] if idc < len(r) else ""):
                        layer = (r[lc].strip().lower() if lc is not None and lc < len(r) else "") or hl
                        rows.setdefault(tc, {"layer": layer, "file": stem, "heading": heading or top,
                                             "row": "| " + " | ".join(r) + " |"})
        self._tc_rows = rows
        return rows

    def _tc_sources(self):
        """(stem, text, heading) of every TC document: tests/*.md, or the 테스트 케이스 section of design.md."""
        if self.single_doc:
            yield ("design", *reversed(section(self.text("design.md"), "테스트 케이스")))
            return
        for path in sorted(glob.glob(self.p("tests", "*.md"))):
            with open(path, encoding="utf-8") as f:
                stem = os.path.splitext(os.path.basename(path))[0]
                yield stem, f.read(), f"tests/{stem}.md"

    # ---- what a single-doc brief may open besides the ticket's own text
    def numbered_headings(self):
        """{'11.2': '11.2 front-common (presentational only)'} — the numbered headings of design.md (fences skipped)."""
        if self._headings is None:
            self._headings, fence = {}, False
            for line in self.text("design.md").splitlines():
                if line.strip().startswith("```"):
                    fence = not fence
                    continue
                m = None if fence else NUMBERED_HEADING.match(line)
                if m:
                    self._headings.setdefault(m.group(2), m.group(1))
        return self._headings

    def track_blocks(self):
        """{'F4': [{'heading', 'title', 'line', 'end', 'para'}]} — bold package blocks of design.md: a line starting
        「**<PKG> …**」 (outside code fences) up to the line before the next such block or heading. `heading` is the
        heading the block sits under, `para` the line of that section's first paragraph when the section opens with
        prose (its common rule) rather than a block, table or code (None otherwise). Line numbers are 1-based."""
        if self._blocks is not None:
            return self._blocks
        self._blocks, fence, heading, para, cur = {}, False, "", None, None
        lines = self.text("design.md").splitlines()

        def close(end):
            if cur:
                cur["end"] = end
        first = True                         # the next non-blank line is the first one under the heading
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("```"):
                fence = not fence
                first = False
                continue
            if fence:
                continue
            h = re.match(r"^#+\s+(.*?)\s*$", line)
            if h:
                close(i - 1)
                cur, heading, para, first = None, h.group(1), None, True
                continue
            b = BLOCK_START.match(line)
            if b:
                close(i - 1)
                cur = {"heading": heading, "title": b.group(1), "line": i, "end": len(lines), "para": para}
                self._blocks.setdefault(b.group(2), []).append(cur)
                first = False
                continue
            if first and stripped:
                first = False
                if not stripped.startswith(("|", "<")):
                    para = i
        close(len(lines))
        for blocks in self._blocks.values():   # trailing blank lines are not part of a block
            for blk in blocks:
                while blk["end"] > blk["line"] and not lines[blk["end"] - 1].strip():
                    blk["end"] -= 1
        return self._blocks

    def read_allowance(self, ticket, titles=()):
        """Single-doc only: [line] naming the parts of design.md the ticket's agents may read besides its checklist and
        tree row — (a) every 「§N」/「§N.M」 its text, checklist titles and `titles` cite, (b) the test-case table rows
        of its TC IDs, (c) its rows of an 「인벤토리」 table, (d) its bold track blocks 「**<PKG> …**」 and (e) the first
        paragraph of the section holding each block. Heading names and line numbers only (the brief stays small);
        [] otherwise."""
        if not self.single_doc:
            return []
        out, heads = [], self.numbered_headings()
        text = "\n".join([ticket.index_section or "", *(b.title for b in ticket.boxes), *titles])
        refs = list(dict.fromkeys((m.group(1), m.group(2) or "") for m in SECTION_REF.finditer(text)))
        for num, part in refs:
            if part and (num, "") in refs:
                continue
            cite = f"§{num}" + (f" {part}" if part else "")
            out.append(f"「{heads[num]}」" + (f" 중 {part} 부분" if part else "") + f" ({cite})" if num in heads else
                       f"{cite} — design.md 에 이 번호의 머리글이 없다")
        for blk in self.track_blocks().get(ticket.package, []):
            where = f"「{blk['heading']}」" if blk["heading"] else "design.md"
            out.append(f"{where} 의 {blk['title']} 블록 (:{blk['line']} 부터 다음 굵은 패키지 블록·머리글 전까지)")
            if blk["para"]:
                para = f"{where} 첫 문단 (:{blk['para']} — 이 절 패키지 블록의 공통 규칙)"
                if para not in out:
                    out.append(para)
        tcs = list(dict.fromkeys([*ticket.tcs, *expand_tcs(text, self.tc_pattern)]))
        rows, by_heading = self.tc_rows(), {}
        for tc in tcs:
            if tc in rows:
                by_heading.setdefault(rows[tc]["heading"], []).append(tc)
        for heading, ids in by_heading.items():
            out.append(f"「{heading}」 표의 {' · '.join(ids)} 행 (테스트 케이스)")
        mark = re.compile(rf"(?<![A-Za-z0-9]){re.escape(ticket.package)}(?![\w-])")
        for head, body, heading in tables(self.text("design.md")):
            if "인벤토리" not in heading:
                continue
            ci = col(head, "패키지", "티켓")
            ci = 0 if ci is None else ci
            keys = [r[0] for r in body if ci < len(r) and mark.search(strip_md(r[ci]))]
            if keys:
                out.append(f"「{heading}」 표의 {ticket.package} 행 — " + " / ".join(keys))
        return out

    def criteria_tcs(self, criteria):
        """TC IDs of a 완료 기준 cell, including 「tests/M 의 storybook 층 TC-M-* 전부」 style layer phrases."""
        out = expand_tcs(criteria, self.tc_pattern)
        for m in re.finditer(r"tests/(\w+)\]?(?:\([^)]*\))?\s*의\s+([\w·]+)\s*층\s+(\S+?)\*?\s*전부", criteria):
            stem, layers = m.group(1), [x.strip().lower() for x in m.group(2).split("·")]
            prefix = m.group(3).rstrip("*")
            for tc, info in self.tc_rows().items():
                if info["file"] == stem and tc.startswith(prefix) and info["layer"] in layers and tc not in out:
                    out.append(tc)
        if re.search(r"층\s+\S+\s*전부", criteria) and not out:
            self.warnings.append(f"완료 기준의 층 표기를 TC 로 풀지 못함: {criteria[:80]}")
        return out
