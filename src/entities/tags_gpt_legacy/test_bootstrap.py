"""
Test / example: typed two-step stance bootstrap.

Run:
    python -m src.entities.tags_gpt.test_bootstrap
"""

from src.entities.tags_gpt.bootstrap import StanceBootstrapStep
from src.entities.tags_gpt.llm import ScriptedJsonLlm
from src.entities.tags_gpt.models import Customer, SourceItem


def test_empty_corpus_returns_empty_catalog():
    llm = ScriptedJsonLlm({})
    catalog = StanceBootstrapStep(llm).bootstrap(_customer(), [])
    assert not catalog.entries
    assert not llm.calls


def test_typed_bootstrap_creates_catalog_entries_by_type():
    llm = ScriptedJsonLlm(
        {
            "type_triage": [
                {
                    "triage": [
                        {"source_item_id": 1, "stance_type": "complaint"},
                        {"source_item_id": 1, "stance_type": "question"},
                    ]
                },
                {
                    "triage": [
                        {"source_item_id": "2", "stance_type": "complaint"},
                        {"source_item_id": 2, "stance_type": "question"},
                        {"source_item_id": 3, "stance_type": "denuncia"},
                        {"source_item_id": 4, "stance_type": "denuncia"},
                        {"source_item_id": 5, "stance_type": "request"},
                        {"source_item_id": 6, "stance_type": "noise"},
                        {"source_item_id": 99, "stance_type": "complaint"},
                    ]
                },
            ],
            "stance_bootstrap_catalog": [
                {
                    "entries": [
                        {
                            "label": "fallas en servicio de agua",
                            "description": "Quejas recurrentes por falta o mala operación del agua.",
                            "evidence_source_item_ids": [1, 2],
                        },
                        {
                            "label": "entrada sin evidencia suficiente",
                            "description": "Debe caer por tener una sola muestra.",
                            "evidence_source_item_ids": [1],
                        },
                    ]
                },
                {
                    "entries": [
                        {
                            "label": "denuncias de corrupción policial",
                            "description": "Acusaciones recurrentes de actos de corrupción policial.",
                            "evidence_source_item_ids": [1, 2],
                        }
                    ]
                },
                {
                    "entries": [
                        {
                            "label": "reporte de fallas de agua",
                            "description": "Preguntas recurrentes sobre dónde reportar fallas de agua.",
                            "evidence_source_item_ids": [1, "2", 999],
                        }
                    ]
                },
            ],
        }
    )
    result = StanceBootstrapStep(llm).bootstrap_with_debug(_customer(), _items())

    assert result.triage is not None
    assert result.triage.dropped_invalid == 1
    assert result.triage.dropped_tag_only == 2
    assert result.catalog_results[1].created == 1  # complaint
    assert result.catalog_results[1].dropped_insufficient_evidence == 1

    entries_by_type = {entry.primary_type for entry in result.catalog.entries.values()}
    assert entries_by_type == {"complaint", "denuncia", "question"}

    assert result.triage is not None
    assert len(result.triage.calls) == 2

    catalog_prompts = "\n".join(
        call["prompt"] for call in llm.calls if call["phase"] == "stance_bootstrap_catalog"
    )
    all_prompts = "\n".join(call["prompt"] for call in llm.calls)
    assert '"kind"' not in catalog_prompts
    assert "long-source-url" not in all_prompts


def test_bootstrap_reuses_batched_type_triage():
    llm = ScriptedJsonLlm(
        {
            "type_triage": [
                {"triage": [{"source_item_id": 1, "stance_type": "noise"}]},
                {"triage": [{"source_item_id": 2, "stance_type": "request"}]},
                {"triage": [{"source_item_id": 2, "stance_type": "noise"}]},
                {"triage": [{"source_item_id": 2, "stance_type": "request"}]},
            ]
        }
    )
    result = StanceBootstrapStep(llm, triage_batch_size=2).bootstrap_with_debug(
        _customer(),
        _items(),
    )

    assert result.triage is not None
    assert len(result.triage.calls) == 4
    assert [call["phase"] for call in llm.calls] == [
        "type_triage",
        "type_triage",
        "type_triage",
        "type_triage",
    ]
    assert result.triage.dropped_tag_only == 4
    assert not result.catalog.entries


def _customer() -> Customer:
    return Customer(
        entity_id=75,
        name="Ayuntamiento",
        description="Gobierno municipal",
    )


def _items() -> list[SourceItem]:
    return [
        SourceItem(
            id="long-source-url-1",
            kind="article",
            text="El servicio de agua falla y vecinos preguntan dónde reportar.",
        ),
        SourceItem(
            id="long-source-url-2",
            kind="user_comment",
            text="Otra vez no hay agua, dónde puedo reportar?",
            parent_source_id="long-source-url-1",
        ),
        SourceItem(
            id="long-source-url-3",
            kind="user_comment",
            text="Vi a un policía pidiendo mordida.",
            parent_source_id="long-source-url-1",
        ),
        SourceItem(
            id="long-source-url-4",
            kind="user_comment",
            text="Los policías siguen pidiendo dinero en la zona.",
            parent_source_id="long-source-url-1",
        ),
        SourceItem(
            id="long-source-url-5",
            kind="user_comment",
            text="¿Me pueden ayudar con mi folio personal?",
            parent_source_id="long-source-url-1",
        ),
        SourceItem(
            id="long-source-url-6",
            kind="user_comment",
            text="Buenos días.",
            parent_source_id="long-source-url-1",
        ),
    ]


if __name__ == "__main__":
    test_empty_corpus_returns_empty_catalog()
    test_typed_bootstrap_creates_catalog_entries_by_type()
    test_bootstrap_reuses_batched_type_triage()
    print("Typed bootstrap tests passed.")
