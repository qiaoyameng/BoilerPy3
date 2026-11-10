import json

import pytest

from boilerpy3 import filters
from boilerpy3.document import TextBlock, TextDocument
from boilerpy3.exceptions import TemplateModelError
from boilerpy3.extractors import Extractor
from boilerpy3.template import (
    MODEL_VERSION,
    TEMPLATE_LABEL,
    NormalizationConfig,
    TemplateBoilerplateFilter,
    TemplateFilterConfig,
    TemplateModel,
    TemplateModelTrainer,
    block_signature,
    normalize_block_text,
)


_extractor = Extractor(None)


def _parse(html):
    return _extractor.parse_doc(html)


NAV = '<nav><a href="/home">Home</a> <a href="/about">About</a></nav>'
COPYRIGHT = '<footer>Copyright 2024 Example Site. All rights reserved worldwide.</footer>'
RECOMMENDED = (
    '<aside><a href="/r1">Recommended story alpha details</a> '
    '<a href="/r2">Recommended story beta details</a></aside>'
)


def _page(article_paragraphs, with_recommended=True, copyright_year=2024, extra_nav=""):
    article = "".join(f"<p>{paragraph}</p>" for paragraph in article_paragraphs)
    rail = RECOMMENDED if with_recommended else ""
    footer = f"<footer>Copyright {copyright_year} Example Site. All rights reserved worldwide.</footer>"
    return (
        f"<html><body>{NAV}{extra_nav}{article}{rail}{footer}</body></html>"
    )


ARTICLE_A = [
    "Markets rallied on Tuesday after the central bank signalled a pause in rate increases for the coming quarter.",
    "Analysts noted that volatility had fallen for three straight weeks and inflows into equity funds resumed.",
]
ARTICLE_B = [
    "The coastal railway project received final approval yesterday following an eighteen month environmental review.",
    "Construction is expected to create several thousand local jobs and reduce freight travel times substantially.",
]

DUPLICATED_PARAGRAPH = (
    "This paragraph appears twice inside a single page but on no other page, so within-page repetition alone "
    "must never be treated as a site-wide template signal by the model."
)


def _train_batch():
    trainer = TemplateModelTrainer(
        filter_config=TemplateFilterConfig(min_support_docs=2, min_support_ratio=0.5, short_block_max_words=1))
    trainer.add_document("train-1", _parse(_page(ARTICLE_A)))
    trainer.add_document("train-2", _parse(_page(ARTICLE_B)))
    trainer.add_document(
        "train-3",
        _parse(_page([DUPLICATED_PARAGRAPH, DUPLICATED_PARAGRAPH], copyright_year=2023)))
    trainer.add_document(
        "train-4",
        _parse(_page(["A fourth article with its own completely distinct reporting text."],
                     with_recommended=False)))
    return trainer


def _signature_for_text(trainer, text):
    block = TextBlock(text, set(), text.count(" ") + 1, 0, 0, 0, 0)
    return block_signature(block, trainer.normalization_config)


def _signature_from_page(trainer, page_html, text_fragment):
    doc = _parse(page_html)
    matches = [block for block in doc.text_blocks if text_fragment in block.text]
    assert matches, f"expected a block containing {text_fragment!r}"
    return block_signature(matches[0], trainer.normalization_config)


def test_normalization_is_explicit_and_ordered():
    assert normalize_block_text("  Foo  2024  Bar. ", True, True, False) == "foo # bar."
    assert normalize_block_text("Foo 2024 Bar.", False, False, True) == "Foo 2024 Bar"
    collapsed = normalize_block_text("Copyright 2024 Example Site.", True, True, False)
    other_year = normalize_block_text("Copyright 1999 Example Site.", True, True, False)
    assert collapsed == other_year


def test_six_pages_separate_site_chrome_from_single_page_repetition(tmp_path):
    trainer = _train_batch()
    model = trainer.build_model()

    sample_page = _page(ARTICLE_A)
    duplicated_page = _page([DUPLICATED_PARAGRAPH, DUPLICATED_PARAGRAPH], copyright_year=2023)
    nav_signature = _signature_from_page(trainer, sample_page, "Home About")
    copyright_signature = _signature_from_page(trainer, sample_page, "Copyright")
    recommended_signature = _signature_from_page(trainer, sample_page, "Recommended story alpha")
    duplicated_signature = _signature_from_page(trainer, duplicated_page, DUPLICATED_PARAGRAPH[:20])

    # every-page navigation/footer and the 3-of-4 recommendation rail are templates
    assert model.template_signatures >= {nav_signature, copyright_signature, recommended_signature}
    # text duplicated on one page only is not a template despite appearing twice in that page
    assert duplicated_signature not in model.template_signatures
    duplicated_record = model.evidence_for(duplicated_signature)
    assert duplicated_record["support_docs"] == 1
    assert duplicated_record["occurrences"] == 1

    nav_record = model.evidence_for(nav_signature)
    assert nav_record["support_docs"] == 4
    assert nav_record["support_ratio"] == 1.0
    assert nav_record["is_template"] is True
    assert nav_record["decision"]["meets_min_support_docs"] is True
    assert nav_record["decision"]["meets_min_support_ratio"] is True
    recommended_record = model.evidence_for(recommended_signature)
    assert recommended_record["support_docs"] == 3
    assert recommended_record["support_ratio"] == 0.75

    # short-block protection stays configurable: the two-word nav is vetoed when the threshold covers it
    strict_trainer = TemplateModelTrainer(
        filter_config=TemplateFilterConfig(min_support_docs=2, min_support_ratio=0.5, short_block_max_words=3))
    strict_trainer.add_document("train-1", _parse(_page(ARTICLE_A)))
    strict_trainer.add_document("train-2", _parse(_page(ARTICLE_B)))
    strict_trainer.add_document(
        "train-3",
        _parse(_page([DUPLICATED_PARAGRAPH, DUPLICATED_PARAGRAPH], copyright_year=2023)))
    strict_trainer.add_document(
        "train-4",
        _parse(_page(["A fourth article with its own completely distinct reporting text."],
                     with_recommended=False)))
    strict_model = strict_trainer.build_model()
    assert nav_signature not in strict_model.template_signatures
    assert strict_model.evidence_for(nav_signature)["vetoes"]["short_block_protection"] == 4

    # apply to the held-out batch: chrome is removed, article prose survives
    applied_filter = TemplateBoilerplateFilter(model)
    apply_doc = _parse(_page(ARTICLE_A))
    filters.MarkEverythingContentFilter().process(apply_doc)
    before = [block.is_content for block in apply_doc.text_blocks]
    assert all(before)
    changes = applied_filter.process(apply_doc)
    assert changes is True
    remaining_text = "\n".join(block.text for block in apply_doc.text_blocks if block.is_content)
    assert all(sentence in remaining_text for sentence in ARTICLE_A)
    assert "Home About" not in remaining_text
    assert "Copyright" not in remaining_text
    assert "Recommended story alpha details" not in remaining_text
    labeled = {block for block in apply_doc.text_blocks if TEMPLATE_LABEL in block.labels}
    assert len(labeled) == 3
    assert applied_filter.last_evidence  # decision evidence is surfaced for matched blocks
    assert all("block_text" not in item for item in applied_filter.last_evidence)

    # application must not mutate training statistics
    assert model.evidence_for(nav_signature)["support_docs"] == 4
    assert model.num_training_docs == 4

    # the filter slots into an existing FilterChain
    chain = filters.FilterChain([filters.MarkEverythingContentFilter(), applied_filter])
    chain_doc = _parse(_page(ARTICLE_B))
    chain.process(chain_doc)
    chain_text = chain_doc.content
    assert ARTICLE_B[0] in chain_text
    assert "Home About" not in chain_text

    # idempotent re-training of the same page-id
    trainer.add_document("train-1", _parse(_page(ARTICLE_A)))
    assert trainer.page_ids == ["train-1", "train-2", "train-3", "train-4"]
    rebuilt = trainer.build_model()
    assert rebuilt.evidence_for(nav_signature)["support_docs"] == 4

    # removing one sample changes the verdict through counts, not stale residue
    assert trainer.remove_document("train-4") is True
    assert trainer.remove_document("train-4") is False
    reduced = trainer.build_model()
    assert reduced.num_training_docs == 3
    assert reduced.evidence_for(nav_signature)["support_docs"] == 3
    assert nav_signature in reduced.template_signatures
    # recommendation rail existed on train-1..train-3, still supported by all remaining docs
    assert recommended_signature in reduced.template_signatures

    # dropping one rail-bearing page leaves the rail on a single page, below the minimum support count
    # use a stricter support threshold so the rail (3/4 docs) starts as a template and flips on removal
    strict_support = TemplateModelTrainer(
        filter_config=TemplateFilterConfig(min_support_docs=3, min_support_ratio=0.5, short_block_max_words=1))
    strict_support.add_document("train-1", _parse(_page(ARTICLE_A)))
    strict_support.add_document("train-2", _parse(_page(ARTICLE_B)))
    strict_support.add_document(
        "train-3",
        _parse(_page([DUPLICATED_PARAGRAPH, DUPLICATED_PARAGRAPH], copyright_year=2023)))
    strict_support.add_document(
        "train-4",
        _parse(_page(["A fourth article with its own completely distinct reporting text."],
                     with_recommended=False)))
    assert recommended_signature in strict_support.build_model().template_signatures
    strict_support.remove_document("train-2")
    rail_only_model = strict_support.build_model()
    rail_record = rail_only_model.evidence_for(recommended_signature)
    assert rail_record["support_docs"] == 2
    assert rail_record["is_template"] is False
    assert rail_record["decision"]["meets_min_support_docs"] is False
    assert recommended_signature not in rail_only_model.template_signatures

    # replacement revokes the old contribution on the main trainer
    trainer.add_document("train-1", _parse(_page([DUPLICATED_PARAGRAPH], with_recommended=False)))
    replaced_model = trainer.build_model()
    assert replaced_model.evidence_for(recommended_signature)["support_docs"] == 2

    # removing the last rail-bearing page revokes the residue entirely
    trainer.remove_document("train-2")
    assert recommended_signature not in trainer.build_model().template_signatures
    assert copyright_signature in trainer.build_model().template_signatures

    # versioned JSON round trip, carrying no HTML/body text
    model_path = tmp_path / "template-model.json"
    model.save(str(model_path))
    raw = json.loads(model_path.read_text(encoding="utf-8"))
    assert raw["format"] == "boilerpy3-template-model"
    assert raw["version"] == MODEL_VERSION
    serialized = model_path.read_text(encoding="utf-8")
    for leaked in ("<html", "<footer", "Copyright", "Recommended story", DUPLICATED_PARAGRAPH[:20]):
        assert leaked not in serialized

    loaded = TemplateModel.load(str(model_path))
    assert loaded.template_signatures == model.template_signatures
    loaded_filter = TemplateBoilerplateFilter(str(model_path))
    loaded_doc = _parse(_page(ARTICLE_A))
    loaded_filter.process(loaded_doc)
    assert "Home About" not in loaded_doc.content


def test_short_block_protection_is_configurable():
    short_text = "Login"
    long_text = "Copyright 2024 Example Site. All rights reserved worldwide."
    docs = [
        TextDocument([TextBlock(short_text, set(), 1, 0, 0, 0, 0),
                      TextBlock(long_text, set(), 9, 0, 0, 0, 1)])
        for _ in range(4)
    ]

    protected = TemplateModelTrainer(filter_config=TemplateFilterConfig(
        min_support_docs=3, min_support_ratio=0.5, short_block_max_words=3))
    for index, doc in enumerate(docs):
        protected.add_document(f"p{index}", doc)
    protected_model = protected.build_model()
    short_signature = block_signature(docs[0].text_blocks[0], protected.normalization_config)
    long_signature = block_signature(docs[0].text_blocks[1], protected.normalization_config)
    assert short_signature not in protected_model.template_signatures
    assert protected_model.evidence_for(short_signature)["vetoes"]["short_block_protection"] == 4
    assert long_signature in protected_model.template_signatures

    unprotected = TemplateModelTrainer(filter_config=TemplateFilterConfig(
        min_support_docs=3, min_support_ratio=0.5, protect_short_blocks=False))
    for index, doc in enumerate(docs):
        unprotected.add_document(f"p{index}", doc)
    unprotected_model = unprotected.build_model()
    assert short_signature in unprotected_model.template_signatures


def test_link_density_window_is_configurable():
    nav_block = TextBlock("Home About Contact", set(), 3, 3, 0, 0, 0)
    prose_block = TextBlock("Copyright 2024 Example Site. All rights reserved worldwide.", set(), 9, 0, 0, 0, 1)
    docs = [TextDocument([nav_block.clone(), prose_block.clone()]) for _ in range(4)]

    prose_only = TemplateModelTrainer(filter_config=TemplateFilterConfig(
        min_support_docs=3, min_support_ratio=0.5, short_block_max_words=0, max_link_density=0.5))
    for index, doc in enumerate(docs):
        prose_only.add_document(f"p{index}", doc)
    model = prose_only.build_model()
    nav_signature = block_signature(nav_block, prose_only.normalization_config)
    prose_signature = block_signature(prose_block, prose_only.normalization_config)
    assert nav_signature not in model.template_signatures
    assert model.evidence_for(nav_signature)["vetoes"]["link_density_out_of_range"] == 4
    assert prose_signature in model.template_signatures

    with pytest.raises(ValueError):
        TemplateFilterConfig(min_link_density=0.9, max_link_density=0.1)


def test_loading_rejects_unknown_version_and_config_mismatch(tmp_path):
    trainer = _train_batch()
    model = trainer.build_model()
    model_path = tmp_path / "model.json"
    model.save(str(model_path))

    raw = json.loads(model_path.read_text(encoding="utf-8"))
    raw["version"] = 999
    bad_version_path = tmp_path / "bad-version.json"
    bad_version_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(TemplateModelError):
        TemplateModel.load(str(bad_version_path))

    different_norm = NormalizationConfig(lowercase=False)
    with pytest.raises(TemplateModelError):
        TemplateModel.load(str(model_path), expected_normalization=different_norm)

    raw = json.loads(model_path.read_text(encoding="utf-8"))
    raw["format"] = "something-else"
    bad_format_path = tmp_path / "bad-format.json"
    bad_format_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(TemplateModelError):
        TemplateModel.load(str(bad_format_path))


def test_minimum_support_ratio_is_enforced():
    block_text = "Repeated boilerplate sentence with enough words to pass short block checks here."
    block = TextBlock(block_text, set(), 13, 0, 0, 0, 0)
    trainer = TemplateModelTrainer(filter_config=TemplateFilterConfig(min_support_docs=2, min_support_ratio=0.8))
    trainer.add_document("p1", TextDocument([block.clone()]))
    trainer.add_document("p2", TextDocument([block.clone()]))
    trainer.add_document("p3", TextDocument([TextBlock("Completely different article prose lives on this page.",
                                                       set(), 9, 0, 0, 0, 0)]))
    model = trainer.build_model()
    signature = block_signature(block, trainer.normalization_config)
    record = model.evidence_for(signature)
    assert record["support_docs"] == 2
    assert record["support_ratio"] == pytest.approx(2 / 3)
    assert record["is_template"] is False
    assert record["decision"]["meets_min_support_docs"] is True
    assert record["decision"]["meets_min_support_ratio"] is False


def test_structural_context_participates_in_signature():
    config = NormalizationConfig()
    first = TextBlock("Same text in different structural context", set(), 6, 0, 0, 0, 0)
    first.tag_level = 2
    second = TextBlock("Same text in different structural context", set(), 6, 0, 0, 0, 0)
    second.tag_level = 5
    assert block_signature(first, config) != block_signature(second, config)

    flat_config = NormalizationConfig(include_tag_level=False, include_markup_labels=False)
    assert block_signature(first, flat_config) == block_signature(second, flat_config)


def test_invalid_page_id_rejected():
    trainer = TemplateModelTrainer()
    with pytest.raises(ValueError):
        trainer.add_document("", TextDocument([TextBlock("Some words here.", set(), 3, 0, 0, 0, 0)]))


def test_repeated_training_is_idempotent_and_removal_rolls_vetoes_back():
    short_block = TextBlock("Login", set(), 1, 0, 0, 0, 0)
    long_block = TextBlock("Copyright 2024 Example Site. All rights reserved worldwide.", set(), 9, 0, 0, 0, 1)
    page = TextDocument([short_block.clone(), long_block.clone()])
    config = TemplateFilterConfig(min_support_docs=2, min_support_ratio=0.5, short_block_max_words=3)
    trainer = TemplateModelTrainer(filter_config=config)

    for _ in range(3):
        trainer.add_document("page-1", page)
    trainer.add_document("page-2", page)
    assert trainer.page_ids == ["page-1", "page-2"]

    model = trainer.build_model()
    short_signature = block_signature(short_block, trainer.normalization_config)
    long_signature = block_signature(long_block, trainer.normalization_config)
    assert model.evidence_for(short_signature)["vetoes"]["short_block_protection"] == 2
    assert model.evidence_for(long_signature)["support_docs"] == 2

    trainer.remove_document("page-1")
    reduced = trainer.build_model()
    assert reduced.evidence_for(short_signature)["vetoes"]["short_block_protection"] == 1
    assert reduced.evidence_for(long_signature)["support_docs"] == 1
    assert long_signature not in reduced.template_signatures

    trainer.remove_document("page-2")
    assert trainer.build_model().signatures == {}
