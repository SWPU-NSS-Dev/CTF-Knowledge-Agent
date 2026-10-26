from scripts.evaluate_hybrid import _metrics


def test_benchmark_preserves_actual_retrieval_order_and_deduplicates():
    results = [
        (
            {
                "query": "needle",
                "expected_article_ids": {"z-relevant": "article-z", "a-other": "article-a"},
                "items": [
                    {"article_id": "article-a"},
                    {"article_id": "article-z"},
                    {"article_id": "article-a"},
                ],
            },
            ["z-relevant"],
        )
    ]

    report = _metrics(results, [12.0], top_k=3)

    assert report["queries"][0]["returned_fixture_ids"] == ["a-other", "z-relevant"]
    assert report["queries"][0]["reciprocal_rank"] == 0.5
    assert report["top1_accuracy"] == 0.0
    assert report["mrr_at_k"] == 0.5
