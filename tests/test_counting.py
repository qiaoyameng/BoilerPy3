import pytest

from boilerpy3 import filters
from boilerpy3.counting import (DEFAULT_STRATEGY, MIXED_CJK_STRATEGY, DefaultWordCountingStrategy,
                                MixedCjkWordCountingStrategy)
from boilerpy3.document import TextBlock, TextDocument
from boilerpy3.extractors import Extractor, NumWordsRulesExtractor

mixed = MixedCjkWordCountingStrategy()
default = DefaultWordCountingStrategy()


def _parse(html, strategy=None):
    return Extractor(None, counting_strategy=strategy).parse_doc(html)


def test_pure_chinese_page():
    html = "<html><body><p>我爱自然语言处理</p></body></html>"
    
    mixed_block = _parse(html, mixed).text_blocks[0]
    # each Han character counts as one word
    assert mixed_block.num_words == 8
    assert mixed_block.text_density == 8.0
    
    # legacy strategy treats the whole run as a single token (old behavior unchanged)
    default_block = _parse(html).text_blocks[0]
    assert default_block.num_words == 1
    assert default_block.text_density == 1.0


def test_pure_english_page():
    html = "<html><body><p>Hello world, it's a test.</p></body></html>"
    block = _parse(html, mixed).text_blocks[0]
    # punctuation does not count; internal apostrophe belongs to the word
    assert block.num_words == 5
    assert mixed.tokenize("Hello world, it's a test.") == ['Hello', 'world', ',', "it's", 'a', 'test', '.']


def test_mixed_page_with_links():
    html = "<html><body><p>点击<a href='x'>这里 read more</a>查看更多</p></body></html>"
    block = _parse(html, mixed).text_blocks[0]
    # same rule inside and outside the anchor: 2 + (2 + 1 + 1) + 4 words
    assert block.num_words == 10
    assert block.num_words_in_anchor_text == 4
    assert block.link_density == pytest.approx(0.4)


def test_combining_marks_follow_previous_character():
    # 'cafe' + combining acute, Han + combining breve: each stays a single word/token
    assert mixed.tokenize('café 中̆') == ['café', '中̆']
    assert mixed.count('café 中̆').num_words == 2


def test_token_width_convention():
    # explicit Unicode width convention: combining marks 0, East Asian Wide/Fullwidth 2, others 1
    assert mixed.token_width('中') == 2
    assert mixed.token_width('a') == 1
    assert mixed.token_width('́') == 0
    assert mixed.token_width('中文ab') == 6
    # default strategy keeps its code-point convention
    assert default.token_width('中文ab') == 4


def test_wrapped_lines_use_character_width_not_bytes():
    # 28 Han characters take 28 * 2 + 27 (spaces) = 83 columns > 80, so the last word wraps;
    # counting bytes (3/char in UTF-8) or 1 column per character would give different line counts
    block = TextBlock('中' * 28, counting_strategy=mixed)
    assert block.num_words == 28
    assert block.num_words_in_wrapped_lines == 27
    assert block.num_wrapped_lines == 1
    assert block.text_density == 27.0
    # 27 characters fit on one line (27 * 2 + 26 = 80)
    assert TextBlock('中' * 27, counting_strategy=mixed).num_words_in_wrapped_lines == 27


def test_split_then_merge_matches_direct_count():
    text = "第一段文字\n第二段文字 with English words"
    direct = TextBlock(text, counting_strategy=mixed)
    
    parts = [TextBlock(p, counting_strategy=mixed) for p in text.split('\n')]
    merged = parts[0]
    merged.merge_next(parts[1])
    
    assert merged.text == direct.text
    assert merged.num_words == direct.num_words == 13
    assert merged.num_words_in_anchor_text == direct.num_words_in_anchor_text


def test_split_paragraph_blocks_filter_uses_document_strategy():
    text = "第一段文字\n第二段文字 with English words"
    doc = TextDocument([TextBlock(text, counting_strategy=mixed)], counting_strategy=mixed)
    filters.SplitParagraphBlocksFilter().process(doc)
    assert [b.num_words for b in doc.text_blocks] == [5, 8]
    assert sum(b.num_words for b in doc.text_blocks) == TextBlock(text, counting_strategy=mixed).num_words
    
    # legacy documents (no strategy) keep the historical behavior: no recounting on split
    legacy_doc = TextDocument([TextBlock(text)])
    filters.SplitParagraphBlocksFilter().process(legacy_doc)
    assert [b.num_words for b in legacy_doc.text_blocks] == [0, 0]


def test_classification_follows_new_counts():
    html = f"<html><body><p>{'中' * 30}</p></body></html>"
    # NumWordsRulesClassifier: > 16 words => content
    assert NumWordsRulesExtractor(counting_strategy=mixed).get_content(html).strip() == '中' * 30
    # legacy strategy sees a single word => not content (old output unchanged)
    assert NumWordsRulesExtractor().get_content(html) == ''


def test_classification_follows_new_density():
    # DensityRulesClassifier: content because prev block density 5 > 4 and curr density 9 <= 9
    html = f"<html><body><p>{'中' * 5}</p><p>{'中' * 9}</p></body></html>"
    doc = _parse(html, mixed)
    assert [b.text_density for b in doc.text_blocks] == [5.0, 9.0]
    filters.DensityRulesClassifier().process(doc)
    assert [b.is_content for b in doc.text_blocks] == [False, True]
    
    # same page with the legacy strategy: densities of 1 classify nothing as content
    legacy_doc = _parse(html)
    assert [b.text_density for b in legacy_doc.text_blocks] == [1.0, 1.0]
    filters.DensityRulesClassifier().process(legacy_doc)
    assert [b.is_content for b in legacy_doc.text_blocks] == [False, False]


def test_document_carries_strategy_from_extractor():
    doc = _parse("<html><body><p>测试</p></body></html>", MIXED_CJK_STRATEGY)
    assert doc.counting_strategy is MIXED_CJK_STRATEGY
    # no strategy passed => legacy default, document stays strategy-less
    assert _parse("<html><body><p>测试</p></body></html>").counting_strategy is None
    assert DEFAULT_STRATEGY.name == 'default'
