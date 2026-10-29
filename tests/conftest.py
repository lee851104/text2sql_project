from collections.abc import Iterator

import pytest

from serving import runtime as runtime_module


@pytest.fixture(autouse=True, scope="session")
def no_local_llm_by_default() -> Iterator[None]:
    """預設當作這台機器沒有開 Ollama。

    auto 模式偵測到 Ollama 就會改用本機 LLM。不釘住的話，同一份測試在開著 Ollama 的
    開發機和沒有 Ollama 的 CI 上會走不同的路徑。用 session 範圍，是因為不少 fixture
    在模組層級就先建好 runtime，函式層級的 monkeypatch 來不及。要測偵測本身的測試
    自己傳 `probe`。
    """

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime_module, "probe_local_llm", lambda *_args, **_kwargs: False)
        yield
