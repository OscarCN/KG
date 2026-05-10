from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.entities.tags_gpt.catalogs import ClaimCatalogStore, StanceCatalog
from src.entities.tags_gpt.llm import ScriptedJsonLlm
from src.entities.tags_gpt.models import (
    ArticleBundle,
    Customer,
    LinkedEventContext,
    RawClaim,
    SourceItem,
    StanceAssignment,
    TypeTriageItem,
)
from src.entities.tags_gpt.persistence import load_snapshot, save_snapshot
from src.entities.tags_gpt.retrieval import LinkedJsonRetriever
from src.entities.tags_gpt.streaming import StreamingState, StreamingTagsPipeline
from src.entities.tags_gpt.tagging import (
    ClaimTagger,
    ClaimUpdater,
    StanceTagger,
    StanceUpdater,
    TypeTriageStep,
)


def customer() -> Customer:
    return Customer(entity_id=75, name="Ayuntamiento", description="Gobierno municipal")


def bundle(*, with_event: bool = True) -> ArticleBundle:
    cust = customer()
    event_ids = ["event-1"] if with_event else []
    events = [LinkedEventContext(id="event-1", description="Reparacion de calle")] if with_event else []
    return ArticleBundle(
        root=SourceItem(id="root", kind="article", text="El municipio arreglo la calle."),
        comments=[
            SourceItem(id="c1", kind="user_comment", text="Gracias, pero falta alumbrado.", parent_source_id="root"),
            SourceItem(id="c2", kind="user_comment", text="Buenos dias.", parent_source_id="root"),
        ],
        event_ids=event_ids,
        linked_events=events,
        customer=cust,
    )


class TagsGptTests(unittest.TestCase):
    def test_model_serialization_round_trip(self) -> None:
        cust = customer()
        self.assertEqual(Customer.from_dict(cust.to_dict()).entity_id, 75)
        item = SourceItem(id="x", kind="article", text="texto")
        self.assertEqual(SourceItem.from_dict(item.to_dict()).text, "texto")

    def test_triage_local_ids_multi_stance_noise_and_unknown_drop(self) -> None:
        llm = ScriptedJsonLlm(
            {
                "type_triage": [
                    {"triage": [{"source_item_id": 1, "stance_type": "complaint", "brief_summary": "queja"}]},
                    {
                        "triage": [
                            {"source_item_id": 2, "stance_type": "gratefulness", "brief_summary": "gracias"},
                            {"source_item_id": 2, "stance_type": "suggestion", "brief_summary": "alumbrado"},
                            {"source_item_id": 3, "stance_type": "noise", "brief_summary": "saludo"},
                            {"source_item_id": 3, "stance_type": "question", "brief_summary": "drop por noise"},
                            {"source_item_id": 99, "stance_type": "complaint", "brief_summary": "unknown"},
                        ]
                    },
                ]
            }
        )
        result = TypeTriageStep(customer(), llm).triage(bundle(), batch_size=15)
        rows = [(x.source_item_id, x.stance_type) for x in result.triaged]
        self.assertIn(("c1", "gratefulness"), rows)
        self.assertIn(("c1", "suggestion"), rows)
        self.assertIn(("c2", "noise"), rows)
        self.assertNotIn(("c2", "question"), rows)
        self.assertGreaterEqual(result.dropped_invalid, 2)

    def test_type_scoped_stance_tagging_and_updater_validation(self) -> None:
        cust = customer()
        catalog = StanceCatalog(cust.entity_id)
        complaint = catalog.add("fallas en alumbrado publico", "desc", primary_type="complaint")
        catalog.add("agradecimiento por obras", "desc", primary_type="gratefulness")
        llm = ScriptedJsonLlm(
            {
                "stance_tagging": {
                    "assignments": [
                        {"source_item_id": 1, "stance_id": complaint.id, "stance_type": "complaint", "reason": "match"}
                    ],
                    "proposals": [
                        {
                            "kind": "add",
                            "label": "fallas en bacheo",
                            "description": "desc",
                            "stance_type": "complaint",
                            "source_item_ids": [1],
                        }
                    ],
                }
            }
        )
        items = [SourceItem(id="c1", kind="user_comment", text="Falta alumbrado")]
        tagging = StanceTagger(cust, llm).tag(
            event=None,
            items=items,
            catalog=catalog,
            stance_type="complaint",
            triage_hints=[
                TypeTriageItem(
                    source_item_id="c1",
                    source_kind="user_comment",
                    stance_type="complaint",
                    brief_summary="falta alumbrado",
                )
            ],
        )
        payload = llm.calls[0]["payload"]
        self.assertEqual({entry["id"] for entry in payload["catalog"]}, {complaint.id})
        summary = StanceUpdater(cust).update(catalog, tagging)
        self.assertEqual(summary.counters.get("assign"), 1)
        bad = StanceAssignment("c1", "user_comment", cust.entity_id, complaint.id, "gratefulness")
        self.assertFalse(catalog.assign(bad))
        uncatalogued = StanceAssignment("c1", "user_comment", cust.entity_id, None, "complaint")
        self.assertTrue(catalog.assign(uncatalogued))

    def test_claim_extraction_no_event_skip_and_root_only_default(self) -> None:
        cust = customer()
        llm = ScriptedJsonLlm(
            {
                "type_triage": {"triage": []},
                "claim_tagging": {"claims": [{"source_item_id": 1, "verbatim": "La obra termino.", "importance": 2}]},
                "claim_update": {"decisions": [{"claim_index": 1, "action": "create", "canonical": "La obra termino"}]},
            }
        )
        state = StreamingState(stance_catalog=StanceCatalog(cust.entity_id), claim_catalogs=ClaimCatalogStore())
        pipeline = StreamingTagsPipeline(
            state=state,
            type_triage=TypeTriageStep(cust, llm),
            stance_tagger=StanceTagger(cust, llm),
            stance_updater=StanceUpdater(cust),
            claim_tagger=ClaimTagger(cust, llm),
            claim_updater=ClaimUpdater(cust, llm),
        )
        pipeline.process_bundle(bundle(with_event=False))
        self.assertFalse(any(call["phase"] == "claim_tagging" for call in llm.calls))

        pipeline.process_bundle(bundle(with_event=True))
        claim_call = [call for call in llm.calls if call["phase"] == "claim_tagging"][0]
        self.assertEqual([item["kind"] for item in claim_call["payload"]["items"]], ["article"])

    def test_claim_catalog_assign_create_rename_merge(self) -> None:
        store = ClaimCatalogStore()
        catalog = store.get(75, "e1")
        claim1 = RawClaim("e1", 75, "A", "s1", "article")
        claim2 = RawClaim("e1", 75, "B", "s2", "article")
        c1 = catalog.create(claim1, "Canon A")
        c2 = catalog.create(claim2, "Canon B")
        self.assertTrue(catalog.rename(c1.id, "Canon A2"))
        self.assertTrue(catalog.merge(c2.id, c1.id))
        self.assertNotIn(c2.id, catalog.clusters)

    def test_retrieval_and_snapshot_save_load(self) -> None:
        cust = customer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "linked.json"
            events = root / "linked__events.json"
            corpus.write_text(
                json.dumps(
                    [
                        {
                            "url": "u1",
                            "title": "Titulo",
                            "text": "Texto",
                            "event_ids": ["e1"],
                            "comments": [{"comment_id": "c1", "comment_text": "Hola"}],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            events.write_text(json.dumps({"e1": {"description": "Evento"}}), encoding="utf-8")
            bundles = list(LinkedJsonRetriever(corpus, events).iter_bundles(cust))
            self.assertEqual(bundles[0].linked_events[0].description, "Evento")

            stance_catalog = StanceCatalog(cust.entity_id)
            stance_catalog.add("label", "desc", primary_type="complaint")
            claims = ClaimCatalogStore()
            snapshot = root / "snapshot.json"
            save_snapshot(snapshot, stance_catalog=stance_catalog, claim_catalogs=claims)
            loaded_stances, loaded_claims = load_snapshot(snapshot)
            self.assertEqual(len(loaded_stances.entries), 1)
            self.assertEqual(loaded_claims.to_dict(), {})


if __name__ == "__main__":
    unittest.main()
