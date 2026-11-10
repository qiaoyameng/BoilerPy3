"""
Tests for the code-aware content mode: <pre> blocks keep their whitespace verbatim, get their own block type
(TextBlock.is_code), are exempt from word-density tokenization and block fusion, and are still classified as
content only based on the surrounding article context.
"""

from boilerpy3 import filters
from boilerpy3.document import TextBlock
from boilerpy3.extractors import DefaultExtractor, Extractor, KeepEverythingExtractor

extractor = Extractor(None)  # noqa

LEAD_IN = "Some lead in text for the article body."
LEAD_OUT = "Some closing text for the article body."

# nested syntax-highlight spans, a tab, a blank line, CRLF line endings, a literal "&lt;" (decoded once to "<")
# and a literal "&amp;lt;" (decoded once to "&lt;")
CODE_HTML = (
    '<pre class="prettyprint"><code><span class="k">def</span> <span class="nf">f</span><span class="p">():</span>\r\n'
    '\t<span class="k">return</span> <span><span class="mi">1</span> &lt; 2</span>  # &amp;lt;\r\n'
    '\r\n'
    '<span class="c"># done</span></code></pre>'
)
CODE_TEXT = "def f():\n\treturn 1 < 2  # &lt;\n\n# done"


def _code_page(pre_html=CODE_HTML):
    return f"<html><head><title>Code</title></head><body><article>" \
           f"<p>{LEAD_IN}</p>{pre_html}<p>{LEAD_OUT}</p></article></body></html>"


def _code_blocks(doc):
    return [tb for tb in doc.text_blocks if tb.is_code]


def test_pre_exact_characters():
    doc = extractor.parse_doc(_code_page())
    code_blocks = _code_blocks(doc)
    assert len(code_blocks) == 1
    assert code_blocks[0].text == CODE_TEXT


def test_pre_crlf_and_cr_normalized_to_lf():
    doc = extractor.parse_doc(_code_page("<pre>a\r\nb\rc\nd</pre>"))
    assert _code_blocks(doc)[0].text == "a\nb\nc\nd"


def test_pre_single_leading_and_trailing_newlines_dropped():
    doc = extractor.parse_doc(_code_page("<pre>\nx = 1\n\n</pre>"))
    assert _code_blocks(doc)[0].text == "x = 1"


def test_pre_code_tag_does_not_duplicate_content():
    doc = extractor.parse_doc(_code_page())
    full_text = doc.get_text(True, True)
    assert full_text.count("def f():") == 1
    # structured (block-level) and plain-text output expose the same code content
    assert CODE_TEXT in full_text
    assert KeepEverythingExtractor().get_content(_code_page()).count(CODE_TEXT) == 1


def test_inline_code_in_paragraph():
    html = "<html><body><p>Use <code>foo()</code> to start it now.</p></body></html>"
    doc = extractor.parse_doc(html)
    assert [tb.text for tb in doc.text_blocks] == ["Use foo() to start it now."]
    assert not doc.text_blocks[0].is_code


def test_code_block_neutral_word_statistics():
    block = _code_blocks(extractor.parse_doc(_code_page()))[0]
    assert block.num_words == 0
    assert block.num_words_in_anchor_text == 0
    assert block.text_density == 0
    assert block.link_density == 0


def test_code_block_not_fused_with_paragraphs():
    doc = extractor.parse_doc(_code_page())
    for tb in doc.text_blocks:
        tb.set_is_content(True)
    filters.BlockProximityFusion(1, True, False).process(doc)
    filters.SimpleBlockFusionProcessor().process(doc)
    filters.LabelFusion().process(doc)
    code_blocks = _code_blocks(doc)
    assert len(code_blocks) == 1
    assert code_blocks[0].text == CODE_TEXT
    assert any(tb.text == LEAD_IN for tb in doc.text_blocks)
    assert any(tb.text == LEAD_OUT for tb in doc.text_blocks)


def test_code_block_not_split_at_newlines():
    doc = extractor.parse_doc(_code_page())
    for tb in doc.text_blocks:
        tb.set_is_content(True)
    filters.SplitParagraphBlocksFilter().process(doc)
    assert [tb.text for tb in _code_blocks(doc)] == [CODE_TEXT]


def test_merge_next_code_flag():
    code_block = TextBlock("x = 1")
    code_block.is_code = True
    text_block = TextBlock("hello")
    code_block.merge_next(text_block)
    assert not code_block.is_code
    other_code_block = TextBlock("y = 2")
    other_code_block.is_code = True
    code_block2 = TextBlock("x = 1")
    code_block2.is_code = True
    code_block2.merge_next(other_code_block)
    assert code_block2.is_code


def _dense_paragraph(seed):
    return ' '.join(f"{seed}word{i}" for i in range(40))


def test_default_extractor_keeps_article_pre_but_drops_header_pre():
    html = (
        "<html><head><title>Article</title></head><body>"
        "<header><nav><a href='/a'>Alpha</a> <a href='/b'>Beta</a></nav>"
        "<pre>BUY OUR STUFF NOW</pre>"
        "<nav><a href='/c'>Gamma</a> <a href='/d'>Delta</a></nav></header>"
        "<article>"
        f"<p>{_dense_paragraph('a')}</p>"
        f"{CODE_HTML}"
        f"<p>{_dense_paragraph('b')}</p>"
        f"<p>{_dense_paragraph('c')}</p>"
        "</article></body></html>"
    )
    doc = DefaultExtractor().get_doc(html)
    code_blocks = _code_blocks(doc)
    assert len(code_blocks) == 2
    article_code = next(tb for tb in code_blocks if tb.text == CODE_TEXT)
    header_code = next(tb for tb in code_blocks if "BUY OUR STUFF" in tb.text)
    assert article_code.is_content
    assert not header_code.is_content
    content = doc.content
    assert CODE_TEXT in content
    assert "BUY OUR STUFF" not in content
