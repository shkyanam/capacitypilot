from types import SimpleNamespace

from capacity_planner import news, semantic_news


def test_chunk_text_preserves_boundaries_and_limit():
    text = "First capacity paragraph.\nSecond paragraph has a different expansion signal."
    chunks = semantic_news.chunk_text(text, 45)
    assert chunks
    assert all(len(chunk) <= 45 for chunk in chunks)
    assert "First capacity paragraph." in chunks[0]


def test_retrieve_ranks_embedding_similarity(monkeypatch):
    monkeypatch.setattr(
        semantic_news,
        "get_settings",
        lambda: SimpleNamespace(
            news_semantic_enabled=True,
            nebius_embedding_model="embed-model",
            news_semantic_chunk_chars=30,
            news_semantic_top_k=1,
        ),
    )
    monkeypatch.setattr(
        semantic_news,
        "_embeddings",
        lambda _texts: [[1.0, 0.0], [0.1, 0.9], [0.9, 0.1]],
    )
    matches = semantic_news.retrieve("Unrelated legal text.\nInfrastructure scale-out is planned.")
    assert len(matches) == 1
    assert matches[0]["chunk_id"] == 2
    assert matches[0]["semantic_score"] > 0.9


def test_semantic_classifier_discards_unallowlisted_categories(monkeypatch):
    candidates = [{"chunk_id": 7, "excerpt": "We will scale platform infrastructure."}]

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"matches":[{"chunk_id":7,"categories":'
                                '["capacity_investment","provision_capacity"],"reason":"plan"}]}'
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr(
        news,
        "get_settings",
        lambda: SimpleNamespace(
            nebius_api_key="test", nebius_base_url="https://example.test/v1", nebius_chat_model="chat"
        ),
    )
    monkeypatch.setattr(news.httpx, "post", lambda *_args, **_kwargs: Response())
    assert news._semantic_classification(candidates) == [
        {"chunk_id": 7, "categories": ["capacity_investment"], "reason": "plan"}
    ]


def test_semantic_evidence_keeps_cited_source_passage(monkeypatch):
    monkeypatch.setattr(
        news,
        "retrieve_semantic_passages",
        lambda _text: [{"chunk_id": 2, "excerpt": "Scale-out planned.", "semantic_score": 0.91}],
    )
    monkeypatch.setattr(
        news,
        "_semantic_classification",
        lambda _candidates: [
            {"chunk_id": 2, "categories": ["capacity_investment"], "reason": "scale-out"}
        ],
    )
    matches, error = news._semantic_evidence("source text")
    assert error is None
    assert matches == [
        {
            "chunk_id": 2,
            "excerpt": "Scale-out planned.",
            "semantic_score": 0.91,
            "categories": ["capacity_investment"],
            "reason": "scale-out",
        }
    ]
