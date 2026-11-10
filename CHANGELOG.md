# Changelog


## Unreleased
- Adds an incremental parsing lifecycle: `BoilerpipeHTMLParser.begin()`,
  `feed_chunk()` and `finish()`, plus `parser.parse_chunks()` and
  `Extractor.get_doc_from_chunks()`, for parsing HTML delivered in chunks without
  re-joining it. Chunk boundaries are transparent, so text, word counts, link
  density, labels and the content judgement are identical to the one-shot parse.
- Adds `abort()` to discard an in-progress document; finished and aborted parsers
  cannot be reused until `begin()` resets all counters, buffers and label stacks.
- The one-shot `feed()`/`get_doc()` results are unchanged.

## 1.0.6
- Adds `request_kwargs` argument to `Extractor`
- Adds note about URL content extraction to README


## 1.0.5
- Adds more type hints
- Converts more camel case variables to snake case
- Specifies Python 3.10 compatibility, adds version to package
- Fixes marked HTML extraction
- Adds new methods and documentation for marked HTML extraction
- Restores `TextBlock.set_is_content()` method


## 1.0.4 (February 3 2021)

- Added 'raise_on_failure' parameter (default `True`) to extractors to raise exceptions when HTML extraction errors are encountered (they will be handled otherwise).
- Updated unit tests
- Fixed some camel-cased variable names
- Made some minor optimizations


## 1.0.3 (November 21 2020)

- Added CI
- Updated test requirements
- Added Flake8 config


## 1.0.2 (December 12 2019)

- Fixed containedTextElements variable (#1)


## 1.0.0 (August 9 2019)

- Initial release.
