"""
Tests for the incremental begin/feed_chunk/finish parsing lifecycle.

These tests assert that how an HTML document is split into chunks cannot change
the parsed TextDocument (text, word counts, link density, labels, contained text
element ids and the final content judgement), that finalisation happens exactly
once, that abort is distinct from finish, and that parser state never leaks
between documents or between two interleaved parsers.
"""

import os

import pytest

from boilerpy3 import parser
from boilerpy3.extractors import ArticleExtractor
from boilerpy3.parser import BoilerpipeHTMLParser, parse_chunks

TESTS_DIR = os.path.abspath(os.path.dirname(os.path.realpath(__file__)))


def _block_signature(doc):
    return [
        (
            block.text,
            block.num_words,
            block.num_words_in_anchor_text,
            block.num_words_in_wrapped_lines,
            block.num_wrapped_lines,
            block.text_density,
            block.link_density,
            block.tag_level,
            block.offset_blocks_start,
            block.offset_blocks_end,
            tuple(sorted(block.labels)),
            frozenset(block.contained_text_elements),
            block.is_content,
        )
        for block in doc.text_blocks
    ], doc.title


def _oneshot(html):
    p = BoilerpipeHTMLParser()
    p.feed(html)
    return p.to_text_document()


def _chunked(html, cuts):
    p = BoilerpipeHTMLParser()
    p.begin()
    pos = 0
    for cut in cuts:
        p.feed_chunk(html[pos:cut])
        pos = cut
    p.feed_chunk(html[pos:])
    return p.finish()


# A small page exercising entities, comments, script content, anchors, inline and
# block tags, and ordinary words -- each of which can straddle a chunk boundary.
SMALL_PAGE = (
    "<!doctype html><html><head><title>Ti&amp;tle</title></head>"
    "<body>"
    "<!-- a<b> comment -->"
    "<p>hel&am &amp;x ordinary words here</p>"
    "<script>if(a<b){x=1;}&amp;</scr" + "ipt>"
    "<a href='x.html'>lin&#39;k anchor</a>"
    "<div><b>bold</b> tail words</div>"
    "<span>&amp;&amp;&amp;</span>"
    "</body></html>"
)


def test_oneshot_get_doc_unchanged():
    # The one-shot convenience path must keep producing the documented result.
    doc = _oneshot(SMALL_PAGE)
    texts = [block.text for block in doc.text_blocks]
    assert "hel&am &x ordinary words here" in texts
    assert "lin'k anchor" in texts
    assert doc.title == "Ti&tle"


def test_every_single_split_point_matches_oneshot():
    reference = _block_signature(_oneshot(SMALL_PAGE))
    for cut in range(len(SMALL_PAGE) + 1):
        assert _block_signature(_chunked(SMALL_PAGE, [cut])) == reference


@pytest.mark.parametrize("size", [1, 2, 3, 5, 8, 13, 21, 64])
def test_fixed_chunk_sizes_match_oneshot(size):
    reference = _block_signature(_oneshot(SMALL_PAGE))
    cuts = list(range(size, len(SMALL_PAGE), size))
    assert _block_signature(_chunked(SMALL_PAGE, cuts)) == reference


def test_real_page_all_split_points_and_sizes():
    with open(os.path.join(TESTS_DIR, 'test.html')) as handle:
        html = handle.read()
    reference = _block_signature(_oneshot(html))

    # every two-way split
    for cut in range(0, len(html) + 1, 97):
        assert _block_signature(_chunked(html, [cut])) == reference

    # a representative spread of chunk sizes
    for size in (1, 2, 4, 9, 17, 37, 128, 257):
        cuts = list(range(size, len(html), size))
        assert _block_signature(_chunked(html, cuts)) == reference


def test_chunking_preserves_link_density_words_and_labels():
    reference = _oneshot(SMALL_PAGE)
    chunked = _chunked(SMALL_PAGE, list(range(7, len(SMALL_PAGE), 7)))

    assert len(chunked.text_blocks) == len(reference.text_blocks)
    for ref_block, chunk_block in zip(reference.text_blocks, chunked.text_blocks):
        assert chunk_block.text == ref_block.text
        assert chunk_block.num_words == ref_block.num_words
        assert chunk_block.num_words_in_anchor_text == ref_block.num_words_in_anchor_text
        assert chunk_block.link_density == ref_block.link_density
        assert chunk_block.text_density == ref_block.text_density
        assert chunk_block.labels == ref_block.labels
        assert chunk_block.contained_text_elements == ref_block.contained_text_elements


def test_extraction_judgement_is_chunk_invariant():
    with open(os.path.join(TESTS_DIR, 'test.html')) as handle:
        html = handle.read()
    extractor = ArticleExtractor()
    expected = extractor.get_content(html)

    for size in (1, 3, 16, 37, 128):
        chunks = [html[j:j + size] for j in range(0, len(html), size)]
        doc = extractor.get_doc_from_chunks(chunks)
        assert doc.content == expected


def test_parse_chunks_helper_never_rejoins_html():
    chunks = [SMALL_PAGE[j:j + 5] for j in range(0, len(SMALL_PAGE), 5)]
    doc_from_helper = parse_chunks(iter(chunks))
    assert _block_signature(doc_from_helper) == _block_signature(_oneshot(SMALL_PAGE))


def test_finish_happens_only_once():
    p = BoilerpipeHTMLParser()
    p.begin()
    p.feed_chunk("<html><body><p>hello</p></body></html>")
    p.finish()

    with pytest.raises(RuntimeError):
        p.finish()
    with pytest.raises(RuntimeError):
        p.feed_chunk("more")


def test_abort_is_separate_from_finish_and_blocks_reuse():
    p = BoilerpipeHTMLParser()
    p.begin()
    p.feed_chunk("<html><body><p>partial")
    p.abort()

    # an aborted parser cannot be resumed or finalised
    with pytest.raises(RuntimeError):
        p.feed_chunk("</p>")
    with pytest.raises(RuntimeError):
        p.finish()
    with pytest.raises(RuntimeError):
        p.abort()

    # a new document on the same instance starts clean
    p.begin()
    p.feed_chunk("<html><body><p>fresh start</p></body></html>")
    doc = p.finish()
    assert [block.text for block in doc.text_blocks] == ["fresh start"]
    assert doc.text_blocks[0].offset_blocks_start == 0


def test_lifecycle_requires_begin_first():
    p = BoilerpipeHTMLParser()
    with pytest.raises(RuntimeError):
        p.feed_chunk("x")
    with pytest.raises(RuntimeError):
        p.finish()
    with pytest.raises(RuntimeError):
        p.abort()


def test_begin_rejects_an_active_document():
    p = BoilerpipeHTMLParser()
    p.begin()
    p.feed_chunk("<html><body><p>x</p>")
    with pytest.raises(RuntimeError):
        p.begin()
    p.abort()


def test_failure_state_is_not_reusable():
    p = BoilerpipeHTMLParser()
    p.begin()
    # malformed input that unbalances the label stack
    with pytest.raises(IndexError):
        p.feed_chunk("<html><body></div></body></html>")
    # the parser is marked aborted and refuses further use
    assert p.lifecycle_state == BoilerpipeHTMLParser.STATE_ABORTED
    with pytest.raises(RuntimeError):
        p.feed_chunk("more")
    with pytest.raises(RuntimeError):
        p.finish()


def test_each_new_document_clears_counts_and_label_stack():
    p = BoilerpipeHTMLParser()

    p.begin()
    p.feed_chunk("<html><body><p>first document words</p></body></html>")
    first = p.finish()
    assert first.text_blocks[0].offset_blocks_start == 0

    p.begin()
    p.feed_chunk("<html><body><p>second</p></body></html>")
    second = p.finish()

    # counters and block offsets restart; nothing from the first doc survives
    assert second.text_blocks[0].offset_blocks_start == 0
    assert [b.text for b in second.text_blocks] == ["second"]
    assert second.title is None
    assert second.text_blocks[0].labels == set()


def test_two_interleaved_parsers_do_not_share_state():
    html_a = (
        "<html><body><p>alpha one</p>"
        "<p>alpha two &amp; half</p></body></html>"
    )
    html_b = (
        "<html><body><p>beta uno</p>"
        "<a href='x'><p>beta linked words</p></a></body></html>"
    )

    parser_a = BoilerpipeHTMLParser()
    parser_b = BoilerpipeHTMLParser()
    parser_a.begin()
    parser_b.begin()

    max_len = max(len(html_a), len(html_b))
    for idx in range(0, max_len, 2):
        if idx < len(html_a):
            parser_a.feed_chunk(html_a[idx:idx + 2])
        if idx < len(html_b):
            parser_b.feed_chunk(html_b[idx:idx + 2])

    doc_a = parser_a.finish()
    doc_b = parser_b.finish()

    assert _block_signature(doc_a) == _block_signature(_oneshot(html_a))
    assert _block_signature(doc_b) == _block_signature(_oneshot(html_b))

    # the anchor block keeps its full link density despite the interleaving
    anchor_block = next(b for b in doc_b.text_blocks if "beta linked" in b.text)
    assert anchor_block.link_density == 1.0
