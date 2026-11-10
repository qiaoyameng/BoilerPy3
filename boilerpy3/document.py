"""
This file is licensed under the terms of the Apache License, Version 2.0. See the LICENSE file in the root of this
repository for complete details.
"""

import copy
import sys
from typing import Dict, List, Optional, Set, Tuple, Union


class ListInfo:
    """
    Describes one HTML list (<ol> or <ul>) found in the source document. Item numbering is fixed at parse time, so
    removing TextBlocks later (e.g. filtered boilerplate items) does not renumber the surviving items.
    """
    
    def __init__(self, list_id: int, ordered: bool, start: Optional[int] = None, reversed: bool = False,
                 path: Tuple[int, ...] = ()) -> None:
        self.list_id = list_id
        self.ordered = ordered
        self.start = start
        self.reversed = reversed
        # ids of enclosing lists, outermost first, including this list's own id
        self.path = tuple(path)
        # number of items that actually produced text (empty items never consume a number)
        self.num_items = 0
        # item_index -> explicit <li value="...">
        self.item_values: Dict[int, int] = {}
        self._numbers = None
    
    def effective_numbers(self) -> Dict[int, int]:
        """
        Computes the effective number of every item, honouring start, reversed and explicit li values. A reversed
        list without an explicit start counts down from the original number of items.
        """
        
        if self._numbers is None:
            if self.start is not None:
                start = self.start
            elif self.reversed:
                start = self.num_items
            else:
                start = 1
            step = -1 if self.reversed else 1
            numbers = {}
            counter = start
            for idx in range(self.num_items):
                if idx in self.item_values:
                    counter = self.item_values[idx]
                numbers[idx] = counter
                counter += step
            self._numbers = numbers
        return self._numbers
    
    def number_for(self, item_index: int) -> Optional[int]:
        return self.effective_numbers().get(item_index)
    
    def __repr__(self) -> str:
        kind = 'ol' if self.ordered else 'ul'
        return f"ListInfo(id={self.list_id}, {kind}, start={self.start}, reversed={self.reversed}, " \
               f"num_items={self.num_items})"


class ListItemRef:
    """
    Reference from a TextBlock to the list item it (partially) belongs to. Follows the TextBlock through filtering,
    merging and splitting; line_offset locates the item start within merged multi-line blocks.
    """
    
    __slots__ = ('list_id', 'item_index', 'value', 'is_item_start', 'line_offset')
    
    def __init__(self, list_id: int, item_index: int, value: Optional[int] = None, is_item_start: bool = False,
                 line_offset: int = 0) -> None:
        self.list_id = list_id
        self.item_index = item_index
        self.value = value
        self.is_item_start = is_item_start
        self.line_offset = line_offset
    
    def at_line(self, line_offset: int) -> 'ListItemRef':
        return ListItemRef(self.list_id, self.item_index, self.value, self.is_item_start, line_offset)
    
    def __repr__(self) -> str:
        return f"ListItemRef(list={self.list_id}, item={self.item_index}, value={self.value}, " \
               f"start={self.is_item_start}, line={self.line_offset})"


class StructuredContentItem:
    """
    One line of content together with its list context: the path of enclosing lists and, for the first line of a
    list item, the item's effective number.
    """
    
    def __init__(self, text: str, list_path: Tuple[int, ...] = (), number: Optional[int] = None,
                 ordered: bool = False, is_list_item_start: bool = False) -> None:
        self.text = text
        self.list_path = tuple(list_path)
        self.number = number
        self.ordered = ordered
        self.is_list_item_start = is_list_item_start
        self.depth = len(self.list_path)
    
    def __eq__(self, other) -> bool:
        if not isinstance(other, StructuredContentItem):
            return NotImplemented
        return (self.text, self.list_path, self.number, self.ordered, self.is_list_item_start) == \
               (other.text, other.list_path, other.number, other.ordered, other.is_list_item_start)
    
    def __repr__(self) -> str:
        return f"StructuredContentItem(text={self.text!r}, path={self.list_path}, number={self.number}, " \
               f"ordered={self.ordered}, start={self.is_list_item_start})"


def render_structured_content(items: List[StructuredContentItem]) -> str:
    """
    Renders structured content items to plain text, adding list markers (effective numbers for ordered lists,
    dashes for unordered lists) and indentation by nesting depth.
    """
    
    lines = []
    for item in items:
        if item.is_list_item_start:
            indent = '  ' * (item.depth - 1)
            marker = f'{item.number}. ' if item.ordered else '- '
            lines.append(f'{indent}{marker}{item.text}')
        elif item.depth > 0:
            lines.append(f"{'  ' * item.depth}{item.text}")
        else:
            lines.append(item.text)
    return ''.join(f'{line}\n' for line in lines)


class DefaultLabels:
    """
    Some pre-defined labels which can be used in conjunction with TextBlock
    """
    
    TITLE = "de.l3s.boilerpipe/TITLE"
    ARTICLE_METADATA = "de.l3s.boilerpipe/ARTICLE_METADATA"
    INDICATES_END_OF_TEXT = "de.l3s.boilerpipe/INDICATES_END_OF_TEXT"
    MIGHT_BE_CONTENT = "de.l3s.boilerpipe/MIGHT_BE_CONTENT"
    STRICTLY_NOT_CONTENT = "de.l3s.boilerpipe/STRICTLY_NOT_CONTENT"
    HR = "de.l3s.boilerpipe/HR"
    MARKUP_PREFIX = "<"


class TextBlock:
    """
    Describes a block of text. A block can be an "atomic" text element (i.e., a sequence of text that is not interrupted
    by any HTML markup) or a compound of such atomic elements.
    """
    
    EMPTY_START: 'TextBlock'
    EMPTY_END: 'TextBlock'
    
    def __init__(self, text: str, contained_text_elements=None, num_words: int = 0, num_words_in_anchor_text: int = 0,
                 num_words_in_wrapped_lines: int = 0, num_wrapped_lines: int = 0, offset_blocks: int = 0,
                 list_path: Tuple[int, ...] = (), list_item_refs: List[ListItemRef] = None) -> None:
        self.is_content = False
        self.labels = set()
        self.num_full_text_words = 0
        self.tag_level = 0
        # ids of the lists enclosing this block, outermost first
        self.list_path = tuple(list_path)
        # references to the list items this block (or parts of it) belongs to
        self.list_item_refs = list(list_item_refs) if list_item_refs else []
        
        self.text = text
        if contained_text_elements is None:
            contained_text_elements = set()
        self.contained_text_elements = contained_text_elements
        self.num_words = num_words
        self.num_words_in_anchor_text = num_words_in_anchor_text
        self.num_words_in_wrapped_lines = num_words_in_wrapped_lines
        self.num_wrapped_lines = num_wrapped_lines
        self.offset_blocks_start = offset_blocks
        self.offset_blocks_end = offset_blocks
        self.text_density = 0
        self.link_density = 0
        self._init_densities()
    
    def _init_densities(self) -> None:
        if self.num_words_in_wrapped_lines == 0:
            self.num_words_in_wrapped_lines = self.num_words
            self.num_wrapped_lines = 1
        self.text_density = self.num_words_in_wrapped_lines / float(self.num_wrapped_lines)
        self.link_density = 0 if self.num_words == 0 else self.num_words_in_anchor_text / float(self.num_words)
    
    def set_is_content(self, is_content: bool) -> bool:
        if is_content != self.is_content:
            self.is_content = is_content
            return True
        else:
            return False
    
    def merge_next(self, next_text_block: 'TextBlock') -> None:
        num_lines_before = self.text.count('\n') + 1 if self.text else 0
        if self.text is None:
            self.text = ""
        self.text = f'{self.text}\n{next_text_block.text}'
        if next_text_block.list_item_refs:
            self.list_item_refs = self.list_item_refs + [
                ref.at_line(ref.line_offset + num_lines_before) for ref in next_text_block.list_item_refs]
        if not self.list_path:
            self.list_path = next_text_block.list_path
        self.num_words += next_text_block.num_words
        self.num_words_in_anchor_text += next_text_block.num_words_in_anchor_text
        self.num_words_in_wrapped_lines += next_text_block.num_words_in_wrapped_lines
        self.num_wrapped_lines += next_text_block.num_wrapped_lines
        self.offset_blocks_start = min(self.offset_blocks_start, next_text_block.offset_blocks_start)
        self.offset_blocks_end = max(self.offset_blocks_end, next_text_block.offset_blocks_end)
        self._init_densities()
        self.is_content |= next_text_block.is_content
        self.contained_text_elements |= next_text_block.contained_text_elements
        self.num_full_text_words += next_text_block.num_full_text_words
        self.labels |= next_text_block.labels
        self.tag_level = min(self.tag_level, next_text_block.tag_level)
    
    def __repr__(self) -> str:
        return f"[{self.offset_blocks_start}-{self.offset_blocks_end}; tl={self.tag_level}; nw={self.num_words};" \
               f"nwl={self.num_wrapped_lines}; ld={self.link_density}]\t" \
               f"{'CONTENT' if self.is_content else 'boilerplate'},{self.labels}\n{self.text}"
    
    def add_label(self, label) -> None:
        """
        Adds an arbitrary String label to this TextBlock.

        :param label: The label
        """
        
        self.labels.add(label)
    
    def has_label(self, label: str) -> bool:
        """
        Checks whether this TextBlock has the given label.

        :param label: The label
        :return: <code>true</code> if this block is marked by the given label.
        """
        
        return label in self.labels
    
    def remove_label(self, label: str) -> bool:
        try:
            self.labels.remove(label)
            return True
        except KeyError:
            return False
    
    def add_labels(self, *labels: Union[List, Set, Tuple]):
        """
        Adds a set of labels to this TextBlock. <code>null</code>-references are silently ignored.

        :param labels: The labels to be added.
        """
        
        if len(labels) == 0 or labels[0] is None:
            return
        if self.labels is None:
            self.labels = set()
        elif len(labels) == 1 and isinstance(labels[0], (set, list)):
            self.labels |= set(labels[0])
        else:
            self.labels |= set(labels)
    
    def clone(self):
        try:
            clone = copy.copy(self)
        except copy.error:
            raise copy.error
        if self.labels is not None:
            clone.labels = self.labels.copy()
        if self.contained_text_elements is not None:
            clone.contained_text_elements = self.contained_text_elements.copy()
        clone.list_path = tuple(self.list_path)
        clone.list_item_refs = list(self.list_item_refs)
        
        return clone


TextBlock.EMPTY_START = TextBlock("", set(), 0, 0, 0, 0, -1)
TextBlock.EMPTY_END = TextBlock("", set(), 0, 0, 0, 0, sys.maxsize)


class TextDocument:
    """
    A text document, consisting of one or more TextBlocks.
    """
    
    def __init__(self, text_blocks: List[TextBlock], title: str = None, lists: List[ListInfo] = None):
        """
        Creates a new TextDocument with given TextBlocks and given title.

        :param text_blocks: The text blocks of this document.
        :param title: The "main" title for this text document.
        :param lists: The lists (ol/ul) found in the source document, used for list-aware output.
        """
        
        self.title = title
        self.text_blocks = text_blocks
        self.lists = list(lists) if lists else []
    
    @property
    def content(self) -> str:
        """
        Returns the TextDocument's content.
        
        :return: The content text.
        """
        
        return self.get_text(True, False)
    
    def get_text(self, include_content: bool, include_non_content: bool) -> str:
        """
        Returns the TextDocument's content, non-content or both.
        
        :param include_content: Whether to include TextBlocks marked as "content".
        :param include_non_content: Whether to include TextBlocks marked as "non-content".
        :return: The text.
        """
        
        sb = []
        for block in self.text_blocks:
            if block.is_content:
                if not include_content:
                    continue
            else:
                if not include_non_content:
                    continue
            sb.append(f'{block.text}\n')
        
        return ''.join(sb)
    
    @property
    def list_aware_content(self) -> str:
        """
        Returns the TextDocument's content rendered with list markers and indentation derived from the source
        document's lists. Markers are added at render time only and never affect word counts or densities.
        
        :return: The list-aware content text.
        """
        
        return render_structured_content(self.get_structured_content(True, False))
    
    def get_structured_content(self, include_content: bool = True,
                               include_non_content: bool = False) -> List[StructuredContentItem]:
        """
        Returns the document's text as structured content items, each carrying the path of enclosing lists and,
        for the first line of a list item, the item's effective number.
        
        :param include_content: Whether to include TextBlocks marked as "content".
        :param include_non_content: Whether to include TextBlocks marked as "non-content".
        :return: The structured content items.
        """
        
        lists_by_id = {info.list_id: info for info in self.lists}
        items = []
        for block in self.text_blocks:
            if block.is_content:
                if not include_content:
                    continue
            else:
                if not include_non_content:
                    continue
            start_refs = {}
            for ref in block.list_item_refs:
                if ref.is_item_start:
                    start_refs.setdefault(ref.line_offset, ref)
            for line_offset, line in enumerate(block.text.split('\n')):
                ref = start_refs.get(line_offset)
                if ref is not None and ref.list_id in lists_by_id:
                    info = lists_by_id[ref.list_id]
                    number = info.number_for(ref.item_index) if info.ordered else None
                    items.append(StructuredContentItem(line, info.path, number, info.ordered, True))
                else:
                    ordered = False
                    if block.list_path and block.list_path[-1] in lists_by_id:
                        ordered = lists_by_id[block.list_path[-1]].ordered
                    items.append(StructuredContentItem(line, block.list_path, None, ordered, False))
        return items
    
    @property
    def debug_string(self) -> str:
        """
        Returns detailed debugging information about the contained TextBlocks.

        :return: Debug information.
        """
        
        return ''.join([f'{tb}\n' for tb in self.text_blocks])


class TextDocumentStatistics:
    """
    Provides shallow statistics on a given TextDocument
    """
    
    def __init__(self, doc: TextDocument, content_only: bool) -> None:
        """
        Computes statistics on a given TextDocument.
        
        :param doc: The TextDocument.
        :param content_only: if true then o
        """
        
        self.num_words = 0
        self.num_blocks = 0
        for tb in doc.text_blocks:
            if content_only and not tb.is_content:
                continue
            self.num_words += tb.num_words
            self.num_blocks += 1
    
    def avg_num_words(self) -> float:
        """
        Returns the average number of words at block-level (= overall number of words divided by the number of blocks).
        
        :return: Average
        """
        
        return self.num_words / float(self.num_blocks)
