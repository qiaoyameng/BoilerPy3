"""
This file is licensed under the terms of the Apache License, Version 2.0. See the LICENSE file in the root of this
repository for complete details.
"""

import re
from html import unescape
from html.parser import HTMLParser
from logging import getLogger
from typing import Dict, Set
from xml.sax.handler import ContentHandler
from xml.sax.xmlreader import AttributesImpl

from boilerpy3.document import DefaultLabels, TextBlock, TextDocument

logger = getLogger('boilerpy3')


# ----------------------------------------------------------------------------
#                     INCREMENTAL PARSING ENTITY DETECTION
# ----------------------------------------------------------------------------

# An entity reference the stdlib HTMLParser is still waiting to complete.
# The trailing forms (``&``, ``&a``, ``&#`` ...) stay buffered across feeds.
PENDING_ENTITY_NAME = re.compile(r'&[a-zA-Z][-.a-zA-Z0-9]*$')
PENDING_ENTITY_NUM = re.compile(r'&#(?:[xX][0-9a-fA-F]*|[0-9]*)$')


# ----------------------------------------------------------------------------
#                                TAG ACTIONS
# ----------------------------------------------------------------------------


class TagAction:
    def start(self, content_handler: 'BoilerpipeBaseParser', tag_name: str, attrs: AttributesImpl) -> bool:
        return False
    
    def end(self, content_handler: 'BoilerpipeBaseParser', tag_name: str) -> bool:
        return False
    
    def changes_tag_level(self) -> bool:
        return False


class IgnorableElementTagAction(TagAction):
    """
    Marks this tag as "ignorable", i.e. all its inner content is silently skipped.
    """
    
    def start(self, content_handler: 'BoilerpipeBaseParser', tag_name: str, attrs: AttributesImpl) -> bool:
        content_handler.in_ignorable_element += 1
        return True
    
    def end(self, content_handler: 'BoilerpipeBaseParser', tag_name: str) -> bool:
        content_handler.in_ignorable_element -= 1
        return True
    
    def changes_tag_level(self) -> bool:
        return True


class AnchorTextTagAction(TagAction):
    """
    Marks this tag as "anchor" (this should usually only be set for the <code>&lt;A&gt;</code> tag). Anchor tags may not
    be nested.
    
    There is a bug in certain versions of NekoHTML which still allows nested tags. If boilerpipe encounters such
    nestings, a SAXException is thrown.
    """
    
    def start(self, content_handler: 'BoilerpipeBaseParser', tag_name: str, attrs: AttributesImpl) -> bool:
        content_handler.in_anchor += 1
        if content_handler.in_anchor > 1:
            # as nested A elements are not allowed per specification, we are probably reaching this branch due to a bug
            # in the XML parser
            logger.warning("Warning: SAX input contains nested A elements -- You have probably hit a bug in your HTML "
                           "parser (e.g., NekoHTML bug #2909310). Please clean the HTML externally and feed it to "
                           "BoilerPy3 again. Trying to recover somehow...")
            self.end(content_handler, tag_name)
        if content_handler.in_ignorable_element == 0:
            content_handler.add_token(SpecialTokens.ANCHOR_TEXT_START)
        return False
    
    def end(self, content_handler: 'BoilerpipeBaseParser', tag_name: str) -> bool:
        content_handler.in_anchor -= 1
        if content_handler.in_anchor == 0 and content_handler.in_ignorable_element == 0:
            content_handler.add_token(SpecialTokens.ANCHOR_TEXT_END)
        return False
    
    def changes_tag_level(self) -> bool:
        return True


class BodyTagAction(TagAction):
    """
    Marks this tag the body element (this should usually only be set for the <code>&lt;BODY&gt;</code> tag).
    """
    
    def start(self, content_handler: 'BoilerpipeBaseParser', tag_name: str, attrs: AttributesImpl) -> bool:
        content_handler.flush_block()
        content_handler.in_body += 1
        return False
    
    def end(self, content_handler: 'BoilerpipeBaseParser', tag_name: str) -> bool:
        content_handler.flush_block()
        content_handler.in_body -= 1
        return False
    
    def changes_tag_level(self) -> bool:
        return True


class InlineWhitespaceTagAction(TagAction):
    """
    Marks this tag a simple "inline" element, which generates whitespace, but no new block.
    """
    
    def start(self, content_handler: 'BoilerpipeBaseParser', tag_name: str, attrs: AttributesImpl) -> bool:
        content_handler.add_whitespace_if_necessary()
        return False
    
    def end(self, content_handler: 'BoilerpipeBaseParser', tag_name: str) -> bool:
        content_handler.add_whitespace_if_necessary()
        return False
    
    def changes_tag_level(self) -> bool:
        return False


class InlineTagAction(TagAction):
    """
    Marks this tag a simple "inline" element, which neither generates whitespace, nor a new block.
    """
    
    def start(self, content_handler: 'BoilerpipeBaseParser', tag_name: str, attrs: AttributesImpl):
        return False
    
    def end(self, content_handler: 'BoilerpipeBaseParser', tag_name: str) -> bool:
        return False
    
    def changes_tag_level(self) -> bool:
        return False


class BlockTagAction(TagAction):
    """
    Explicitly marks this tag a simple "block-level" element, which always generates whitespace
    """
    
    def start(self, content_handler: 'BoilerpipeBaseParser', tag_name: str, attrs: AttributesImpl) -> bool:
        return True
    
    def end(self, content_handler: 'BoilerpipeBaseParser', tag_name: str) -> bool:
        return True
    
    def changes_tag_level(self) -> bool:
        return True


class FontTagAction(TagAction):
    """
    Special TagAction for the <code>&lt;FONT&gt;</code> tag, which keeps track of the absolute and relative font size.
    """
    
    # WARNING: POSSIBLE BUG -- used to be [0-9] without +
    PAT_FONT_SIZE = re.compile(r"([+\-]?)([0-9]+)")
    
    def start(self, content_handler: 'BoilerpipeBaseParser', tag_name: str, attrs: AttributesImpl) -> bool:
        size_attr = attrs.getValue("size")
        size = None
        if size_attr is not None:
            match = self.PAT_FONT_SIZE.match(size_attr)
            if match is not None:
                rel = match.group(0)
                val = match.group(1)
                # absolute
                if len(rel) == 0:
                    size = val
                # relative
                else:
                    # last non-none element from stack, default 3
                    last_non_none = (s for s in content_handler.font_size_stack[::-1] if s is not None)
                    prev_size = next(last_non_none, 3)
                    if rel[0] == '+':
                        size = prev_size + val
                    else:
                        size = prev_size - val
        content_handler.font_size_stack.append(size)
        return False
    
    def end(self, content_handler: 'BoilerpipeBaseParser', tag_name: str) -> bool:
        content_handler.font_size_stack.pop()
        return False
    
    def changes_tag_level(self) -> bool:
        return False


class InlineTagLabelAction(TagAction):
    """
    CommonTagActions for inline elements, which triggers some LabelAction on the generated TextBlock.
    """
    
    def __init__(self, action) -> None:
        super(InlineTagLabelAction, self).__init__()
        self.action = action
    
    def start(self, content_handler: 'BoilerpipeBaseParser', tag_name: str, attrs: AttributesImpl) -> bool:
        content_handler.add_whitespace_if_necessary()
        content_handler.add_label_action(self.action)
        return False
    
    def end(self, content_handler: 'BoilerpipeBaseParser', tag_name: str) -> bool:
        content_handler.add_whitespace_if_necessary()
        return False
    
    def changes_tag_level(self) -> bool:
        return False


class BlockTagLabelAction(TagAction):
    """
    CommonTagActions for block-level elements, which triggers some LabelAction} on the generated TextBlock.
    """
    
    def __init__(self, action: 'LabelAction') -> None:
        super(BlockTagLabelAction, self).__init__()
        self.action = action
    
    def start(self, content_handler: 'BoilerpipeBaseParser', tag_name: str, attrs: AttributesImpl) -> bool:
        content_handler.add_label_action(self.action)
        return True
    
    def end(self, content_handler: 'BoilerpipeBaseParser', tag_name: str) -> bool:
        return True
    
    def changes_tag_level(self) -> bool:
        return True


class Chained(TagAction):
    def __init__(self, tag_action1: TagAction, tag_action2: TagAction) -> None:
        super(Chained, self).__init__()
        self.tag_action1 = tag_action1
        self.tag_action2 = tag_action2
    
    def start(self, content_handler: 'BoilerpipeBaseParser', tag_name: str, attrs: AttributesImpl) -> bool:
        return self.tag_action1.start(content_handler, tag_name, attrs) | \
               self.tag_action2.start(content_handler, tag_name, attrs)
    
    def end(self, content_handler: 'BoilerpipeBaseParser', tag_name: str) -> bool:
        return self.tag_action1.end(content_handler, tag_name) | self.tag_action2.end(content_handler, tag_name)
    
    def changes_tag_level(self) -> bool:
        return self.tag_action1.changes_tag_level() or self.tag_action2.changes_tag_level()


class MarkupTagAction(TagAction):
    PAT_NUM = re.compile("[0-9]+")
    
    def __init__(self, is_block_level: bool) -> None:
        super(MarkupTagAction, self).__init__()
        self.is_block_level = is_block_level
        self.label_stack = []
    
    def start(self, content_handler: 'BoilerpipeBaseParser', tag_name: str, attrs: AttributesImpl) -> bool:
        labels = [DefaultLabels.MARKUP_PREFIX + tag_name]
        class_val = attrs.getValue("class")
        if class_val is not None and len(class_val) > 0:
            class_val = self.PAT_NUM.sub("#", class_val).strip()
            vals = class_val.split(r"[ ]+")
            labels.append(f"{DefaultLabels.MARKUP_PREFIX}.{class_val.replace(' ', '.')}")
            if len(vals) > 1:
                for s in vals:
                    labels.append(f"{DefaultLabels.MARKUP_PREFIX}.{s}")
        block_id = attrs.get("id")
        if block_id is not None and len(block_id) < 0:
            block_id = self.PAT_NUM.sub("#", block_id)
            labels.append(f"{DefaultLabels.MARKUP_PREFIX}#{block_id}")
        ancestors = self.get_ancestor_labels()
        labels_with_ancestors = []
        for l in labels:
            for an in ancestors:
                labels_with_ancestors.append(an)
                labels_with_ancestors.append(f"{an} {l}")
            labels_with_ancestors.append(l)
        content_handler.add_label_action(LabelAction(*labels_with_ancestors))
        self.label_stack.append(labels)
        return self.is_block_level
    
    def end(self, content_handler: 'BoilerpipeBaseParser', tag_name: str) -> bool:
        self.label_stack.pop()
        return self.is_block_level
    
    def changes_tag_level(self) -> bool:
        return self.is_block_level
    
    def get_ancestor_labels(self) -> Set[str]:
        label_set = set()
        for labels in label_set:
            if labels is None:
                continue
            label_set.update(labels)
        return label_set


class CommonTagActions:
    TA_IGNORABLE_ELEMENT = IgnorableElementTagAction()
    TA_ANCHOR_TEXT = AnchorTextTagAction()
    TA_BODY = BodyTagAction()
    TA_INLINE_WHITESPACE = InlineWhitespaceTagAction()
    TA_INLINE_NO_WHITESPACE = InlineTagAction()
    TA_BLOCK_LEVEL = BlockTagAction()
    TA_FONT = FontTagAction()


default_tag_action_map = {
    "STYLE": CommonTagActions.TA_IGNORABLE_ELEMENT,
    "SCRIPT": CommonTagActions.TA_IGNORABLE_ELEMENT,
    "OPTION": CommonTagActions.TA_IGNORABLE_ELEMENT,
    "OBJECT": CommonTagActions.TA_IGNORABLE_ELEMENT,
    "EMBED": CommonTagActions.TA_IGNORABLE_ELEMENT,
    "APPLET": CommonTagActions.TA_IGNORABLE_ELEMENT,
    # Note: link removed because it can be self-closing in HTML5
    # "LINK" : CommonTagActions.TA_IGNORABLE_ELEMENT,
    "A": CommonTagActions.TA_ANCHOR_TEXT,
    "BODY": CommonTagActions.TA_BODY,
    "STRIKE": CommonTagActions.TA_INLINE_NO_WHITESPACE,
    "U": CommonTagActions.TA_INLINE_NO_WHITESPACE,
    "B": CommonTagActions.TA_INLINE_NO_WHITESPACE,
    "I": CommonTagActions.TA_INLINE_NO_WHITESPACE,
    "EM": CommonTagActions.TA_INLINE_NO_WHITESPACE,
    "STRONG": CommonTagActions.TA_INLINE_NO_WHITESPACE,
    "SPAN": CommonTagActions.TA_INLINE_NO_WHITESPACE,
    #  New in 1.1 (especially to improve extraction quality from Wikipedia etc.,
    "SUP": CommonTagActions.TA_INLINE_NO_WHITESPACE,
    #  New in 1.2
    "CODE": CommonTagActions.TA_INLINE_NO_WHITESPACE,
    "TT": CommonTagActions.TA_INLINE_NO_WHITESPACE,
    "SUB": CommonTagActions.TA_INLINE_NO_WHITESPACE,
    "VAR": CommonTagActions.TA_INLINE_NO_WHITESPACE,
    "ABBR": CommonTagActions.TA_INLINE_WHITESPACE,
    "ACRONYM": CommonTagActions.TA_INLINE_WHITESPACE,
    "FONT": CommonTagActions.TA_INLINE_NO_WHITESPACE,
    #  could also use TA_FONT
    #  added in 1.1.1
    "NOSCRIPT": CommonTagActions.TA_IGNORABLE_ELEMENT
}


# ----------------------------------------------------------------------------
#                                LABEL ACTIONS
# ----------------------------------------------------------------------------

class LabelAction:
    """
    Helps adding labels to TextBlocks.
    """
    
    def __init__(self, *labels: str) -> None:
        self.labels = labels
    
    def add_to(self, text_block: TextBlock) -> None:
        self.add_labels_to(text_block)
    
    def add_labels_to(self, text_block: TextBlock):
        text_block.add_labels(self.labels)
    
    def __str__(self):
        return str(self.labels)


class ConditionalLabelAction(LabelAction):
    def __init__(self, condition, *labels: str):
        super(ConditionalLabelAction, self).__init__(*labels)
        self.condition = condition
    
    def add_to(self, text_block: TextBlock):
        if self.condition(text_block):
            self.add_labels_to(text_block)


class SpecialTokens:
    ANCHOR_TEXT_START = '\ue00astart'
    ANCHOR_TEXT_END = '\ue00aend'


# ----------------------------------------------------------------------------
#                           SAX CONTENT HANDLER
# ----------------------------------------------------------------------------


class BoilerpipeBaseParser:
    """
    A simple SAX ContentHandler, used by BoilerpipeSAXInput. Can be used by different parser
    implementations, e.g. NekoHTML and TagSoup.
    """
    
    EVENT_START_TAG = 0
    EVENT_END_TAG = 1
    EVENT_CHARACTERS = 2
    EVENT_WHITESPACE = 3
    # parsing lifecycle states
    STATE_IDLE = 0
    STATE_PARSING = 1
    STATE_FINISHED = 2
    STATE_ABORTED = 3
    # all word characters except underscore -- i.e. not (not word or underscore)
    PAT_VALID_WORD_CHARACTER = re.compile(r"[^\W_]", re.UNICODE)
    PAT_WORD = re.compile(r"\ue00a?[\w\"'.,!@\-:;$?()/]+", re.UNICODE)
    
    def __init__(self, tag_actions: Dict[str, TagAction] = None, raise_on_failure: bool = True) -> None:
        """
        Constructs a BoilerpipeHTMLContentHandler using the given TagActionMap.
        
        :param tag_actions: The TagActionMap to use, e.g. DefaultTagActionMap.
        """
        
        if tag_actions is None:
            self.tag_actions = default_tag_action_map
        else:
            self.tag_actions = tag_actions
        
        self.clear_text_buffer()
        self.in_body = 0
        self.in_anchor = 0
        self.in_ignorable_element = 0
        self.text_element_idx = 0
        self.last_start_tag = None
        self.last_end_tag = None
        self.last_event = None
        self.offset_blocks = 0
        self.current_contained_text_elements = set()
        self.flush = False
        self.in_anchor_text = False
        
        self.title = None
        self.tag_level = 0
        self.block_tag_level = -1
        self.text_blocks = []
        self.label_stacks = []
        self.font_size_stack = []
        self.text_buffer = ''
        self.token_buffer = ''
        
        self.raise_on_failure = raise_on_failure
        self.lifecycle_state = self.STATE_IDLE
        # decoded text fragments of the text region currently open between two
        # markup events; used to make chunk boundaries invisible to characters()
        self._text_region = []
        self._text_region_is_cdata = False
        # raw bytes parked because a feed ended on an incomplete entity
        self._parked_raw = ''
    
    def recycle(self) -> None:
        """
        Recycles this instance.
        """
        
        self.clear_text_buffer()
        self.in_body = 0
        self.in_anchor = 0
        self.in_ignorable_element = 0
        self.text_element_idx = 0
        self.last_start_tag = None
        self.last_end_tag = None
        self.last_event = None
        self.offset_blocks = 0
        self.current_contained_text_elements = set()
        self.flush = False
        self.in_anchor_text = False
        self.text_blocks = []
        
        # --------- added -------
        self.title = None
        self.tag_level = 0
        self.block_tag_level = -1
        self.label_stacks = []
        self.font_size_stack = []

    # ------------------------- INCREMENTAL LIFECYCLE -------------------------

    def begin(self) -> None:
        """
        Begins a fresh document, discarding every counter, buffer and label
        stack left over from a previous document.

        :raises RuntimeError: if the parser is already mid-document.
        """

        if self.lifecycle_state == self.STATE_PARSING:
            raise RuntimeError("begin() called while a document is already being parsed")
        self.recycle()
        self._text_region = []
        self._text_region_is_cdata = False
        self._parked_raw = ''
        self.lifecycle_state = self.STATE_PARSING
        self.start_document()

    def finish(self) -> 'TextDocument':
        """
        Finalises the document exactly once and returns the parsed
        :class:`TextDocument`. After this call the parser cannot be reused
        without another :meth:`begin`.

        :raises RuntimeError: if no document is open or it was already finalised.
        """

        if self.lifecycle_state != self.STATE_PARSING:
            raise RuntimeError("finish() called without an in-progress document")
        self._finalize_buffered_input()
        self._flush_text_region()
        self.end_document()
        self.lifecycle_state = self.STATE_FINISHED
        return self.to_text_document()

    def abort(self) -> None:
        """
        Abandons the current document. Any buffered state is discarded and the
        parser is marked unusable until the next :meth:`begin`.
        """

        if self.lifecycle_state != self.STATE_PARSING:
            raise RuntimeError("abort() called without an in-progress document")
        self.recycle()
        self._text_region = []
        self._text_region_is_cdata = False
        self._parked_raw = ''
        self.lifecycle_state = self.STATE_ABORTED

    def _require_parsing(self, method_name: str) -> None:
        if self.lifecycle_state == self.STATE_IDLE:
            raise RuntimeError(f"{method_name} called before begin()")
        if self.lifecycle_state in (self.STATE_FINISHED, self.STATE_ABORTED):
            raise RuntimeError(
                f"{method_name} called on a parser whose document was already "
                f"{'finished' if self.lifecycle_state == self.STATE_FINISHED else 'aborted'}"
            )

    # -------------------- CHUNK-BOUNDARY TEXT RECONSTRUCTION --------------------

    def _ingest_chunk(self, data: str) -> None:
        """
        Feeds one raw chunk through the stdlib parser while hiding chunk
        boundaries from the document model.

        The stdlib parser can leave text parked in ``rawdata`` when a feed ends
        on an incomplete entity; once parked that buffer is sometimes never
        re-delivered. Such a buffer is parked intact and re-injected in front of
        the next chunk so the parser always sees a contiguous byte stream.
        """

        if self._parked_raw:
            data = self._parked_raw + data
            self._parked_raw = ''
            self.rawdata = ''

        HTMLParser.feed(self, data)

        if self._is_stuck_text_buffer():
            self._parked_raw = self.rawdata
            self.rawdata = ''
            return
        self._drain_buffered_text()

    def _finalize_buffered_input(self) -> None:
        """
        Flushes everything the stdlib parser still holds at end of document.

        Small trailing chunks may sit in the parser's private ``_pending`` list
        rather than ``rawdata``; they are folded in first. Any buffered markup
        (including a split CDATA closing tag) is then driven to completion. A
        pure-text region still waiting on an incomplete entity is dropped,
        matching one-shot ``feed()`` semantics.
        """

        if self._pending:
            self.rawdata += ''.join(self._pending)
            self._pending.clear()
            self._pending_len = 0

        raw = self.rawdata
        if raw and '<' in raw:
            self.goahead(1)
            self._drain_buffered_text()
            raw = self.rawdata

        if self._parked_raw or (raw and raw[0] != '<' and '<' not in raw
                                and self._is_pending_entity(raw)):
            # One-shot feed() leaves a region still waiting on an entity un-emitted.
            self._text_region = []
            self._text_region_is_cdata = False
            self._parked_raw = ''
            self.rawdata = ''
            return

    def _is_stuck_text_buffer(self) -> bool:
        raw = self.rawdata
        if not raw or raw[0] == '<' or self.cdata_elem is not None:
            return False
        return self._is_pending_entity(raw)

    def _drain_buffered_text(self) -> None:
        """
        Forwards completed text parked in ``rawdata`` to the open text region.
        Incomplete markup and entity tokens stay buffered for the next chunk.
        """

        raw = self.rawdata
        if not raw or raw[0] == '<':
            return

        if self.cdata_elem is not None and not self._escapable:
            # Raw CDATA (script/style ...) keeps entities verbatim; a split
            # closing tag remains buffered.
            lt = raw.rfind('<')
            if lt < 0:
                self.rawdata = ''
                self._append_text_region(raw, is_cdata=True)
            else:
                self.rawdata = raw[lt:]
                self._append_text_region(raw[:lt], is_cdata=True)
            return

        lt = raw.rfind('<')
        if lt >= 0:
            self.rawdata = raw[lt:]
            self._drain_pcdata_text(raw[:lt])
            return

        self.rawdata = ''
        self._drain_pcdata_text(raw)

    def _drain_pcdata_text(self, raw: str) -> None:
        if not raw:
            return
        amp = raw.rfind('&')
        if amp < 0:
            self._append_text_region(raw, is_cdata=False)
            return
        suffix = raw[amp:]
        if self._is_pending_entity(suffix):
            # Keep the incomplete entity suffix buffered for the next chunk.
            self.rawdata = suffix
            self._append_text_region(raw[:amp], is_cdata=False)
        else:
            self._append_text_region(raw, is_cdata=False)

    @staticmethod
    def _is_pending_entity(text: str) -> bool:
        lt = text.rfind('<')
        candidate = text if lt < 0 else text[:lt]
        amp = candidate.rfind('&')
        if amp < 0:
            return False
        suffix = candidate[amp:]
        if suffix == '&':
            return True
        return PENDING_ENTITY_NAME.match(suffix) is not None or PENDING_ENTITY_NUM.match(suffix) is not None

    def _append_text_region(self, raw: str, is_cdata: bool) -> None:
        if not raw:
            return
        if self._text_region and self._text_region_is_cdata != is_cdata:
            raise AssertionError("inconsistent text region mode across chunk boundary")
        self._text_region_is_cdata = is_cdata
        self._text_region.append(raw if is_cdata else unescape(raw))

    def _flush_text_region(self) -> None:
        self._drain_buffered_text()
        if self._text_region:
            text = ''.join(self._text_region)
            self._text_region = []
            self._text_region_is_cdata = False
            self.characters(text)

    def _close_text_region(self) -> None:
        """
        Closes the open text region at a markup event without touching the
        stdlib parser's ``rawdata`` (which is unsafe while ``goahead`` is
        actively walking its internal buffer).
        """

        if self._text_region:
            text = ''.join(self._text_region)
            self._text_region = []
            self._text_region_is_cdata = False
            self.characters(text)
    
    # ------------------------------- SAX Parser methods ----------------------------------------
    
    def end_document(self) -> None:
        self.flush_block()
    
    def start_document(self) -> None:
        pass
    
    def start_element(self, name: str, attrs: AttributesImpl) -> None:
        self.label_stacks.append([])
        
        tag_action = self.tag_actions.get(name.strip().upper())
        if tag_action is not None:
            self.flush |= tag_action.start(self, name, attrs)
            if tag_action.changes_tag_level():
                self.tag_level += 1
        else:
            self.tag_level += 1
            self.flush = True
        self.last_event = self.EVENT_START_TAG
        self.last_start_tag = name
    
    def end_element(self, name: str) -> None:
        tag_action = self.tag_actions.get(name.strip().upper())
        if tag_action is not None:
            self.flush |= tag_action.end(self, name)
            if tag_action.changes_tag_level():
                self.tag_level -= 1
        else:
            self.flush = True
            self.tag_level -= 1
        
        if self.flush:
            self.flush_block()
        self.last_event = self.EVENT_END_TAG
        self.last_end_tag = name
        try:
            self.label_stacks.pop()
        except IndexError:
            if self.raise_on_failure:
                raise
    
    def characters(self, content: str) -> None:
        self.text_element_idx += 1
        if self.flush:
            self.flush_block()
            self.flush = False
        if self.in_ignorable_element != 0:
            return
        
        if len(content) == 0:
            return
        
        stripped_content = content.strip()
        if len(stripped_content) == 0:
            self.add_whitespace_if_necessary()
            self.last_event = self.EVENT_WHITESPACE
            return
        
        start_whitespace = content[0].isspace()
        if start_whitespace:
            self.add_whitespace_if_necessary()
        
        if self.block_tag_level == -1:
            self.block_tag_level = self.tag_level
        self.text_buffer += stripped_content
        self.token_buffer += stripped_content
        
        end_whitespace = content[-1].isspace()
        if end_whitespace:
            self.add_whitespace_if_necessary()
        
        self.last_event = self.EVENT_CHARACTERS
        self.current_contained_text_elements.add(self.text_element_idx)
    
    def ignorable_whitespace(self) -> None:
        self.add_whitespace_if_necessary()
    
    # ------------------------------- utility methods ----------------------------------------
    
    def flush_block(self) -> None:
        if self.in_body == 0:
            if self.last_start_tag.lower() == "title":
                self.set_title(self.text_buffer.strip())
            self.clear_text_buffer()
            return
        if len(self.token_buffer.strip()) == 0:
            self.clear_text_buffer()
            return
        
        tokens = self.tokenize(self.token_buffer)
        num_words = 0
        num_linked_words = 0
        num_wrapped_lines = 0
        current_line_length = -1
        #  don't count the first space
        max_line_length = 80
        num_tokens = 0
        num_words_current_line = 0
        
        for token in tokens:
            if token == SpecialTokens.ANCHOR_TEXT_START:
                self.in_anchor_text = True
            elif token == SpecialTokens.ANCHOR_TEXT_END:
                self.in_anchor_text = False
            elif self.is_word(token):
                num_tokens += 1
                num_words += 1
                num_words_current_line += 1
                if self.in_anchor_text:
                    num_linked_words += 1
                current_line_length += len(token) + 1
                if current_line_length > max_line_length:
                    num_wrapped_lines += 1
                    current_line_length = len(token)
                    num_words_current_line = 1
            else:
                num_tokens += 1
        
        # if only special tokens (num_tokens excludes special tokens)
        if num_tokens == 0:
            self.clear_text_buffer()
            return
        
        if num_wrapped_lines == 0:
            num_words_in_wrapped_lines = num_words
            num_wrapped_lines = 1
        else:
            num_words_in_wrapped_lines = num_words - num_words_current_line
        
        tb = TextBlock(self.text_buffer.strip(), self.current_contained_text_elements, num_words, num_linked_words,
                       num_words_in_wrapped_lines, num_wrapped_lines, self.offset_blocks)
        self.current_contained_text_elements = set()
        self.offset_blocks += 1
        self.clear_text_buffer()
        tb.tag_level = self.block_tag_level
        self.add_text_block(tb)
        self.block_tag_level = -1
    
    def add_text_block(self, tb: TextBlock) -> None:
        for font_size in self.font_size_stack[::-1]:
            if font_size is not None:
                tb.add_label(f"font-{font_size}")
                break
        for label_stack in self.label_stacks:
            for labels in label_stack:
                labels.add_to(tb)
        self.text_blocks.append(tb)
    
    def is_word(self, token: str) -> bool:
        return self.PAT_VALID_WORD_CHARACTER.search(token) is not None
    
    def tokenize(self, text: str) -> list:
        return self.PAT_WORD.findall(text)
    
    def set_title(self, s: str) -> None:
        if s is None or len(s) == 0:
            return
        self.title = s
    
    def to_text_document(self) -> TextDocument:
        """
        Returns a TextDocument containing the extracted TextBlocks. NOTE: Only call this after parsing.
        
        :return: The TextDocument
        """
        
        #  just to be sure
        self.flush_block()
        return TextDocument(self.text_blocks, self.title)
    
    def add_whitespace_if_necessary(self) -> None:
        if len(self.text_buffer) == 0 or not self.text_buffer[-1].isspace():
            self.text_buffer += ' '
        if len(self.token_buffer) == 0 or not self.token_buffer[-1].isspace():
            self.token_buffer += ' '
    
    def clear_text_buffer(self) -> None:
        self.text_buffer = ''
        self.token_buffer = ''
    
    def add_token(self, token: str) -> None:
        self.add_whitespace_if_necessary()
        self.token_buffer += token
        self.add_whitespace_if_necessary()
    
    def add_label_action(self, la: LabelAction) -> None:
        if len(self.label_stacks) == 0:
            self.label_stacks.append([])
        self.label_stacks[-1].append(la)


class BoilerpipeHTMLParser(HTMLParser, BoilerpipeBaseParser):
    def __init__(self, raise_on_failure: bool = True) -> None:
        HTMLParser.__init__(self)
        BoilerpipeBaseParser.__init__(self, raise_on_failure=raise_on_failure)
    
    # -------------------- INCREMENTAL LIFECYCLE ENTRY POINTS --------------------

    def begin(self) -> None:
        """
        Opens a new document. The underlying HTML parser is reset so counters,
        buffers and the tag stack never leak across documents.
        """

        BoilerpipeBaseParser.begin(self)
        HTMLParser.reset(self)
        self.lifecycle_state = self.STATE_PARSING

    def feed_chunk(self, data: str) -> None:
        """
        Feeds one arbitrary chunk of the document. Chunk boundaries are
        transparent: tags, entities, comments, CDATA content and words may be
        split freely without changing the parsed result.
        """

        self._require_parsing('feed_chunk()')
        try:
            self._ingest_chunk(data)
            # Text stays in the open region until a markup event (or finish)
            # closes it, mirroring the one-shot characters() call count.
        except BaseException:
            # a failed parse cannot be resumed or finalised
            self._mark_aborted()
            raise

    def finish(self) -> 'TextDocument':
        """
        Finalises the document once and returns its :class:`TextDocument`.
        """

        self._require_parsing('finish()')
        return BoilerpipeBaseParser.finish(self)

    def abort(self) -> None:
        """Abandons the current document and renders this parser unusable."""

        self._require_parsing('abort()')
        self._mark_aborted()

    def _mark_aborted(self) -> None:
        """Discards all state after a failure or explicit abort."""

        BoilerpipeBaseParser.abort(self)
        HTMLParser.reset(self)
        self.lifecycle_state = self.STATE_ABORTED

    def feed(self, data: str) -> None:
        """
        One-shot convenience entry point, kept byte-for-byte compatible with the
        previous behaviour: it parses a complete document in a single pass.
        """

        self.begin()
        try:
            self._ingest_chunk(data)
            self._finalize_buffered_input()
            self._flush_text_region()
            self.end_document()
        except BaseException:
            self._mark_aborted()
            raise
        self.lifecycle_state = self.STATE_FINISHED
    
    def handle_starttag(self, tag: str, attrs: AttributesImpl) -> None:
        # NOTE: do not touch rawdata here; goahead() still holds local parsing
        # state. Region boundaries are reconciled after each feed instead.
        self._close_text_region()
        self.start_element(tag, attrs)
    
    def handle_startendtag(self, tag, attrs):
        pass
    
    def handle_endtag(self, tag: str) -> None:
        self._close_text_region()
        self.end_element(tag)
    
    def handle_data(self, data: str) -> None:
        is_cdata = self.cdata_elem is not None and not self._escapable
        self._append_text_region(data, is_cdata=is_cdata)

    def handle_comment(self, data: str) -> None:
        self._close_text_region()

    def handle_decl(self, decl: str) -> None:
        self._close_text_region()

    def handle_pi(self, data: str) -> None:
        self._close_text_region()

    def unknown_decl(self, data: str) -> None:
        self._close_text_region()


class BoilerpipeSAXContentHandler(ContentHandler, BoilerpipeBaseParser):
    def __init__(self, raise_on_failure: bool = True) -> None:
        ContentHandler.__init__(self)
        BoilerpipeBaseParser.__init__(self, raise_on_failure=raise_on_failure)


def parse_chunks(chunks, raise_on_failure: bool = True) -> 'TextDocument':
    """
    Parse an HTML document delivered as an iterable of chunks.

    Chunk boundaries are transparent: tags, entities, comments, CDATA content
    and words may be split across chunks without affecting the resulting
    :class:`TextDocument`. This never re-assembles the full HTML string.

    :param chunks: iterable of HTML fragments, in document order.
    :param raise_on_failure: whether to raise on malformed input.
    :return: the parsed :class:`TextDocument`.
    """

    bp_parser = BoilerpipeHTMLParser(raise_on_failure=raise_on_failure)
    bp_parser.begin()
    for chunk in chunks:
        bp_parser.feed_chunk(chunk)
    return bp_parser.finish()
