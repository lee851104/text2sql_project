from text2sql.retriever import TfidfRetriever


def test_tfidf_retrieval_is_relevant_and_deterministic() -> None:
    examples = [
        {"id": "unit", "question": "台中機組尖峰出力"},
        {"id": "reserve", "question": "備轉容量率最低日"},
        {"id": "outage", "question": "機組歲修排程"},
    ]
    retriever = TfidfRetriever(examples)
    first = retriever.retrieve("今年備轉容量率最低是哪天", top_k=2)
    second = retriever.retrieve("今年備轉容量率最低是哪天", top_k=2)
    assert first[0].example["id"] == "reserve"
    assert first == second
    assert first[0].score > first[1].score


def test_empty_retriever_returns_no_examples() -> None:
    assert TfidfRetriever([]).retrieve("任意問題") == []


EXAMPLES = [
    {"id": "unit", "question": "台中機組尖峰出力"},
    {"id": "reserve", "question": "備轉容量率最低日"},
    {"id": "outage", "question": "機組歲修排程"},
]


def test_an_unrelated_question_gets_no_examples_instead_of_zero_scored_ones() -> None:
    """五個 0.000 分的範例送進 prompt 只會誤導模型，不如什麼都不給。"""

    retriever = TfidfRetriever(EXAMPLES)
    assert retriever.retrieve("asdfghjkl", top_k=5) != [], "預設必須維持不篩"
    assert all(item.score == 0.0 for item in retriever.retrieve("asdfghjkl", top_k=5))
    assert retriever.retrieve("asdfghjkl", top_k=5, min_score=0.05) == []


def test_the_floors_default_to_off() -> None:
    """評測與範圍問句建議靠 top-1 分數自己判斷，不能被門檻改掉。"""

    retriever = TfidfRetriever(EXAMPLES)
    assert len(retriever.retrieve("台中機組尖峰出力", top_k=3)) == 3


def test_the_relative_floor_drops_examples_far_behind_the_first() -> None:
    """分數尺度隨問句長度變動，固定門檻一個人撐不住。"""

    retriever = TfidfRetriever(EXAMPLES)
    unfiltered = retriever.retrieve("台中機組尖峰出力", top_k=3)
    assert unfiltered[0].score > unfiltered[-1].score

    kept = retriever.retrieve("台中機組尖峰出力", top_k=3, relative_score=0.5)
    assert kept, "第一名自己永遠留得下來"
    assert all(item.score >= unfiltered[0].score * 0.5 for item in kept)
    assert len(kept) < len(unfiltered)


def test_the_first_result_always_survives_its_own_relative_floor() -> None:
    retriever = TfidfRetriever(EXAMPLES)
    kept = retriever.retrieve("備轉容量率最低日", top_k=3, relative_score=1.0)
    assert [item.example["id"] for item in kept] == ["reserve"]
