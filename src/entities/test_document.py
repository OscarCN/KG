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


def test_news_record_carries_media_pictures():
    pics = [{"url": "http://img/1.jpg", "url_md5": "m1", "extra": "x"}]
    article = record_to_article(
        {"_id": "abc", "text": "body", "doctype": "news", "media_pictures": pics}
    )
    assert article["media_pictures"] == pics


def test_facebook_record_carries_media_pictures():
    pics = [{"url": "http://img/2.jpg", "url_md5": "m2"}]
    article = record_to_article(
        {"_id": "fb1", "type": "facebook",
         "message": {"body": "post", "media_pictures": pics}}
    )
    assert article["media_pictures"] == pics


def test_missing_media_pictures_defaults_to_empty_list():
    article = record_to_article({"_id": "abc", "text": "body", "doctype": "news"})
    assert article["media_pictures"] == []
