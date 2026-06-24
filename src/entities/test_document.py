"""Tests for record_to_article (src/entities/document.py)."""

from src.entities.document import record_to_article


def test_news_record_sets_source_type():
    article = record_to_article(
        {
            "_id": "abc",
            "text": "body",
            "title": "t",
            "url": "http://x/y",
            "doctype": "news",
        }
    )
    assert article["source_type"] == "news"
    assert article["document_type"] == "news"


def test_facebook_record_sets_source_type():
    article = record_to_article(
        {
            "_id": "fb1",
            "type": "facebook",
            "message": {
                "body": "post",
                "title": "tt",
                "type": "facebook",
            },
        }
    )
    assert article["source_type"] == "facebook"
    assert article["document_type"] == "facebook"
