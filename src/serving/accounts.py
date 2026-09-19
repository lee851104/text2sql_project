"""Account roster: who may sign in, and whose rows they may read.

名冊放在版本控制外的檔案，不寫進程式碼。密碼雜湊即使不可逆，進了 Git 就無法撤回，
所以 `configs/accounts.yaml` 由 `.gitignore` 排除，版控裡只留
`configs/accounts.example.yaml` 當格式範例 —— 與既有 `.env.example` 同一套慣例。

授權綁定用 `dim_plant_scope.plant_id`，不用電廠名稱。名稱會因改制、更名或全半形差異而變，
編號則是建庫時被釘住、而且每次建庫都逐筆比對過漂移的識別碼。把授權掛在會變的字串上，
等於哪天有人改了一個字，某個帳號就換了一座電廠。

`plant_name` 是選填的對照欄位：填了就會在啟動時與名冊比對，不符即拒絕啟動。它讓名冊
對人類可讀，同時把建庫層那條漂移偵測延伸到授權層 —— 編號與名稱只要對不起來就停下來。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ALL_PLANTS = "all"
HASH_SCHEME = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 600_000
MINIMUM_ITERATIONS = 100_000
MINIMUM_PASSWORD_LENGTH = 8
MAXIMUM_PASSWORD_LENGTH = 512


class AccountRosterError(ValueError):
    """The roster is unusable; the service must refuse to start rather than guess."""


@dataclass(frozen=True, slots=True)
class Account:
    """One sign-in identity and the data scope bound to it."""

    username: str
    password_hash: str = field(repr=False)
    plant_id: int | None
    expected_plant_name: str | None = None

    @property
    def sees_every_plant(self) -> bool:
        return self.plant_id is None


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """Encode a password as ``scheme$iterations$salt$digest`` with a fresh salt."""

    if not MINIMUM_PASSWORD_LENGTH <= len(password) <= MAXIMUM_PASSWORD_LENGTH:
        raise AccountRosterError(
            f"密碼長度必須介於 {MINIMUM_PASSWORD_LENGTH} 與 {MAXIMUM_PASSWORD_LENGTH}。"
        )
    if iterations < MINIMUM_ITERATIONS:
        raise AccountRosterError(f"PBKDF2 迭代次數不得低於 {MINIMUM_ITERATIONS}。")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{HASH_SCHEME}${iterations}${salt.hex()}${digest.hex()}"


def decode_password_hash(encoded: str) -> tuple[int, bytes, bytes]:
    parts = encoded.split("$")
    if len(parts) != 4 or parts[0] != HASH_SCHEME:
        raise AccountRosterError(f"密碼雜湊格式必須是 {HASH_SCHEME}$迭代次數$salt$digest。")
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        digest = bytes.fromhex(parts[3])
    except ValueError as error:
        raise AccountRosterError("密碼雜湊的迭代次數、salt 或 digest 無法解析。") from error
    if iterations < MINIMUM_ITERATIONS:
        raise AccountRosterError(f"密碼雜湊的迭代次數不得低於 {MINIMUM_ITERATIONS}。")
    if not salt or not digest:
        raise AccountRosterError("密碼雜湊的 salt 與 digest 不可為空。")
    return iterations, salt, digest


def verify_password(password: str, encoded: str) -> bool:
    """Compare a candidate against an encoded hash in constant time."""

    iterations, salt, expected = decode_password_hash(encoded)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def _scope_of(entry: dict[str, object], username: str) -> int | None:
    if "scope" not in entry:
        raise AccountRosterError(f"帳號「{username}」沒有指定 scope。")
    scope = entry["scope"]
    if isinstance(scope, str) and scope.strip().casefold() == ALL_PLANTS:
        return None
    if isinstance(scope, bool) or not isinstance(scope, int):
        raise AccountRosterError(
            f"帳號「{username}」的 scope 必須是 {ALL_PLANTS!r}，"
            "或 dim_plant_scope 的 plant_id 整數。"
        )
    if scope < 1:
        raise AccountRosterError(f"帳號「{username}」的 plant_id 必須是正整數。")
    return scope


def parse_roster(payload: object) -> tuple[Account, ...]:
    """Validate a parsed roster document and return its accounts."""

    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), list):
        raise AccountRosterError("名冊必須是含有 accounts 清單的 mapping。")
    entries = payload["accounts"]
    if not entries:
        raise AccountRosterError("名冊至少需要一個帳號。")

    accounts: list[Account] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise AccountRosterError(f"第 {index} 個帳號不是 mapping。")
        raw_username = entry.get("username")
        if not isinstance(raw_username, str) or not raw_username.strip():
            raise AccountRosterError(f"第 {index} 個帳號沒有 username。")
        username = raw_username.strip()
        if len(username) > 80:
            raise AccountRosterError(f"帳號「{username}」長度超過 80。")
        if username in seen:
            raise AccountRosterError(f"帳號「{username}」重複；重複的名字無法對應到唯一權限。")
        seen.add(username)

        password_hash = entry.get("password")
        if not isinstance(password_hash, str) or not password_hash.strip():
            raise AccountRosterError(f"帳號「{username}」沒有 password 雜湊。")
        decode_password_hash(password_hash.strip())

        expected_name = entry.get("plant_name")
        if expected_name is not None and (
            not isinstance(expected_name, str) or not expected_name.strip()
        ):
            raise AccountRosterError(f"帳號「{username}」的 plant_name 必須是非空字串。")

        plant_id = _scope_of(entry, username)
        if plant_id is None and expected_name is not None:
            raise AccountRosterError(
                f"帳號「{username}」的 scope 是 {ALL_PLANTS}，不應該同時綁定 plant_name。"
            )
        accounts.append(
            Account(
                username=username,
                password_hash=password_hash.strip(),
                plant_id=plant_id,
                expected_plant_name=expected_name.strip() if expected_name else None,
            )
        )
    return tuple(accounts)


def load_roster(path: Path) -> tuple[Account, ...]:
    """Read and validate the roster file."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise AccountRosterError(f"無法讀取帳號名冊 {path}：{error}") from error
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise AccountRosterError(f"帳號名冊 {path} 不是合法的 YAML：{error}") from error
    return parse_roster(payload)


@dataclass(frozen=True, slots=True)
class AccountBinding:
    """One account's resolved plant, or the reason it could not be resolved."""

    account: Account
    plant_name: str | None
    problem: str | None

    @property
    def is_valid(self) -> bool:
        return self.problem is None


def describe_bindings(
    accounts: Sequence[Account], plant_names: Mapping[int, str]
) -> tuple[AccountBinding, ...]:
    """Resolve every account's binding, reporting problems instead of raising on the first.

    這是綁定規則的唯一定義處。`resolve_plant_names` 是它的從嚴版本（一有問題就失敗），
    盤點指令則需要看完整份名冊，所以兩者共用同一套判斷，不各寫一份。
    """

    bindings: list[AccountBinding] = []
    for account in accounts:
        if account.sees_every_plant:
            bindings.append(AccountBinding(account, None, None))
            continue
        actual = plant_names.get(account.plant_id or 0)
        if actual is None:
            bindings.append(
                AccountBinding(
                    account,
                    None,
                    f"綁定的 plant_id={account.plant_id} 不在 dim_plant_scope 名冊中。",
                )
            )
            continue
        if account.expected_plant_name is not None and account.expected_plant_name != actual:
            bindings.append(
                AccountBinding(
                    account,
                    actual,
                    f"綁定的 plant_id={account.plant_id} 目前是「{actual}」，"
                    f"名冊記的是「{account.expected_plant_name}」。"
                    "編號與名稱對不起來時不啟動，以免帳號換了一座電廠。",
                )
            )
            continue
        bindings.append(AccountBinding(account, actual, None))
    return tuple(bindings)


def resolve_plant_names(
    accounts: Sequence[Account], plant_names: Mapping[int, str]
) -> dict[str, str | None]:
    """Bind each account to a plant name, refusing any binding that no longer holds.

    回傳 username → 電廠名稱（全廠帳號為 None）。編號查不到，或名冊上的 `plant_name`
    與資料庫對不起來，都直接失敗：授權綁到哪一座電廠無法確認時，不該讓服務照常啟動。
    """

    resolved: dict[str, str | None] = {}
    for binding in describe_bindings(accounts, plant_names):
        if binding.problem is not None:
            raise AccountRosterError(f"帳號「{binding.account.username}」{binding.problem}")
        resolved[binding.account.username] = binding.plant_name
    return resolved


USAGE = """用法：
  python -m serving.accounts hash   由 stdin 讀密碼，印出名冊用的雜湊（預設子指令）
  python -m serving.accounts list   盤點名冊：每個帳號的範圍與綁定是否仍對得上資料庫

範例：
  echo -n 'your-password' | python -m serving.accounts hash
"""


def render_roster(
    *,
    roster_path: Path,
    database: Path,
    accounts: Sequence[Account] | None,
    plant_names: Mapping[int, str] | None,
) -> tuple[str, int]:
    """Render the roster inventory and the exit code that reports its health.

    ``accounts`` 為 None 代表沒有名冊檔（服務沿用單一管理員帳號，這是支援的部署方式，
    不是錯誤）。``plant_names`` 為 None 代表讀不到資料庫，因此無法驗證綁定 —— 這時回
    非零離開碼：**未驗不等於通過**，與專案合併門檻的判定原則一致。
    """

    lines = [f"帳號名冊：{roster_path}"]
    if accounts is None:
        lines.append("狀態　　：名冊不存在；服務會沿用環境變數的單一管理員帳號，範圍為全廠。")
        return "\n".join(lines) + "\n", 0

    lines.append(f"資料庫　：{database}")
    lines.append(f"帳號數　：{len(accounts)}")
    if plant_names is None:
        lines.append("狀態　　：讀不到資料庫，無法驗證綁定。以下只列出名冊內容。")
        lines.append("")
        for account in accounts:
            lines.append(f"- {account.username}")
            if account.sees_every_plant:
                lines.append("    範圍：全部電廠")
            else:
                lines.append(f"    範圍：plant_id={account.plant_id}")
                lines.append("    綁定：? 未驗證")
        return "\n".join(lines) + "\n", 1

    lines.append("")
    problems = 0
    for binding in describe_bindings(accounts, plant_names):
        lines.append(f"- {binding.account.username}")
        if binding.account.sees_every_plant:
            lines.append("    範圍：全部電廠")
            continue
        scope = f"plant_id={binding.account.plant_id}"
        if binding.plant_name is not None:
            scope += f"（{binding.plant_name}）"
        lines.append(f"    範圍：{scope}")
        if binding.is_valid:
            lines.append("    綁定：ok")
        else:
            problems += 1
            lines.append(f"    綁定：✗ {binding.problem}")

    lines.append("")
    if problems:
        lines.append(f"有 {problems} 個帳號的綁定對不上資料庫；服務會在解析時拒絕這些帳號。")
    else:
        lines.append("所有綁定都對得上資料庫。")
    return "\n".join(lines) + "\n", 1 if problems else 0


def _list_command() -> int:
    import sys

    from ingest.validate import PROJECT_ROOT, resolve_configured_paths
    from text2sql.scope_guard import ScopeCatalog

    roster_path = PROJECT_ROOT / "configs" / "accounts.yaml"
    database = resolve_configured_paths(PROJECT_ROOT)["database"].resolve()

    accounts: Sequence[Account] | None = None
    if roster_path.exists():
        try:
            accounts = load_roster(roster_path)
        except AccountRosterError as error:
            sys.stderr.write(f"{error}\n")
            return 2

    plant_names: Mapping[int, str] | None = None
    if accounts is not None and database.exists():
        try:
            plant_names = ScopeCatalog.from_database(database).plant_names_by_id()
        except sqlite3.Error:
            plant_names = None

    report, code = render_roster(
        roster_path=roster_path,
        database=database,
        accounts=accounts,
        plant_names=plant_names,
    )
    sys.stdout.write(report)
    return code


def _hash_command() -> int:
    import sys

    password = sys.stdin.read().rstrip("\r\n")
    if not password:
        sys.stderr.write("請由 stdin 提供密碼。\n" + USAGE)
        return 2
    try:
        print(hash_password(password))
    except AccountRosterError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


def _main(argv: Sequence[str] | None = None) -> int:
    import sys

    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(item in {"-h", "--help", "help"} for item in arguments):
        sys.stderr.write(USAGE)
        return 0
    if len(arguments) > 1:
        sys.stderr.write(USAGE)
        return 2
    command = arguments[0] if arguments else "hash"
    if command == "list":
        return _list_command()
    if command == "hash":
        return _hash_command()
    sys.stderr.write(f"未知的子指令：{command}\n" + USAGE)
    return 2


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(_main())
