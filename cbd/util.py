"""Small helpers shared by every module: time, atomic JSON files, subprocess calls."""

import datetime
import json
import os
import subprocess


class DriverError(Exception):
    """A stop the driver cannot resolve by itself. `kind` is one of EXCEPTION_KINDS."""

    def __init__(self, kind, message):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind
        self.message = message


# The categories a ticket can stop on (state.md 「미해결 예외」, plan.json status blocked).
EXCEPTION_KINDS = ("정체", "검증 실패", "선행 대기", "환경", "착수 시트", "연결 불명", "인증", "리뷰어 불가",
                   "중복 기동", "리베이스 충돌", "사람 승인", "계약 불일치")


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def now_ts():
    return datetime.datetime.now().timestamp()


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    """Write through a temp file and rename, so a reader never sees half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def run(args, cwd=None, timeout=600, env=None, check=False):
    """Run a command (argument list, never a shell string) and return the CompletedProcess."""
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, **(env or {})})
    if check and r.returncode != 0:
        raise DriverError("환경", f"{' '.join(args[:3])} 실패 (exit {r.returncode}): {(r.stderr or r.stdout).strip()[:300]}")
    return r


def git(path, *args, timeout=120, check=True):
    r = run(["git", "-C", path, *args], timeout=timeout)
    if check and r.returncode != 0:
        raise DriverError("환경", f"git {' '.join(args[:2])} 실패: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()
