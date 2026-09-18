#!/usr/bin/env python3
"""check_merge.py — 分支併入 main 前的合併門檻檢查。

判定三級：
    BLOCK  不可合併（CI 會掛、機密外洩、或有衝突）
    WARN   可合併，但需人工確認（commit 格式、log.md、文件同步、工作目錄…）
    PASS   全部符合要求

用法:
    python .claude/skills/pre-merge-check/scripts/check_merge.py
    python .claude/skills/pre-merge-check/scripts/check_merge.py feature/x
    python .claude/skills/pre-merge-check/scripts/check_merge.py feature/x --base main --no-ci
    python .claude/skills/pre-merge-check/scripts/check_merge.py --out 報告路徑.md --json

離開碼: PASS=0, WARN=1, BLOCK=2, 執行錯誤=3
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
RULES_PATH = SKILL_DIR / "references" / "gate_rules.json"
TEMPLATE_PATH = SKILL_DIR / "templates" / "merge_report.md.template"

RESULT_TEXT = {
    "BLOCK": "不可合併，先清掉 BLOCK 項目",
    "WARN": "可以合併，但下列項目需人工確認",
    "PASS": "符合要求，可以合併",
}

REPO = Path.cwd()


def die(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)
    raise SystemExit(3)


def run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            cwd=str(REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"找不到執行檔: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"逾時（{timeout}s）: {' '.join(cmd)}")


def git(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    # core.quotepath=false：不要把中文檔名（啟動.bat…）跳脫成 \345\225\237，否則 pattern 全比不到
    return run(["git", "-c", "core.quotepath=false", *args], timeout=timeout)


def unquote_path(path: str) -> str:
    """還原 git 在特殊字元時仍會加的雙引號與八進位跳脫。"""
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        inner = path[1:-1]
        try:
            return (
                inner.encode("latin-1").decode("unicode_escape").encode("latin-1").decode("utf-8")
            )
        except (UnicodeDecodeError, UnicodeEncodeError):
            return inner
    return path


# --------------------------------------------------------------------------
# 檢查結果
# --------------------------------------------------------------------------
class Finding:
    def __init__(
        self,
        key: str,
        title: str,
        status: str,
        detail: str,
        evidence: list[str] | None = None,
        fix: str = "",
    ) -> None:
        self.key = key
        self.title = title
        self.status = status  # PASS / WARN / BLOCK / SKIP
        self.detail = detail
        self.evidence = evidence or []
        self.fix = fix

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
            "fix": self.fix,
        }


def trim(lines: list[str], limit: int = 25) -> list[str]:
    if len(lines) <= limit:
        return lines
    return lines[:limit] + [f"...（另有 {len(lines) - limit} 筆，已省略）"]


# --------------------------------------------------------------------------
# 路徑比對
# --------------------------------------------------------------------------
def match_pattern(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path == pattern.rstrip("/") or path.startswith(pattern)
    return path == pattern or fnmatch.fnmatch(path, pattern)


# --------------------------------------------------------------------------
# git 資料收集
# --------------------------------------------------------------------------
def changed_files(merge_base: str, branch: str) -> list[tuple[str, str]]:
    proc = git("diff", "--name-status", "-M", f"{merge_base}..{branch}")
    if proc.returncode != 0:
        die(f"取得變更檔案失敗：{proc.stderr.strip()}")
    out: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        path = unquote_path(parts[-1])  # rename 取新路徑
        out.append((status, path))
    return out


def branch_commits(merge_base: str, branch: str) -> list[tuple[str, str]]:
    sep = "\x1f"
    proc = git("log", f"--format=%h{sep}%s", f"{merge_base}..{branch}")
    if proc.returncode != 0:
        die(f"取得 commit 列表失敗：{proc.stderr.strip()}")
    commits = []
    for line in proc.stdout.splitlines():
        if sep in line:
            sha, subject = line.split(sep, 1)
            commits.append((sha, subject))
    return commits


def added_lines(merge_base: str, branch: str, paths: list[str] | None = None):
    """逐行產出 (檔案, 行號, 內容) —— 只看新增的行。"""
    cmd = ["diff", "--no-color", f"{merge_base}..{branch}"]
    if paths:
        cmd += ["--", *paths]
    proc = git(*cmd)
    if proc.returncode != 0:
        return
    cur_file, cur_line = "", 0
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            cur_file = unquote_path(line[6:])
            continue
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            cur_line = int(m.group(1)) if m else 0
            continue
        if line.startswith("+") and not line.startswith("+++"):
            yield cur_file, cur_line, line[1:]
            cur_line += 1
        elif not line.startswith("-") and not line.startswith("\\"):
            cur_line += 1


# --------------------------------------------------------------------------
# 個別檢查
# --------------------------------------------------------------------------
def resolve_base(base: str, do_fetch: bool) -> tuple[str, Finding]:
    """目標分支一律以 origin 的為準。

    本機 main 常常落後遠端好幾個 commit，拿它當基準會漏掉真正會發生的衝突。
    """
    remote = f"origin/{base}"
    if do_fetch:
        git("fetch", "origin", base, timeout=600)

    has_remote = git("rev-parse", "--verify", "--quiet", remote).returncode == 0
    has_local = git("rev-parse", "--verify", "--quiet", base).returncode == 0
    if not has_remote and not has_local:
        die(f"找不到目標分支：{base}（本機與 origin 都沒有）")

    if not has_remote:
        return base, Finding(
            "base.ref",
            "比較基準",
            "WARN",
            f"`{remote}` 不存在，只能拿本機 `{base}` 當基準；若它已落後，衝突可能驗不出來。",
            fix=f"確認 remote 設定，或先 `git fetch origin {base}`。",
        )

    detail = f"以 `{remote}` 為基準。"
    if has_local:
        out = git("rev-list", "--count", f"{base}..{remote}").stdout.strip()
        behind = int(out) if out.isdigit() else 0
        if behind:
            detail += f"（本機 `{base}` 落後 {behind} 個 commit，已自動改用遠端，未受影響）"
    detail += (
        "" if do_fetch else "　未加 --fetch，這份 origin 快照的新舊取決於你上次 fetch 的時間。"
    )
    return remote, Finding("base.ref", "比較基準", "PASS", detail)


def check_worktree(branch: str) -> Finding:
    """工作目錄髒的時候，CI 檢查驗的是工作目錄，不是那個 commit —— 不可判 PASS。"""
    dirty = [ln for ln in git("status", "--porcelain").stdout.splitlines() if ln.strip()]
    if not dirty:
        return Finding("ci.worktree", "工作目錄狀態", "PASS", "乾淨，CI 結果就是該 commit 的結果。")
    return Finding(
        "ci.worktree",
        "工作目錄狀態",
        "WARN",
        f"有 {len(dirty)} 項未提交變更，下面的 ruff／pytest 驗的是工作目錄內容，"
        f"不等於 `{branch}` 這個 commit 的結果。",
        evidence=trim(dirty, 15),
        fix="把變更 commit 或 stash 後重跑，才能把 CI 結果當成這個 commit 的驗收證據。",
    )


def check_conflict(base: str, branch: str) -> Finding:
    proc = git("merge-tree", "--write-tree", base, branch)
    if proc.returncode == 0:
        return Finding("merge.conflict", "與 main 的合併衝突", "PASS", "可乾淨合併，無衝突。")
    stderr = proc.stderr.lower()
    if "unknown option" in stderr or "usage:" in stderr:
        mb = git("merge-base", base, branch).stdout.strip()
        legacy = git("merge-tree", mb, base, branch)
        conflicts = [ln for ln in legacy.stdout.splitlines() if ln.startswith("<<<<<<<")]
        if conflicts:
            return Finding(
                "merge.conflict",
                "與 main 的合併衝突",
                "BLOCK",
                f"預演合併發現 {len(conflicts)} 處衝突。",
                fix=f"先在分支上 `git merge {base}` 解掉衝突，再重跑檢查。",
            )
        return Finding("merge.conflict", "與 main 的合併衝突", "PASS", "可乾淨合併，無衝突。")

    lines = proc.stdout.splitlines()
    messages = [ln for ln in lines if ln.startswith(("CONFLICT", "Auto-merging"))]
    # merge-tree --write-tree 的衝突暫存行格式：<mode> <oid> <stage>\t<path>
    staged = sorted(
        {
            unquote_path(ln.split("\t", 1)[1])
            for ln in lines
            if "\t" in ln and re.match(r"^\d{6} [0-9a-f]{40} [123]\t", ln)
        }
    )
    count = len(staged) or len(messages)
    return Finding(
        "merge.conflict",
        "與 main 的合併衝突",
        "BLOCK",
        f"預演合併有 {count} 個檔案衝突，無法自動合併。"
        if count
        else "預演合併有衝突，無法自動合併。",
        evidence=trim([f"衝突檔案：{p}" for p in staged] + messages) or trim(lines),
        fix=f"先在分支上 `git merge {base}` 解掉衝突並提交，再重跑檢查。",
    )


def check_behind(base: str, branch: str) -> Finding:
    proc = git("rev-list", "--count", f"{branch}..{base}")
    if proc.returncode != 0 or not proc.stdout.strip().isdigit():
        return Finding(
            "merge.behind",
            "與 main 的同步狀態",
            "WARN",
            "無法判定是否落後目標分支。",
            evidence=trim((proc.stderr or proc.stdout).splitlines()),
            fix=f"確認 `{base}` 存在且已 fetch：`git fetch origin {base}`。",
        )
    behind = int(proc.stdout.strip())
    if behind == 0:
        return Finding(
            "merge.behind", "與 main 的同步狀態", "PASS", f"已包含 {base} 的最新 commit。"
        )
    return Finding(
        "merge.behind",
        "與 main 的同步狀態",
        "WARN",
        f"落後 {base} {behind} 個 commit，合併前的驗收證據不代表合併後的結果。",
        fix=f"在分支上 `git merge {base}`（或 rebase）同步後重跑一次全套測試。",
    )


def check_forbidden(files: list[str], rules: dict) -> Finding:
    hits = []
    for path in files:
        for rule in rules["forbidden"]:
            if match_pattern(path, rule["pattern"]):
                hits.append(f"{path}  ←  {rule['reason']}")
                break
    if hits:
        return Finding(
            "git.artifacts",
            "禁止進版控的檔案",
            "BLOCK",
            f"有 {len(hits)} 個不該進 Git 的檔案。",
            evidence=trim(hits),
            fix="`git rm --cached <檔案>` 移出版控，確認 .gitignore 已涵蓋，再重寫這些 commit。",
        )
    return Finding(
        "git.artifacts", "禁止進版控的檔案", "PASS", "沒有機密、執行期資料或快取檔進入版控。"
    )


def check_large(files: list[str], branch: str, rules: dict) -> Finding:
    limit = int(rules.get("max_file_bytes", 5 * 1024 * 1024))
    big = []
    for path in files:
        proc = git("cat-file", "-s", f"{branch}:{path}")
        if proc.returncode != 0:
            continue
        try:
            size = int(proc.stdout.strip())
        except ValueError:
            continue
        if size > limit:
            big.append(f"{path}  {size / 1024 / 1024:.1f} MB")
    if big:
        return Finding(
            "git.large",
            "大型檔案",
            "BLOCK",
            f"有 {len(big)} 個檔案超過 {limit / 1024 / 1024:.0f} MB。",
            evidence=trim(big),
            fix="大型原始資料與快照走 GitHub Releases，不進 Git；移出後重寫 commit。",
        )
    return Finding(
        "git.large", "大型檔案", "PASS", f"沒有超過 {limit / 1024 / 1024:.0f} MB 的檔案。"
    )


ALLOW_MARKER = "pre-merge-check: allow-secret"


def check_secrets(merge_base: str, branch: str, rules: dict) -> Finding:
    patterns = [(p["name"], re.compile(p["regex"])) for p in rules["secret_patterns"]]
    allow = [re.compile(a["regex"]) for a in rules.get("secret_allowlist", [])]
    hits, allowed = [], 0
    for path, lineno, text in added_lines(merge_base, branch):
        if path.endswith(".lock") or path.startswith("tests/"):
            continue
        if not any(regex.search(text) for _, regex in patterns):
            continue
        if ALLOW_MARKER in text or any(regex.search(text) for regex in allow):
            allowed += 1
            continue
        name = next(n for n, regex in patterns if regex.search(text))
        hits.append(f"{path}:{lineno}  疑似 {name}")

    note = f"（另有 {allowed} 處命中允許清單，已排除）" if allowed else ""
    if hits:
        return Finding(
            "git.secrets",
            "機密字串外洩",
            "BLOCK",
            f"新增的內容有 {len(hits)} 處疑似憑證{note}。報告只記位置，不記內容。",
            evidence=trim(hits),
            fix="移除硬編碼憑證改讀環境變數，**並視為已外洩立刻換發金鑰**，再重寫含機密的 commit。"
            f"確認是刻意保留的值，就加進 references/gate_rules.json 的 secret_allowlist，"
            f"或在該行加註解 `{ALLOW_MARKER}`。",
        )
    return Finding("git.secrets", "機密字串外洩", "PASS", f"新增內容沒有比對到憑證樣式{note}。")


def check_whitespace(merge_base: str, branch: str) -> Finding:
    proc = git("diff", "--check", merge_base, branch)
    if proc.returncode == 0:
        return Finding("git.whitespace", "空白字元檢查", "PASS", "`git diff --check` 通過。")
    return Finding(
        "git.whitespace",
        "空白字元檢查",
        "BLOCK",
        "`git diff --check` 有行尾空白或空白錯誤，D 的驗收清單要求必須乾淨。",
        evidence=trim(proc.stdout.splitlines()),
        fix="清掉行尾空白後 amend 或另開一個 commit。",
    )


def check_commit_format(commits: list[tuple[str, str]], rules: dict) -> Finding:
    types = "|".join(rules["conventional_commit_types"])
    regex = re.compile(rf"^({types})(\([^)]+\))?!?: .+")
    bad = [
        f"{sha}  {subject}"
        for sha, subject in commits
        if not subject.startswith("Merge ") and not regex.match(subject)
    ]
    if bad:
        return Finding(
            "commit.format",
            "Commit message 格式",
            "WARN",
            f"{len(bad)}／{len(commits)} 個 commit 不符 Conventional Commits。",
            evidence=trim(bad),
            fix="改寫成 `<type>: <說明>`，type 限 "
            f"{', '.join(rules['conventional_commit_types'])}；"
            "歷史上的 main 都是這個格式。",
        )
    return Finding(
        "commit.format",
        "Commit message 格式",
        "PASS",
        f"{len(commits)} 個 commit 都符合 Conventional Commits。",
    )


def check_log_checkpoint(files: list[str], merge_base: str, branch: str) -> Finding:
    if "log.md" not in files:
        return Finding(
            "log.checkpoint",
            "log.md 儲存點紀錄",
            "WARN",
            "這個分支沒有更新 log.md。",
            fix="依 docs/TEAM_4_ROLES.md，commit 前必須在 log.md 新增本次 checkpoint，"
            "記錄狀態、驗收證據與精確回退方式。",
        )
    added = [text for _, _, text in added_lines(merge_base, branch, ["log.md"])]
    body = "\n".join(added)
    has_heading = any(re.match(r"##\s*CP-", line.strip()) for line in added)
    missing = [k for k in ("時間", "狀態", "回退方式") if k not in body]
    if not has_heading or missing:
        gaps = []
        if not has_heading:
            gaps.append("沒有新的 `## CP-xxx` 標題")
        if missing:
            gaps.append("缺少欄位：" + "、".join(missing))
        return Finding(
            "log.checkpoint",
            "log.md 儲存點紀錄",
            "WARN",
            "log.md 有改動，但不是完整的 checkpoint（" + "；".join(gaps) + "）。",
            evidence=trim(added, 12),
            fix="比照 log.md 既有 CP 區塊補齊：`## CP-xxx`、時間、狀態、驗收證據、回退方式。",
        )
    return Finding("log.checkpoint", "log.md 儲存點紀錄", "PASS", "已新增完整的 checkpoint 區塊。")


def check_tests_touched(files: list[str]) -> Finding:
    src = [p for p in files if p.startswith("src/") and p.endswith(".py")]
    tests = [p for p in files if p.startswith("tests/")]
    if src and not tests:
        return Finding(
            "test.coupling",
            "測試同步",
            "WARN",
            f"改了 {len(src)} 個 src/ 模組，但沒有任何 tests/ 變更。",
            evidence=trim(src),
            fix="高風險的資料切換、auth、語料發布與稽核改動必須補負向、競態或故障注入測試；"
            "純重構請說明為什麼既有測試已足夠。",
        )
    return Finding("test.coupling", "測試同步", "PASS", "程式與測試一起變更，或這次沒有動到 src/。")


def check_doc_sync(files: list[str], rules: dict) -> Finding:
    gaps = []
    for rule in rules.get("doc_sync", []):
        touched = [p for p in files if any(match_pattern(p, w) for w in rule["when"])]
        if not touched:
            continue
        if any(any(match_pattern(p, e) for e in rule["expect_any"]) for p in files):
            continue
        gaps.append(
            f"{'、'.join(touched[:3])} 有變更，但 {'／'.join(rule['expect_any'])} 都沒更新"
            f" —— {rule['reason']}"
        )
    if gaps:
        return Finding(
            "doc.sync",
            "文件同步",
            "WARN",
            f"有 {len(gaps)} 項行為變更沒有對應的文件更新。",
            evidence=gaps,
            fix="同步 README.md／docs/SERVING.md／src/serving/API_CONTRACT.md；"
            "若行為其實沒變，在合併說明中註明。",
        )
    return Finding("doc.sync", "文件同步", "PASS", "行為變更與文件同步沒有落差。")


# --------------------------------------------------------------------------
# CI 等價驗收
# --------------------------------------------------------------------------
def ci_findings(skip_reason: str | None) -> list[Finding]:
    specs = [
        ("ci.format", "ruff format --check", ["ruff", "format", "--check", "."]),
        ("ci.lint", "ruff check", ["ruff", "check", "."]),
        ("ci.test", "pytest", ["pytest", "-q"]),
    ]
    if skip_reason:
        out = [
            Finding(
                key,
                title,
                "SKIP",
                f"未執行：{skip_reason}",
                fix=f"`git switch <分支>` 後重跑本檢查，或在該分支上直接執行 `{' '.join(cmd)}`。",
            )
            for key, title, cmd in specs
        ]
        out.append(Finding("ci.js", "node --check app.js", "SKIP", f"未執行：{skip_reason}"))
        return out

    uv = shutil.which("uv")
    prefix = [uv, "run"] if uv else [sys.executable, "-m"]

    # 把實際跑的 ruff 版本寫進報告：本機版本與 pyproject 的 pin 不同時，
    # 會出現 CI 不會有的 BLOCK，看得到版本才查得出來
    ver = run([*prefix, "ruff", "--version"], timeout=120).stdout.strip()
    ver_note = f"（{ver}）" if ver.startswith("ruff") else ""

    findings = []
    for key, title, cmd in specs:
        proc = run([*prefix, *cmd])
        note = ver_note if cmd[0] == "ruff" else ""
        if proc.returncode == 0:
            findings.append(Finding(key, title, "PASS", f"通過。{note}"))
        else:
            tail = (proc.stdout + "\n" + proc.stderr).strip().splitlines()
            findings.append(
                Finding(
                    key,
                    title,
                    "BLOCK",
                    f"失敗（exit {proc.returncode}），CI 會擋下這次合併。{note}",
                    evidence=trim(tail[-20:]),
                    fix=f"在分支上修到 `{' '.join(['uv', 'run', *cmd])}` 乾淨通過為止。"
                    + (
                        "　本機 ruff 版本與 CI 不同時會出現 CI 不會有的失敗，"
                        "先 `uv sync --extra dev` 對齊再判斷。"
                        if cmd[0] == "ruff"
                        else ""
                    ),
                )
            )

    app_js = REPO / "src" / "serving" / "static" / "app.js"
    if not app_js.is_file():
        findings.append(Finding("ci.js", "node --check app.js", "PASS", "專案沒有 app.js，略過。"))
    elif not shutil.which("node"):
        findings.append(
            Finding(
                "ci.js",
                "node --check app.js",
                "WARN",
                "找不到 node，前端語法未驗。",
                fix="裝 Node.js 後執行 `node --check src/serving/static/app.js`。",
            )
        )
    else:
        proc = run(["node", "--check", "src/serving/static/app.js"], timeout=120)
        if proc.returncode == 0:
            findings.append(Finding("ci.js", "node --check app.js", "PASS", "前端語法通過。"))
        else:
            findings.append(
                Finding(
                    "ci.js",
                    "node --check app.js",
                    "BLOCK",
                    "前端 JavaScript 語法錯誤。",
                    evidence=trim((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-15:]),
                    fix="修正 src/serving/static/app.js 語法錯誤。",
                )
            )
    return findings


# --------------------------------------------------------------------------
# 報告
# --------------------------------------------------------------------------
BADGE = {"BLOCK": "❌ BLOCK", "WARN": "⚠️ WARN", "PASS": "✅ PASS", "SKIP": "⏭️ SKIP"}


def render_section(findings: list[Finding], empty: str) -> str:
    if not findings:
        return empty
    blocks = []
    for f in findings:
        parts = [f"### {f.title}", "", f"- 結論：{f.detail}"]
        if f.fix:
            parts.append(f"- 修正：{f.fix}")
        if f.evidence:
            parts += ["- 證據：", "", "```text", *f.evidence, "```"]
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def write_report(path: Path, ctx: dict) -> None:
    if not TEMPLATE_PATH.is_file():
        die(f"找不到報告樣板：{TEMPLATE_PATH}")
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    for key, val in ctx.items():
        text = text.replace("{{" + key + "}}", str(val))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
def main() -> int:
    global REPO

    parser = argparse.ArgumentParser(description="分支併入 main 前的合併門檻檢查")
    parser.add_argument("branch", nargs="?", default=None, help="要檢查的分支，預設為目前 HEAD")
    parser.add_argument(
        "--base", default=None, help="目標分支，預設讀 gate_rules.json 的 base_branch"
    )
    parser.add_argument(
        "--out", default=None, help="報告輸出路徑，預設 reports/merge_check/<分支>_<時間>.md"
    )
    parser.add_argument(
        "--no-ci", action="store_true", help="略過 ruff／pytest／node（只做 git 靜態檢查）"
    )
    parser.add_argument(
        "--fetch", action="store_true", help="先 git fetch，確保比較基準是 origin 上的最新狀態"
    )
    parser.add_argument("--json", action="store_true", help="同時把結構化結果印到 stdout")
    args = parser.parse_args()

    top = run(["git", "rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        die("這裡不是 git repository。")
    REPO = Path(top.stdout.strip())

    rules = (
        json.loads(RULES_PATH.read_text(encoding="utf-8"))
        if RULES_PATH.is_file()
        else die(f"找不到規則檔：{RULES_PATH}")
    )

    base = args.base or rules.get("base_branch", "main")
    head = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    branch = args.branch or head

    if git("rev-parse", "--verify", "--quiet", branch).returncode != 0:
        die(f"找不到分支／ref：{branch}")

    base_ref, base_finding = resolve_base(base, args.fetch)

    merge_base = git("merge-base", base_ref, branch).stdout.strip()
    if not merge_base:
        die(f"{branch} 與 {base_ref} 沒有共同祖先，無法比較。")

    commits = branch_commits(merge_base, branch)
    changes = changed_files(merge_base, branch)
    files = [p for _, p in changes]
    live = [p for s, p in changes if not s.startswith("D")]

    if not commits:
        die(f"{branch} 相對 {base_ref} 沒有任何 commit，沒東西可以合併。")

    # CI 只能反映目前 working tree；分支不是 HEAD 就不跑，避免給出假的通過
    skip_reason = None
    if args.no_ci:
        skip_reason = "指定了 --no-ci"
    elif branch != head:
        skip_reason = f"要檢查的是 {branch}，但目前簽出的是 {head}"

    findings: list[Finding] = [
        base_finding,
        check_conflict(base_ref, branch),
        check_behind(base_ref, branch),
        check_secrets(merge_base, branch, rules),
        check_forbidden(live, rules),
        check_large(live, branch, rules),
        check_whitespace(merge_base, branch),
        check_commit_format(commits, rules),
        check_log_checkpoint(files, merge_base, branch),
        check_tests_touched(files),
        check_doc_sync(files, rules),
        *([] if skip_reason else [check_worktree(branch)]),
        *ci_findings(skip_reason),
    ]

    blocks = [f for f in findings if f.status == "BLOCK"]
    warns = [f for f in findings if f.status == "WARN"]
    skips = [f for f in findings if f.status == "SKIP"]
    passes = [f for f in findings if f.status == "PASS"]

    if blocks:
        result = "BLOCK"
    elif warns or skips:
        result = "WARN"
    else:
        result = "PASS"

    if result == "BLOCK":
        next_action = (
            f"**先不要合併。** 依上面 BLOCK 項目修正後，在分支上重跑：\n\n"
            f"```bash\npython .claude/skills/pre-merge-check/scripts/check_merge.py {branch}\n```"
        )
    else:
        merge_cmds = (
            f"```bash\ngit switch {base}\ngit pull --ff-only\ngit merge --no-ff {branch}\n```"
        )
        if result == "WARN":
            next_action = (
                "**可以合併，但要先人工確認上面每一項 WARN。**\n\n"
                "確認完成後再自行執行合併（本檢查不會動 git）：\n\n" + merge_cmds
            )
        else:
            next_action = "**符合要求，可以合併。**\n\n" + merge_cmds

    summary_rows = "\n".join(f"| {f.title} | {BADGE[f.status]} | {f.detail} |" for f in findings)

    ts = datetime.now()
    out_path = (
        Path(args.out)
        if args.out
        else (
            REPO
            / "reports"
            / "merge_check"
            / f"{re.sub(r'[^A-Za-z0-9_.-]', '-', branch)}_{ts:%Y%m%d-%H%M%S}.md"
        )
    )
    write_report(
        out_path,
        {
            "RESULT": result,
            "RESULT_TEXT": RESULT_TEXT[result],
            "BRANCH": branch,
            "BASE": base_ref,
            "GENERATED_AT": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "MERGE_BASE_SHORT": merge_base[:9],
            "COMMIT_COUNT": len(commits),
            "FILE_COUNT": len(files),
            "SUMMARY_TABLE": summary_rows,
            "BLOCK_SECTION": render_section(blocks, "_無。_"),
            "WARN_SECTION": render_section(warns + skips, "_無。_"),
            "PASS_SECTION": "\n".join(f"- {f.title} —— {f.detail}" for f in passes) or "_無。_",
            "NEXT_ACTION": next_action,
        },
    )

    print()
    print(f"分支 {branch} → {base_ref}｜commit {len(commits)}｜檔案 {len(files)}")
    print("-" * 72)
    for f in findings:
        print(f"{BADGE[f.status]:<10} {f.title:<22} {f.detail}")
    print("-" * 72)
    print(f"判定：{result} —— {RESULT_TEXT[result]}")
    print(f"修正說明：{out_path}")

    if args.json:
        print(
            json.dumps(
                {
                    "result": result,
                    "branch": branch,
                    "base": base_ref,
                    "report": str(out_path),
                    "findings": [f.as_dict() for f in findings],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    return {"PASS": 0, "WARN": 1, "BLOCK": 2}[result]


if __name__ == "__main__":
    raise SystemExit(main())
