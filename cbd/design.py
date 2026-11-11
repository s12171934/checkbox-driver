"""Reads the design graph a project is built from.

Inputs (all under <design_root>):
- plan/commits.md       ## <PKG> sections of "- [ ] `<commit title>`" lines — one checkbox = one commit.
- plan/ticket-index.md  요약표 (티켓 · Jira? · 저장소 · 베이스 · 층 · 선행 · 체인 앞 · 핀) and per-ticket 「테스트:」 lines.
- plan/jira-map.md      트리 표 (키 · Jira · 브랜치?) — the Jira key of each package.
- plan/order.md         완료 기준 tables and the 스택 체인 table (branch names per repo, in chain order).
- tests/*.md            TC rows (ID or TC column, optional 층 column), used to expand layer-named criteria.

Column positions differ between projects, so every table is read by its header names.
"""

import glob
import os
import re

from cbd.util import DriverError

DASH = {"", "—", "-", "–"}
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
        self.tcs = []
        self.chain_position = 0
        self.chain_length = 0

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if k != "boxes"} | {"boxes": [b.title for b in self.boxes]}


class Design:
    def __init__(self, root, tc_pattern=r"TC-[A-Z0-9]+-\d+"):
        self.root = root
        self.tc_pattern = tc_pattern
        if not os.path.isfile(self.p("plan", "commits.md")):
            raise DriverError("착수 시트", f"설계 루트에 plan/commits.md 가 없다: {root}")
        self.tickets = {}
        self.order = []
        self.chains = {}           # repo -> [package] in chain order
        self.warnings = []
        self._tc_rows = None
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
                self.warnings.append(f"{t.package}: commits.md 에 체크박스가 없다")
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

    # ---- tests/*.md
    def tc_rows(self):
        """{tc: {'layer', 'file', 'row'}} from every table in tests/*.md that has an ID or TC column."""
        if self._tc_rows is not None:
            return self._tc_rows
        rows = {}
        for path in sorted(glob.glob(self.p("tests", "*.md"))):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            stem = os.path.splitext(os.path.basename(path))[0]
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
                        rows.setdefault(tc, {"layer": layer, "file": stem, "row": "| " + " | ".join(r) + " |"})
        self._tc_rows = rows
        return rows

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
