import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from ingest.build_db import build_database
from ingest.validate import PROJECT_ROOT
from serving import runtime as runtime_module
from serving.runtime import RuntimeManager, build_runtime
from text2sql.llm import DisabledLLM


class StubOpenAILLM:
    calls: list[tuple[str | None, str | None, float]] = []

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ):
        self.calls.append((model, api_key, timeout_seconds))

    def generate(self, prompt: str) -> str:
        raise AssertionError(f"not expected in runtime tests: {prompt}")


@pytest.fixture(scope="module")
def database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("runtime-modes") / "power.db"
    build_database(path)
    return path


def test_build_runtime_offline_ignores_environment_key(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    monkeypatch.setenv("OPENAI_MODEL", "   ")
    monkeypatch.setattr(
        runtime_module,
        "OpenAILLM",
        lambda **_kwargs: pytest.fail("offline mode must not construct the online adapter"),
    )

    service = build_runtime(
        database=database,
        root=PROJECT_ROOT,
        mode="offline",
        model="offline-model",
    )

    assert service.mode == "offline"
    assert service.online_llm is False
    assert service.source == "offline"
    assert service.model == "offline-model"
    assert service.root == PROJECT_ROOT.resolve()
    assert isinstance(service.pipeline.llm, DisabledLLM)
    assert "environment-secret" not in repr(service)


def test_offline_default_ignores_invalid_online_model_environment(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "   ")

    service = build_runtime(database=database, mode="offline")

    assert service.mode == "offline"
    assert service.model == "gpt-5.4-mini"


def test_auto_mode_preserves_environment_model_until_key_becomes_available(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "environment-model")
    monkeypatch.setattr(runtime_module, "OpenAILLM", StubOpenAILLM)
    manager = RuntimeManager(database=database, default_mode="auto")

    assert manager.active_runtime.mode == "offline"
    assert manager.status()["model"] == "environment-model"

    monkeypatch.setenv("OPENAI_API_KEY", "later-key")
    online = manager.get_runtime()

    assert online.mode == "online"
    assert online.model == "environment-model"


@pytest.mark.parametrize(
    ("mode", "argument_key", "environment_key", "expected_source"),
    [
        ("online", "memory-secret", None, "memory"),
        ("auto", None, "environment-secret", "environment"),
    ],
)
def test_build_runtime_selects_online_without_exposing_key(
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    argument_key: str | None,
    environment_key: str | None,
    expected_source: str,
) -> None:
    StubOpenAILLM.calls.clear()
    monkeypatch.setattr(runtime_module, "OpenAILLM", StubOpenAILLM)
    if environment_key is None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENAI_API_KEY", environment_key)

    service = build_runtime(
        database=database,
        mode=mode,  # type: ignore[arg-type]
        model="selected-model",
        api_key=argument_key,
    )
    secret = argument_key or environment_key

    assert service.mode == "online"
    assert service.online_llm is True
    assert service.source == expected_source
    assert service.model == "selected-model"
    assert StubOpenAILLM.calls == [("selected-model", secret, 30.0)]
    assert secret is not None
    assert secret not in repr(service)


def test_explicit_online_requires_a_key(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="API key"):
        build_runtime(database=database, mode="online")


def test_manager_accepts_injected_runtime_and_reports_safe_status(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service = build_runtime(database=database, mode="offline", model="configured-model")
    manager = RuntimeManager(service)

    assert manager.active_runtime is service
    assert manager.get_runtime("offline") is service
    assert manager.status() == {
        "default_mode": "offline",
        "active_mode": "offline",
        "online_configured": False,
        "provider": "openai",
        "model": "configured-model",
        "source": "offline",
        "offline_capability": True,
    }


def test_injected_auto_runtime_preserves_requested_default(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service = build_runtime(database=database, mode="auto")
    manager = RuntimeManager(service)

    assert service.mode == "offline"
    assert service.requested_mode == "auto"
    assert manager.default_mode == "auto"

    monkeypatch.setenv("OPENAI_API_KEY", "later-environment-secret")
    monkeypatch.setattr(runtime_module, "OpenAILLM", StubOpenAILLM)
    assert manager.get_runtime().mode == "online"


def test_injected_runtime_rejects_inconsistent_model_or_key_overrides(
    database: Path,
) -> None:
    service = build_runtime(database=database, mode="offline")

    with pytest.raises(ValueError, match="不可同時覆寫"):
        RuntimeManager(service, model="different-model")
    with pytest.raises(ValueError, match="不可同時覆寫"):
        RuntimeManager(service, api_key="different-key")


def test_injected_online_runtime_remains_usable_without_retaining_its_key(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(runtime_module, "OpenAILLM", StubOpenAILLM)
    service = build_runtime(
        database=database,
        mode="online",
        model="injected-model",
        api_key="injected-secret",
    )

    manager = RuntimeManager(service)
    selected = manager.get_runtime()

    assert selected.pipeline is service.pipeline
    assert selected.source == "injected"
    assert manager.status()["online_configured"] is True
    assert "injected-secret" not in repr(manager)

    assert manager.configure(mode="online").source == "injected"
    assert manager.configure(mode="offline").mode == "offline"
    assert manager.configure(mode="online").source == "injected"


def test_caller_supplied_environment_runtime_is_purged_when_key_is_removed(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "OpenAILLM", StubOpenAILLM)
    monkeypatch.setenv("OPENAI_API_KEY", "temporary-environment-secret")
    service = build_runtime(database=database, mode="online")
    manager = RuntimeManager(service)

    monkeypatch.delenv("OPENAI_API_KEY")
    status = manager.status()

    assert status["online_configured"] is False
    assert status["active_mode"] == "offline"
    assert status["source"] == "offline"


def test_caller_supplied_environment_runtime_detects_key_rotation_during_handoff(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "OpenAILLM", StubOpenAILLM)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key-a")
    service = build_runtime(database=database, mode="online")

    monkeypatch.setenv("OPENAI_API_KEY", "environment-key-b")
    manager = RuntimeManager(service)
    selected = manager.get_runtime("auto")

    assert selected.pipeline is not service.pipeline
    assert selected.source == "environment"
    assert StubOpenAILLM.calls[-1][1] == "environment-key-b"


def test_failed_online_configuration_preserves_previous_state(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service = build_runtime(database=database, mode="offline", model="stable-model")
    manager = RuntimeManager(service)
    previous_status = manager.status()

    class BrokenOpenAILLM:
        def __init__(self, **_kwargs: object):
            raise RuntimeError("adapter setup failed")

    monkeypatch.setattr(runtime_module, "OpenAILLM", BrokenOpenAILLM)
    with pytest.raises(RuntimeError, match="adapter setup failed"):
        manager.configure(mode="online", model="replacement-model", api_key="do-not-leak")

    assert manager.active_runtime is service
    assert manager.status() == previous_status
    assert manager.default_mode == "offline"
    assert "do-not-leak" not in repr(manager)
    assert "do-not-leak" not in repr(manager.status())


def test_manager_switches_modes_per_query_and_keeps_key_in_memory(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(runtime_module, "OpenAILLM", StubOpenAILLM)
    service = build_runtime(database=database, mode="offline", model="initial-model")
    manager = RuntimeManager(service)

    configured = manager.configure(
        mode="online",
        model="online-model",
        api_key="memory-only-secret",
    )
    offline = manager.get_runtime("offline")
    automatic = manager.get_runtime("auto")

    assert configured.mode == "online"
    assert configured.source == "memory"
    assert offline.mode == "offline"
    assert automatic.mode == "online"
    assert automatic is configured
    assert manager.default_mode == "online"
    assert manager.status()["online_configured"] is True
    assert manager.status()["active_mode"] == "online"
    assert "memory-only-secret" not in repr(manager)
    assert "memory-only-secret" not in repr(manager.status())
    with pytest.raises(TypeError):
        vars(manager)


def test_environment_source_survives_cached_mode_switch(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    monkeypatch.setattr(runtime_module, "OpenAILLM", StubOpenAILLM)
    manager = RuntimeManager(database=database, default_mode="offline")

    online = manager.get_runtime("online")
    manager.get_runtime("offline")
    automatic = manager.get_runtime("auto")

    assert online.source == "environment"
    assert automatic is online
    assert manager.status()["source"] == "environment"


def test_removed_environment_key_purges_cached_online_runtime(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    monkeypatch.setattr(runtime_module, "OpenAILLM", StubOpenAILLM)
    manager = RuntimeManager(database=database, default_mode="online")

    assert manager.active_runtime.mode == "online"
    monkeypatch.delenv("OPENAI_API_KEY")

    status = manager.status()
    assert status["online_configured"] is False
    assert status["active_mode"] == "offline"
    assert manager._online_runtime is None
    assert manager.get_runtime("auto").mode == "offline"
    with pytest.raises(RuntimeError, match="API key"):
        manager.get_runtime("online")


def test_concurrent_mode_selection_returns_complete_runtimes(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(runtime_module, "OpenAILLM", StubOpenAILLM)
    manager = RuntimeManager(
        database=database,
        default_mode="auto",
        api_key="thread-secret",
    )
    modes = ["offline", "online", "auto"] * 6

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(manager.get_runtime, modes))

    assert {item.mode for item in results} == {"offline", "online"}
    assert all(item.database == database.resolve() for item in results)
    assert "thread-secret" not in repr(manager.status())


def test_database_switch_is_atomic_and_old_runtime_remains_usable(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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


def test_failed_database_switch_keeps_previous_runtime(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    manager = RuntimeManager(database=database, default_mode="offline")
    previous = manager.active_runtime

    with pytest.raises(FileNotFoundError):
        manager.switch_database(tmp_path / "missing.db")

    assert manager.active_runtime is previous
    assert manager.get_runtime().database == database.resolve()


def test_the_runtime_takes_the_retrieval_floors_from_config() -> None:
    """門檻改在設定檔就要生效，不能是程式裡寫死的數字。"""

    config = yaml.safe_load((PROJECT_ROOT / "configs/retriever.yaml").read_text(encoding="utf-8"))
    pipeline = build_runtime(mode="offline").pipeline
    assert pipeline.min_score == float(config["min_score"])
    assert pipeline.relative_score == float(config["relative_score"])
    assert pipeline.min_score > 0.0, "設定檔給了門檻，管線卻沒吃到"
