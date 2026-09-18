"""合併門檻（.claude/skills/pre-merge-check）的回歸測試。

這些測試鎖住幾個實際踩過的坑：中文檔名被 git 八進位跳脫、純排版變更誤報文件未同步、
工作目錄髒掉卻判 PASS、以及至今沒有被真實觸發過的大型檔案與 log.md 半套路徑。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

GATE_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "pre-merge-check"
    / "scripts"
    / "check_merge.py"
)

if not GATE_SCRIPT.is_file():
    pytest.skip("pre-merge-check skill 未安裝", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("check_merge", GATE_SCRIPT)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

# 測試裡不透過 uv：暫存 repo 沒有 pyproject，`uv run` 會嘗試解析專案而失敗
PY_PREFIX = [sys.executable, "-m"]


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout.strip()


def _commit(root: Path, message: str, files: dict[str, str]) -> str:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """建立一個只有 main 的暫存 repo，並把門檻的 REPO 指過去。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "gate@test")
    _git(root, "config", "user.name", "gate")
    _commit(root, "feat: init", {"README.md": "base\n"})
    monkeypatch.setattr(gate, "REPO", root)
    return root


@pytest.fixture
def rules() -> dict:
    import json

    return json.loads(gate.RULES_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 純函式
# --------------------------------------------------------------------------
def test_match_pattern_handles_directory_prefix_and_glob() -> None:
    assert gate.match_pattern("data/raw/x.csv", "data/")
    assert gate.match_pattern("data", "data/")
    assert not gate.match_pattern("database.py", "data/")
    assert gate.match_pattern("啟動.bat", "*.bat")
    assert not gate.match_pattern("src/cli.py", "*.bat")


def test_unquote_path_restores_octal_escaped_chinese_filename() -> None:
    quoted = '"\\345\\225\\237\\345\\213\\225.bat"'
    assert gate.unquote_path(quoted) == "啟動.bat"
    assert gate.unquote_path("src/cli.py") == "src/cli.py"


def test_overall_lets_block_win_and_never_passes_on_skip() -> None:
    def f(status: str) -> object:
        return gate.Finding("k", "t", status, "d")

    assert gate.overall([f("PASS"), f("WARN"), f("BLOCK")]) == "BLOCK"
    assert gate.overall([f("PASS"), f("WARN")]) == "WARN"
    # 沒跑過的檢查不構成證據，因此不得判 PASS
    assert gate.overall([f("PASS"), f("SKIP")]) == "WARN"
    assert gate.overall([f("PASS"), f("PASS")]) == "PASS"


def test_check_forbidden_blocks_secrets_and_data_artifacts(rules: dict) -> None:
    finding = gate.check_forbidden([".env", "data/raw/x.csv", "src/cli.py"], rules)
    assert finding.status == "BLOCK"
    assert len(finding.evidence) == 2
    assert gate.check_forbidden([".env.example", "src/cli.py"], rules).status == "PASS"


def test_check_commit_format_accepts_conventional_and_merge_commits(rules: dict) -> None:
    good = [("a1", "feat: add thing"), ("a2", "Merge branch 'main' into x")]
    assert gate.check_commit_format(good, rules).status == "PASS"

    bad = gate.check_commit_format([("a3", "update stuff")], rules)
    assert bad.status == "WARN"
    assert "a3" in bad.evidence[0]


# --------------------------------------------------------------------------
# 需要 git 的檢查
# --------------------------------------------------------------------------
def test_check_worktree_warns_so_a_dirty_tree_cannot_pass(repo: Path) -> None:
    assert gate.check_worktree("main").status == "PASS"

    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    dirty = gate.check_worktree("main")
    assert dirty.status == "WARN"
    assert "README.md" in "\n".join(dirty.evidence)


def test_check_large_blocks_oversized_file(repo: Path, rules: dict) -> None:
    small = dict(rules, max_file_bytes=1024)
    _git(repo, "switch", "-q", "-c", "feature")
    _commit(repo, "feat: add blob", {"blob.bin": "x" * 5000})

    finding = gate.check_large(["blob.bin"], "feature", small)
    assert finding.status == "BLOCK"
    assert "blob.bin" in finding.evidence[0]
    assert gate.check_large(["blob.bin"], "feature", rules).status == "PASS"


def test_check_secrets_blocks_keys_but_honours_allowlist_and_marker(
    repo: Path, rules: dict
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "feature")
    _commit(repo, "feat: add config", {"conf.py": 'TOKEN = "sk-abcdefghijklmnopqrstuvwxyz123"\n'})

    assert gate.check_secrets(base, "feature", rules).status == "BLOCK"

    allowed = dict(rules, secret_allowlist=[{"regex": "sk-abcdef", "reason": "測試用"}])
    assert gate.check_secrets(base, "feature", allowed).status == "PASS"

    _commit(
        repo,
        "feat: mark as reviewed",
        {"conf.py": f'TOKEN = "sk-abcdefghijklmnopqrstuvwxyz123"  # {gate.ALLOW_MARKER}\n'},
    )
    assert gate.check_secrets(base, "feature", rules).status == "PASS"


def test_check_log_checkpoint_warns_on_incomplete_entry(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "feature")
    _commit(repo, "docs: half a checkpoint", {"log.md": "## CP-999 — 只有標題\n\n- 狀態：已完成\n"})

    partial = gate.check_log_checkpoint(["log.md"], base, "feature")
    assert partial.status == "WARN"
    assert "回退方式" in partial.detail

    _commit(
        repo,
        "docs: complete the checkpoint",
        {
            "log.md": "## CP-999 — 完整\n\n- 時間：2026-09-18\n- 狀態：已完成\n"
            "- 回退方式：回退本 commit\n"
        },
    )
    assert gate.check_log_checkpoint(["log.md"], base, "feature").status == "PASS"

    assert gate.check_log_checkpoint(["src/cli.py"], base, "feature").status == "WARN"


# --------------------------------------------------------------------------
# 純排版變更不該被當成行為變更
# --------------------------------------------------------------------------
def test_format_only_files_separates_reformatting_from_real_changes(repo: Path) -> None:
    base = _commit(repo, "feat: add module", {"app.py": 'def f(a, b):\n    return {"x": a + b}\n'})
    _git(repo, "switch", "-q", "-c", "feature")
    _commit(
        repo,
        "style: reformat and add a real change",
        {
            # 只有排版不同，ruff format 正規化後與 base 相同
            "app.py": 'def f(a,b):\n    return {  "x":a+b  }\n',
            "real.py": "def g():\n    return 1\n",
        },
    )
    _commit(repo, "feat: change behaviour", {"real.py": "def g():\n    return 2\n"})

    only = gate.format_only_files(["app.py", "real.py"], base, "feature", PY_PREFIX)
    assert only == {"app.py"}


def test_doc_sync_ignores_a_format_only_change(rules: dict) -> None:
    files = ["src/serving/app.py"]
    assert gate.check_doc_sync(files, rules).status == "WARN"
    # 同一批檔案，但被判定為純排版時不得再要求更新 API 文件
    assert gate.check_doc_sync(files, rules, format_only=set(files)).status == "PASS"


# --------------------------------------------------------------------------
# 端到端
# --------------------------------------------------------------------------
def test_cli_exits_2_and_writes_a_report_when_blocking(repo: Path, tmp_path: Path) -> None:
    _git(repo, "switch", "-q", "-c", "feature")
    _commit(repo, "feat: leak a key", {".env": "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123\n"})

    report = tmp_path / "report.md"
    proc = subprocess.run(
        [sys.executable, str(GATE_SCRIPT), "feature", "--no-ci", "--out", str(report)],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert proc.returncode == 2, proc.stdout + proc.stderr
    body = report.read_text(encoding="utf-8")
    assert "**BLOCK**" in body
    assert "禁止進版控的檔案" in body
