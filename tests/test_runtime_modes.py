import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from ingest.build_db import build_database
from ingest.validate import PROJECT_ROOT
from serving import runtime as runtime_module
from serving.runtime import RuntimeManager, _provider_settings, build_runtime
from text2sql.llm import DisabledLLM, FakeLLM


class StubLocalLLM:
    # 收下的設定（base_url／timeout／extra_body…）原樣保留，供設定相關的測試檢查。
    options: list[dict[str, object]] = []

    def __init__(self, **options: object):
        self.options.append(options)

    def generate(self, prompt: str) -> str:
        raise AssertionError(f"not expected in runtime tests: {prompt}")


def _always(value: bool):
    calls: list[str] = []

    def probe(base_url: str) -> bool:
        calls.append(base_url)
        return value

    probe.calls = calls  # type: ignore[attr-defined]
    return probe


@pytest.fixture(scope="module")
def database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("runtime-modes") / "power.db"
    build_database(path)
    return path


@pytest.fixture(autouse=True)
def pin_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 provider 釘成 ollama，並換掉轉接層。

    這個檔案驗的是 runtime 的機制 —— 模式切換、Ollama 探測、快取、換資料庫 ——
    與預設開不開推理無關。環境變數 POWERQUERY_LLM_PROVIDER 或 llm.yaml 換成推理模式時，
    這些測試不該整批紅。專門驗設定檔的那幾筆自己讀檔。
    """

    monkeypatch.delenv(runtime_module.PROVIDER_ENV, raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setattr(runtime_module, "LocalLLM", StubLocalLLM)
    StubLocalLLM.options.clear()


def test_offline_never_builds_the_local_adapter_or_probes(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "LocalLLM",
        lambda **_kwargs: pytest.fail("offline mode must not construct the local adapter"),
    )
    probe = _always(True)

    service = build_runtime(database=database, mode="offline", model="offline-model", probe=probe)

    assert service.mode == "offline"
    assert service.local_llm is False
    assert service.model == "offline-model"
    assert service.root == PROJECT_ROOT.resolve()
    assert isinstance(service.pipeline.llm, DisabledLLM)
    assert probe.calls == [], "離線模式不該去連 Ollama"


def test_local_mode_uses_the_configured_ollama_endpoint(database: Path) -> None:
    service = build_runtime(database=database, mode="local", model="qwen3:8b")

    assert service.mode == "local"
    assert service.local_llm is True
    assert service.provider == "ollama"
    assert service.base_url == "http://127.0.0.1:11434/v1"
    sent = StubLocalLLM.options[-1]
    assert sent["base_url"] == "http://127.0.0.1:11434/v1"
    assert sent["model"] == "qwen3:8b"
    assert sent["timeout_seconds"] == 120.0
    assert sent["extra_body"] == {"reasoning_effort": "none"}
    assert sent["temperature"] == 0.0


def test_local_mode_does_not_quietly_fall_back_when_ollama_is_down(database: Path) -> None:
    """明講要本機 LLM 就給它；Ollama 沒開時讓使用者看到真正的失敗，而不是悄悄換成規則。"""

    probe = _always(False)
    service = build_runtime(database=database, mode="local", probe=probe)

    assert service.mode == "local"
    assert probe.calls == []


@pytest.mark.parametrize(("available", "expected"), [(True, "local"), (False, "offline")])
def test_auto_mode_follows_whether_ollama_answers(
    database: Path, available: bool, expected: str
) -> None:
    probe = _always(available)

    service = build_runtime(database=database, mode="auto", probe=probe)

    assert service.mode == expected
    assert service.requested_mode == "auto"
    assert probe.calls == ["http://127.0.0.1:11434/v1"]


def test_the_cloud_mode_name_is_gone(database: Path) -> None:
    with pytest.raises(ValueError, match="offline、local 或 auto"):
        build_runtime(database=database, mode="online")  # type: ignore[arg-type]


def test_model_comes_from_ollama_model_then_the_default(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert build_runtime(database=database, mode="offline").model == "qwen3:8b"
    monkeypatch.setenv("OLLAMA_MODEL", "gemma3:4b")
    assert build_runtime(database=database, mode="offline").model == "gemma3:4b"
    monkeypatch.setenv("OLLAMA_MODEL", "   ")
    assert build_runtime(database=database, mode="offline").model == "qwen3:8b"


def test_manager_status_reports_ollama_without_any_credential_fields(database: Path) -> None:
    service = build_runtime(database=database, mode="offline", model="configured-model")
    manager = RuntimeManager(service, probe=_always(False))

    assert manager.active_runtime is service
    assert manager.get_runtime("offline") is service
    assert manager.status() == {
        "default_mode": "offline",
        "active_mode": "offline",
        "local_available": False,
        "provider": "ollama",
        "model": "configured-model",
        "base_url": "http://127.0.0.1:11434/v1",
        "offline_capability": True,
    }


def test_auto_manager_switches_to_local_once_ollama_comes_up(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"up": False}
    manager = RuntimeManager(database=database, default_mode="auto", probe=lambda _url: state["up"])
    assert manager.active_runtime.mode == "offline"

    state["up"] = True
    # 快取期間內沿用上次的結果，不必每個查詢都多等一次連線。
    assert manager.get_runtime().mode == "offline"
    monkeypatch.setattr(runtime_module, "PROBE_TTL_SECONDS", 0.0)
    assert manager.get_runtime().mode == "local"


def test_configuring_auto_probes_right_away(database: Path) -> None:
    state = {"up": False}
    manager = RuntimeManager(
        database=database, default_mode="offline", probe=lambda _url: state["up"]
    )
    manager.status()

    state["up"] = True
    configured = manager.configure(mode="auto")

    assert configured.mode == "local", "剛按下套用就該看到 Ollama 現在的狀態"


def test_injected_runtime_rejects_a_model_override(database: Path) -> None:
    service = build_runtime(database=database, mode="offline")

    with pytest.raises(ValueError, match="不可同時覆寫"):
        RuntimeManager(service, model="different-model")


def test_injected_local_runtime_is_kept_instead_of_rebuilt(database: Path) -> None:
    """測試帶進來的 FakeLLM 不能被換成真的 Ollama。"""

    built = build_runtime(database=database, mode="local")
    built.pipeline.llm = FakeLLM([])
    manager = RuntimeManager(built, probe=_always(False))

    assert manager.get_runtime().pipeline is built.pipeline
    assert manager.get_runtime("auto").pipeline is built.pipeline
    assert manager.status()["local_available"] is True
    assert manager.configure(mode="offline").mode == "offline"
    assert manager.configure(mode="local").pipeline is built.pipeline


def test_injected_local_runtime_refuses_a_database_switch(database: Path, tmp_path: Path) -> None:
    replacement = tmp_path / "power-version-2.db"
    shutil.copyfile(database, replacement)
    manager = RuntimeManager(build_runtime(database=database, mode="local"))

    with pytest.raises(RuntimeError, match="注入式"):
        manager.switch_database(replacement)


def test_failed_configuration_preserves_previous_state(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = build_runtime(database=database, mode="offline", model="stable-model")
    manager = RuntimeManager(service, probe=_always(False))
    previous_status = manager.status()

    class BrokenLocalLLM:
        def __init__(self, **_kwargs: object):
            raise RuntimeError("adapter setup failed")

    monkeypatch.setattr(runtime_module, "LocalLLM", BrokenLocalLLM)
    with pytest.raises(RuntimeError, match="adapter setup failed"):
        manager.configure(mode="local", model="replacement-model")

    assert manager.active_runtime is service
    assert manager.status() == previous_status
    assert manager.default_mode == "offline"


def test_manager_switches_modes_per_query(database: Path) -> None:
    service = build_runtime(database=database, mode="offline", model="initial-model")
    manager = RuntimeManager(service, probe=_always(True))

    configured = manager.configure(mode="local", model="qwen3:8b")
    offline = manager.get_runtime("offline")
    automatic = manager.get_runtime("auto")

    assert configured.mode == "local"
    assert offline.mode == "offline"
    assert automatic is configured
    assert manager.default_mode == "local"
    assert manager.status()["active_mode"] == "local"
    with pytest.raises(TypeError):
        vars(manager)


def test_concurrent_mode_selection_returns_complete_runtimes(database: Path) -> None:
    manager = RuntimeManager(database=database, default_mode="auto", probe=_always(True))
    modes = ["offline", "local", "auto"] * 6

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(manager.get_runtime, modes))

    assert {item.mode for item in results} == {"offline", "local"}
    assert all(item.database == database.resolve() for item in results)


def test_database_switch_is_atomic_and_old_runtime_remains_usable(
    database: Path, tmp_path: Path
) -> None:
    replacement = tmp_path / "power-version-2.db"
    shutil.copyfile(database, replacement)
    manager = RuntimeManager(database=database, default_mode="offline")
    old_runtime = manager.active_runtime

    new_runtime = manager.switch_database(replacement)

    assert new_runtime.database == replacement.resolve()
    assert manager.active_runtime is new_runtime
    assert old_runtime.database == database.resolve()
    assert old_runtime.executor.execute("SELECT COUNT(*) FROM v_outage LIMIT 1", ())[1]
    assert manager.get_runtime("offline").database == replacement.resolve()


def test_failed_database_switch_keeps_previous_runtime(database: Path, tmp_path: Path) -> None:
    manager = RuntimeManager(database=database, default_mode="offline")
    previous = manager.active_runtime

    with pytest.raises(FileNotFoundError):
        manager.switch_database(tmp_path / "missing.db")

    assert manager.active_runtime is previous
    assert manager.get_runtime().database == database.resolve()


def test_the_runtime_takes_the_retrieval_floors_from_config(database: Path) -> None:
    """門檻改在設定檔就要生效，不能是程式裡寫死的數字。"""

    config = yaml.safe_load((PROJECT_ROOT / "configs/retriever.yaml").read_text(encoding="utf-8"))
    # 要用 fixture 現建的快照。省略 database 會落回 data/processed/power.db，
    # 那個檔不進版控 —— 本機跑得過，CI 只會拿到 FileNotFoundError。
    pipeline = build_runtime(database=database, mode="offline").pipeline
    assert pipeline.top_k == int(config["top_k"])
    assert pipeline.min_score == float(config["min_score"])
    assert pipeline.relative_score == float(config["relative_score"])
    assert pipeline.min_score > 0.0, "設定檔給了門檻，管線卻沒吃到"


def test_evaluation_can_turn_retrieval_off(database: Path) -> None:
    """RAG 對照實驗靠這個：0 筆範例，其餘 prompt 不變。"""

    pipeline = build_runtime(database=database, mode="offline", retrieval_top_k=0).pipeline

    assert pipeline.top_k == 0
    assert pipeline.retriever.retrieve("2026年7月台中#1尖峰出力", top_k=0) == []


# ── 設定檔：只有本機模型 ─────────────────────────────────────────────────


def test_unknown_provider_fails_instead_of_quietly_falling_back() -> None:
    """打錯名字要當場失敗；默默換成另一種模式，評測數字會對不上。"""

    with pytest.raises(ValueError, match="不支援的本機 LLM provider"):
        _provider_settings({"providers": {"ollama": {}}}, "gpt")


def test_the_provider_environment_variable_switches_modes(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(runtime_module.PROVIDER_ENV, "ollama-reasoning")

    service = build_runtime(database=database, mode="local")

    assert service.provider == "ollama-reasoning"
    assert StubLocalLLM.options[-1]["timeout_seconds"] == 600.0
    assert StubLocalLLM.options[-1]["extra_body"] is None, "推理模式不送 reasoning_effort"


def test_shipped_config_only_talks_to_a_local_ollama() -> None:
    """設定檔本身也要被釘住：地端是這個專題的前提，不能有任何雲端端點或金鑰欄位。"""

    config = yaml.safe_load((PROJECT_ROOT / "configs/llm.yaml").read_text(encoding="utf-8"))

    assert config["provider"] == "ollama"
    assert set(config["providers"]) == {"ollama", "ollama-reasoning"}
    for name in config["providers"]:
        settings = _provider_settings(config, name)
        assert settings["base_url"].startswith("http://127.0.0.1:"), name
        assert not any("key" in key for key in settings), f"{name} 不該有金鑰欄位"
        assert settings["temperature"] == 0, "同一句每次生出不同 SQL，評測會失去意義"
        assert settings["model_env"] == "OLLAMA_MODEL"
    assert _provider_settings(config, "ollama")["extra_body"] == {"reasoning_effort": "none"}
