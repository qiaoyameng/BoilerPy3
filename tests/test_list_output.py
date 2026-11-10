from boilerpy3.document import ListItemRef, TextBlock, TextDocument, render_structured_content
from boilerpy3.extractors import Extractor, KeepEverythingExtractor
from boilerpy3.filters import BoilerpipeFilter, BoilerplateBlockFilter, FilterChain, MarkEverythingContentFilter, \
    SplitParagraphBlocksFilter

LIST_PAGE = """
<html><head><title>Steps</title></head><body>
<p>Intro paragraph with some real content words here.</p>
<ol start="-2">
  <li>First step of the procedure explained briefly.</li>
  <li>ADVERTISEMENT buy cheap stuff while you read.</li>
  <li>Third step keeps its original number.</li>
  <li value="7">Fourth step jumps numbering explicitly.
    <ol reversed>
      <li>Nested sub step alpha text here.</li>
      <li>Nested sub step beta text here.</li>
      <li>Nested sub step gamma ADVERTISEMENT here.</li>
    </ol>
  </li>
  <li><p>Final step first paragraph here.</p><p>Continuation paragraph same item.</p></li>
  <li></li>
</ol>
<ul>
  <li>Unordered point one.</li>
  <li>Unordered point two.</li>
</ul>
</body></html>
"""


class AdvertisementFilter(BoilerpipeFilter):
    def process(self, doc) -> bool:
        changes = False
        for tb in doc.text_blocks:
            if 'ADVERTISEMENT' in tb.text:
                changes |= tb.set_is_content(False)
        return changes


def make_extractor():
    return Extractor(FilterChain([
        MarkEverythingContentFilter(),
        AdvertisementFilter(),
        BoilerplateBlockFilter()
    ]))


def test_list_aware_content_rendering():
    extractor = make_extractor()
    assert extractor.get_list_aware_content(LIST_PAGE) == (
        "Intro paragraph with some real content words here.\n"
        "-2. First step of the procedure explained briefly.\n"
        "0. Third step keeps its original number.\n"
        "7. Fourth step jumps numbering explicitly.\n"
        "  3. Nested sub step alpha text here.\n"
        "  2. Nested sub step beta text here.\n"
        "8. Final step first paragraph here.\n"
        "  Continuation paragraph same item.\n"
        "- Unordered point one.\n"
        "- Unordered point two.\n"
    )


def test_structured_content_items():
    extractor = make_extractor()
    items = extractor.get_structured_content(LIST_PAGE)
    assert [(item.text, item.list_path, item.number, item.is_list_item_start) for item in items] == [
        ("Intro paragraph with some real content words here.", (), None, False),
        ("First step of the procedure explained briefly.", (0,), -2, True),
        ("Third step keeps its original number.", (0,), 0, True),
        ("Fourth step jumps numbering explicitly.", (0,), 7, True),
        ("Nested sub step alpha text here.", (0, 1), 3, True),
        ("Nested sub step beta text here.", (0, 1), 2, True),
        ("Final step first paragraph here.", (0,), 8, True),
        ("Continuation paragraph same item.", (0,), None, False),
        ("Unordered point one.", (2,), None, True),
        ("Unordered point two.", (2,), None, True),
    ]
    assert [item.depth for item in items] == [0, 1, 1, 1, 2, 2, 1, 1, 1, 1]
    assert [item.ordered for item in items[1:8]] == [True] * 7
    assert [item.ordered for item in items[8:]] == [False, False]


def test_filtered_item_keeps_original_number():
    # the second item (number -1) is filtered as an advertisement; the third item must still use its original
    # number 0 instead of being renumbered to -1
    items = make_extractor().get_structured_content(LIST_PAGE)
    numbers = [item.number for item in items if item.list_path == (0,) and item.is_list_item_start]
    assert numbers == [-2, 0, 7, 8]


def test_reversed_list_start_uses_original_item_count():
    # the reversed nested list originally had 3 items, so it counts down from 3 even though the last item
    # (gamma) was filtered out, leaving only 2 content blocks
    doc = make_extractor().get_doc(LIST_PAGE)
    nested_list = doc.lists[1]
    assert nested_list.reversed
    assert nested_list.num_items == 3
    items = doc.get_structured_content()
    nested_numbers = [item.number for item in items if item.list_path == (0, 1)]
    assert nested_numbers == [3, 2]


def test_empty_items_do_not_consume_numbers():
    doc = KeepEverythingExtractor().get_doc(LIST_PAGE)
    # 5 non-empty items; the empty <li></li> at the end must not count
    assert doc.lists[0].num_items == 5
    assert doc.lists[0].effective_numbers() == {0: -2, 1: -1, 2: 0, 3: 7, 4: 8}


def test_plain_content_output_unchanged():
    extractor = make_extractor()
    content = extractor.get_content(LIST_PAGE)
    assert content == (
        "Intro paragraph with some real content words here.\n"
        "First step of the procedure explained briefly.\n"
        "Third step keeps its original number.\n"
        "Fourth step jumps numbering explicitly.\n"
        "Nested sub step alpha text here.\n"
        "Nested sub step beta text here.\n"
        "Final step first paragraph here.\n"
        "Continuation paragraph same item.\n"
        "Unordered point one.\n"
        "Unordered point two.\n"
    )


def test_markers_not_counted_as_words():
    doc = make_extractor().get_doc(LIST_PAGE)
    first_item_block = next(tb for tb in doc.text_blocks if tb.text.startswith('First step'))
    # 7 words of original text; the rendered "-2." marker must not affect word counts or densities
    assert first_item_block.num_words == 7
    assert first_item_block.num_words_in_wrapped_lines == 7
    assert first_item_block.text_density == 7.0
    # rendering must not mutate the blocks
    _ = doc.list_aware_content
    assert first_item_block.num_words == 7
    assert first_item_block.text == 'First step of the procedure explained briefly.'


def test_list_metadata_survives_merge():
    block1 = TextBlock("first line", list_path=(0,), list_item_refs=[ListItemRef(0, 0, None, True)])
    block2 = TextBlock("second line", list_path=(0,), list_item_refs=[ListItemRef(0, 1, 5, True)])
    block1.merge_next(block2)
    assert [(ref.item_index, ref.value, ref.line_offset) for ref in block1.list_item_refs] == [(0, None, 0), (1, 5, 1)]
    assert block1.list_path == (0,)


def test_list_metadata_survives_split():
    block = TextBlock("para one\npara two", list_path=(0,),
                      list_item_refs=[ListItemRef(0, 0, None, True, 0), ListItemRef(0, 1, None, True, 1)])
    doc = TextDocument([block])
    assert SplitParagraphBlocksFilter().process(doc)
    assert len(doc.text_blocks) == 2
    for idx, split_block in enumerate(doc.text_blocks):
        assert split_block.list_path == (0,)
        assert [(ref.item_index, ref.line_offset) for ref in split_block.list_item_refs] == [(idx, 0)]


def test_list_metadata_survives_clone():
    block = TextBlock("text", list_path=(0, 1), list_item_refs=[ListItemRef(1, 2, 3, True)])
    clone = block.clone()
    assert clone.list_path == (0, 1)
    assert clone.list_item_refs == block.list_item_refs
    assert clone.list_item_refs is not block.list_item_refs


def test_render_structured_content_indentation():
    doc = KeepEverythingExtractor().get_doc(LIST_PAGE)
    items = doc.get_structured_content()
    rendered = render_structured_content(items)
    assert "\n  3. Nested sub step alpha text here.\n" in rendered
    assert rendered.startswith("Intro paragraph")
