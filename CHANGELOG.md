# Changelog


## Unreleased
- Adds a code-aware content mode for `<pre>` blocks: whitespace (spaces, tabs, newlines) is preserved verbatim,
  CRLF/CR line endings are normalized to LF, and HTML entities are decoded exactly once
- `<pre>` blocks become their own block type (`TextBlock.is_code`) with neutral word/density statistics, are never
  fused with or split like ordinary paragraphs, and are still classified as content based on the surrounding article
  context (e.g. a `<pre>` in the page header/navigation is not kept unconditionally)


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
