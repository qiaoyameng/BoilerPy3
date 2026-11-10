"""
This file is licensed under the terms of the Apache License, Version 2.0. See the LICENSE file in the root of this
repository for complete details.

Document-level word counting strategies.

A counting strategy decides, for a given text, what counts as a "word" and how wide a token is when simulating
line wrapping. The strategy is applied consistently when TextBlocks are created (parser), split
(SplitParagraphBlocksFilter) and merged (TextBlock.merge_next sums the per-block counts produced by the same
strategy), so densities are always derived from one consistent set of counts.
"""

import re
import unicodedata
from typing import List, NamedTuple

# Special tokens used by the parser to mark anchor text boundaries. Kept here so that both the parser and the
# counting helpers share a single definition.
ANCHOR_TEXT_START = '\ue00astart'
ANCHOR_TEXT_END = '\ue00aend'

MAX_LINE_LENGTH = 80


class BlockStats(NamedTuple):
    """
    Word/line statistics of a single text block.
    """
    num_words: int
    num_linked_words: int
    num_words_in_wrapped_lines: int
    num_wrapped_lines: int
    num_tokens: int


class WordCountingStrategy:
    """
    Interface for word counting strategies.
    """

    name = 'abstract'

    def tokenize(self, text: str) -> List[str]:
        """
        Splits text into tokens. Whitespace is dropped; tokens may be words or non-word tokens (e.g. punctuation).
        """
        raise NotImplementedError

    def is_word(self, token: str) -> bool:
        """
        Checks whether a token produced by tokenize() counts as a word.
        """
        raise NotImplementedError

    def token_width(self, token: str) -> int:
        """
        Returns the display width of a token, used to simulate line wrapping.
        """
        raise NotImplementedError

    def count(self, text: str) -> BlockStats:
        """
        Counts words and wrapped lines for a complete text.
        """
        return compute_block_counts(self.tokenize(text), self)


class DefaultWordCountingStrategy(WordCountingStrategy):
    """
    The legacy BoilerPy3 counting rules (English-oriented): tokens are runs of word characters and selected
    punctuation, a token counts as a word if it contains at least one word character, and token width is the
    number of Unicode code points. This reproduces the historical behavior exactly and remains the default.
    """

    name = 'default'

    # all word characters except underscore -- i.e. not (not word or underscore)
    PAT_VALID_WORD_CHARACTER = re.compile(r"[^\W_]", re.UNICODE)
    PAT_WORD = re.compile(r"\ue00a?[\w\"'.,!@\-:;$?()/]+", re.UNICODE)

    def tokenize(self, text: str) -> List[str]:
        return self.PAT_WORD.findall(text)

    def is_word(self, token: str) -> bool:
        return self.PAT_VALID_WORD_CHARACTER.search(token) is not None

    def token_width(self, token: str) -> int:
        return len(token)


# Han ideograph ranges (BMP + supplementary planes), plus the ideographic number zero and iteration mark.
_HAN_RANGES = (
    (0x3006, 0x3007),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF),
    (0x2A700, 0x2B73F),
    (0x2B740, 0x2B81F),
    (0x2B820, 0x2CEAF),
    (0x30000, 0x3134A),
)

# Apostrophes that may appear inside a word (e.g. "it's", "don't").
_APOSTROPHES = ("'", '’', 'ʼ')

_COMBINING_CATEGORIES = ('Mn', 'Mc', 'Me')


def _is_han(char: str) -> bool:
    code_point = ord(char)
    return any(start <= code_point <= end for start, end in _HAN_RANGES)


def _is_combining(char: str) -> bool:
    return unicodedata.category(char) in _COMBINING_CATEGORIES


def _is_latin_word_char(char: str) -> bool:
    return char.isalnum() and not _is_han(char)


class MixedCjkWordCountingStrategy(WordCountingStrategy):
    """
    Counting rules for mixed Chinese/English text, without any third-party tokenizer:

    - each Han ideograph counts as one word
    - a run of Latin letters/digits counts as one word; apostrophes between two word characters belong to the
      word (e.g. "it's" is one word)
    - combining marks (Unicode categories Mn/Mc/Me) attach to the preceding character
    - punctuation and other symbols do not count as words
    - the same rules apply inside and outside anchor text

    Token width convention (used for wrapped-line simulation, uniformly -- never bytes in one place and
    characters in another): combining marks have width 0, characters with East Asian Width W or F have width 2,
    every other code point has width 1.
    """

    name = 'mixed-cjk'

    def tokenize(self, text: str) -> List[str]:
        tokens = []
        idx, length = 0, len(text)
        while idx < length:
            char = text[idx]
            if char.isspace():
                idx += 1
                continue
            if char == '\ue00a':
                # special token (anchor text boundary): consume the marker plus its ASCII name
                end = idx + 1
                while end < length and text[end].isascii() and text[end].isalpha():
                    end += 1
                tokens.append(text[idx:end])
                idx = end
                continue
            if _is_han(char):
                end = idx + 1
                while end < length and _is_combining(text[end]):
                    end += 1
                tokens.append(text[idx:end])
                idx = end
                continue
            if _is_latin_word_char(char):
                end = idx + 1
                while end < length:
                    next_char = text[end]
                    if _is_latin_word_char(next_char) or _is_combining(next_char):
                        end += 1
                    elif next_char in _APOSTROPHES and end + 1 < length and _is_latin_word_char(text[end + 1]):
                        end += 1
                    else:
                        break
                tokens.append(text[idx:end])
                idx = end
                continue
            # punctuation/symbol run (never counts as a word); combining marks follow the preceding character
            end = idx + 1
            while end < length:
                next_char = text[end]
                if next_char.isspace() or next_char == '\ue00a' or _is_han(next_char) \
                        or _is_latin_word_char(next_char):
                    break
                end += 1
            tokens.append(text[idx:end])
            idx = end
        return tokens

    def is_word(self, token: str) -> bool:
        return any(_is_han(char) or char.isalnum() for char in token)

    def token_width(self, token: str) -> int:
        width = 0
        for char in token:
            if _is_combining(char):
                continue
            width += 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        return width


DEFAULT_STRATEGY = DefaultWordCountingStrategy()
MIXED_CJK_STRATEGY = MixedCjkWordCountingStrategy()


def compute_block_counts(tokens: List[str], strategy: WordCountingStrategy,
                         max_line_length: int = MAX_LINE_LENGTH, in_anchor_text: bool = False) -> BlockStats:
    """
    Computes word and wrapped-line statistics for a token sequence, using the given strategy for word detection
    and token widths. Anchor text boundary tokens (ANCHOR_TEXT_START/END) toggle linked-word counting and are
    not counted themselves.

    :param in_anchor_text: Whether the token sequence starts inside anchor text (anchors may span block
                           boundaries; the parser carries this state across blocks).
    """
    num_words = 0
    num_linked_words = 0
    num_wrapped_lines = 0
    current_line_length = -1
    #  don't count the first space
    num_tokens = 0
    num_words_current_line = 0
    for token in tokens:
        if token == ANCHOR_TEXT_START:
            in_anchor_text = True
        elif token == ANCHOR_TEXT_END:
            in_anchor_text = False
        elif strategy.is_word(token):
            num_tokens += 1
            num_words += 1
            num_words_current_line += 1
            if in_anchor_text:
                num_linked_words += 1
            current_line_length += strategy.token_width(token) + 1
            if current_line_length > max_line_length:
                num_wrapped_lines += 1
                current_line_length = strategy.token_width(token)
                num_words_current_line = 1
        else:
            num_tokens += 1

    # if only special tokens (num_tokens excludes special tokens)
    if num_tokens == 0:
        return BlockStats(0, 0, 0, 0, 0)

    if num_wrapped_lines == 0:
        return BlockStats(num_words, num_linked_words, num_words, 1, num_tokens)
    return BlockStats(num_words, num_linked_words, num_words - num_words_current_line, num_wrapped_lines,
                      num_tokens)
