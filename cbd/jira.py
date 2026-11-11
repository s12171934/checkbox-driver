"""Jira updates the driver makes: tick a checklist item, move the status. Optional — no `jira` config, no calls.

config.json "jira": {"site": "x.atlassian.net", "email": "...", "token_keychain_service": "..." (or env
ATLASSIAN_API_TOKEN), "progress_status": "진행 중", "done_status": "완료"}

Checklist = the ticket description's ADF taskList (jira-map: 체크리스트는 commits 목록 그대로). An item matches a
checkbox when its text, stripped of backticks and spaces, equals or contains the commit title.
"""

import base64
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cbd.util import DriverError, run


class Jira:
    def __init__(self, cfg):
        self.c = cfg["jira"] or {}

    @property
    def enabled(self):
        return bool(self.c.get("site") and self.c.get("email"))

    def _token(self):
        t = os.environ.get("ATLASSIAN_API_TOKEN")
        svc = self.c.get("token_keychain_service")
        if not t and svc:
            t = run(["security", "find-generic-password", "-s", svc, "-w"]).stdout.strip()
        if not t:
            raise DriverError("인증", "Jira 토큰 없음 — ATLASSIAN_API_TOKEN 또는 jira.token_keychain_service")
        return t

    def call(self, method, path, body=None):
        site = self.c["site"].removeprefix("https://").rstrip("/")
        auth = base64.b64encode(f"{self.c['email']}:{self._token()}".encode()).decode()
        req = Request(f"https://{site}/{path.lstrip('/')}", method=method,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": "Basic " + auth, "Accept": "application/json",
                               **({"Content-Type": "application/json"} if body is not None else {})})
        try:
            with urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except HTTPError as e:
            kind = "인증" if e.code in (401, 403) else "환경"
            raise DriverError(kind, f"Jira {method} {path} → HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
        except URLError as e:
            raise DriverError("연결 불명", f"Jira {method} {path}: {e}")

    def transition(self, key, name):
        if not self.enabled or not key or not name:
            return False
        cur = self.call("GET", f"rest/api/3/issue/{key}?fields=status")["fields"]["status"]["name"]
        if cur == name:
            return False
        for t in self.call("GET", f"rest/api/3/issue/{key}/transitions").get("transitions") or []:
            if t.get("name") == name or (t.get("to") or {}).get("name") == name:
                self.call("POST", f"rest/api/3/issue/{key}/transitions", {"transition": {"id": t["id"]}})
                return True
        raise DriverError("환경", f"{key} 에 「{name}」 전이가 없다 (지금 {cur})")

    def check_box(self, key, title, done=True):
        """Tick the checklist item of `title`. Returns False when there is no matching item."""
        if not self.enabled or not key:
            return False
        desc = self.call("GET", f"rest/api/3/issue/{key}?fields=description")["fields"].get("description")
        if not desc:
            return False
        want = _norm(title)
        hit = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "taskItem":
                    text = _norm("".join(_texts(node)))
                    if text and (text == want or want in text):
                        hit.append(node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(desc)
        if not hit:
            return False
        state = "DONE" if done else "TODO"
        if hit[0].setdefault("attrs", {}).get("state") == state:
            return True
        hit[0]["attrs"]["state"] = state
        self.call("PUT", f"rest/api/3/issue/{key}", {"fields": {"description": desc}})
        return True

    def add_box(self, key, title):
        """Append a checklist item (a fix box added after the start)."""
        if not self.enabled or not key:
            return False
        desc = self.call("GET", f"rest/api/3/issue/{key}?fields=description")["fields"].get("description") or \
            {"type": "doc", "version": 1, "content": []}
        lists = [n for n in desc.get("content", []) if n.get("type") == "taskList"]
        item = {"type": "taskItem", "attrs": {"localId": f"cbd-{abs(hash(title)) % 10 ** 9}", "state": "TODO"},
                "content": [{"type": "text", "text": title}]}
        if lists:
            lists[-1]["content"].append(item)
        else:
            desc["content"].append({"type": "taskList", "attrs": {"localId": "cbd-list"}, "content": [item]})
        self.call("PUT", f"rest/api/3/issue/{key}", {"fields": {"description": desc}})
        return True


def _texts(node):
    if isinstance(node, dict):
        if node.get("type") == "text":
            yield node.get("text", "")
        for v in node.get("content") or []:
            yield from _texts(v)


def _norm(s):
    return re.sub(r"[\s`]+", "", s or "")
