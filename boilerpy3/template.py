"""
Trainable repeated-block (template/boilerplate) filter.

Single-page density heuristics cannot distinguish site-wide chrome (navigation, copyright notices, recommendation
rails) from article text because both look identical within one document. This module instead learns which blocks
recur across a small batch of same-site pages:

* :class:`NormalizationConfig` defines the exact text normalization and structural-context features used to build a
  block signature. The serialized configuration is part of the model, so a model is rejected if the runtime
  normalization settings differ.
* :class:`TemplateModelTrainer` accumulates per-signature document support. The caller supplies a stable page-id;
  each document contributes at most once per signature and re-training the same page-id is idempotent. Replacing or
  removing a sample revokes its previous contributions.
* :class:`TemplateModel` is the versioned, JSON-serializable training result. It stores signature hashes and
  decision evidence only -- never HTML or block text.
* :class:`TemplateBoilerplateFilter` applies a trained model inside an existing
  :class:`boilerpy3.filters.FilterChain`. Application never mutates training statistics.
"""

import hashlib
import json
import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Set, Tuple

from boilerpy3.document import DefaultLabels, TextBlock, TextDocument
from boilerpy3.exceptions import TemplateModelError
from boilerpy3.filters import BoilerpipeFilter

MODEL_FORMAT = "boilerpy3-template-model"
MODEL_VERSION = 1

TEMPLATE_LABEL = "de.l3s.boilerpipe/TEMPLATE_BOILERPLATE"

_PAT_WHITESPACE = re.compile(r"\s+")
_PAT_DIGITS = re.compile(r"\d+")
_PAT_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_block_text(text: str, lowercase: bool, collapse_digits: bool, strip_punctuation: bool) -> str:
    """
    Applies the configured, explicit normalization steps to a block's text.

    Order is fixed so that the same block always yields the same normalized form: unicode NFKC folding, optional
    lower-casing, optional digit collapsing (each run of digits becomes ``#``), optional punctuation removal, then
    whitespace collapsing and trimming.
    """

    normalized = unicodedata.normalize("NFKC", text)
    if lowercase:
        normalized = normalized.lower()
    if collapse_digits:
        normalized = _PAT_DIGITS.sub("#", normalized)
    if strip_punctuation:
        normalized = _PAT_PUNCTUATION.sub(" ", normalized)
    return _PAT_WHITESPACE.sub(" ", normalized).strip()


class NormalizationConfig:
    """
    Exact configuration used to turn a :class:`TextBlock` into a stable signature.

    Text normalization flags:
        lowercase: fold all characters to lower case.
        collapse_digits: replace every run of digits with ``#`` (copyright years etc. then compare equal).
        strip_punctuation: remove unicode word/punctuation symbols.

    Structural context flags:
        include_tag_level: add the block's HTML tag level to the signature.
        include_markup_labels: add the block's markup labels (``<tag`` / ``<.class`` style labels) to the signature.

    The full configuration is embedded in every saved model. Loading a model with a mismatched configuration is
    rejected because previously computed signatures would no longer be reproducible.
    """

    _FIELDS = ("lowercase", "collapse_digits", "strip_punctuation", "include_tag_level", "include_markup_labels")

    def __init__(self, lowercase: bool = True, collapse_digits: bool = True, strip_punctuation: bool = False,
                 include_tag_level: bool = True, include_markup_labels: bool = True) -> None:
        self.lowercase = bool(lowercase)
        self.collapse_digits = bool(collapse_digits)
        self.strip_punctuation = bool(strip_punctuation)
        self.include_tag_level = bool(include_tag_level)
        self.include_markup_labels = bool(include_markup_labels)

    def to_dict(self) -> Dict[str, bool]:
        return {field: getattr(self, field) for field in self._FIELDS}

    @classmethod
    def from_dict(cls, data: Dict[str, bool]) -> 'NormalizationConfig':
        if not isinstance(data, dict) or set(data.keys()) != set(cls._FIELDS):
            raise TemplateModelError("invalid normalization configuration in model")
        if any(not isinstance(value, bool) for value in data.values()):
            raise TemplateModelError("invalid normalization configuration in model")
        return cls(**data)

    def __eq__(self, other) -> bool:
        return isinstance(other, NormalizationConfig) and self.to_dict() == other.to_dict()

    def __repr__(self) -> str:
        return f"NormalizationConfig({self.to_dict()})"


class TemplateFilterConfig:
    """
    Thresholds controlling when a repeated block is judged to be a template.

        min_support_docs: minimum number of distinct training documents containing the signature.
        min_support_ratio: minimum fraction of training documents containing the signature (0.0 - 1.0).
        protect_short_blocks: when true, blocks of at most ``short_block_max_words`` words never count as templates
                              (short repeated headings/captions are protected).
        short_block_max_words: word-count threshold for short-block protection.
        min_link_density / max_link_density: inclusive link-density window a block must fall in to be eligible;
                                              navigation rails (high link density) and prose (zero link density) can
                                              thus be allowed or rejected independently.
    """

    _FIELDS = ("min_support_docs", "min_support_ratio", "protect_short_blocks", "short_block_max_words",
               "min_link_density", "max_link_density")

    def __init__(self, min_support_docs: int = 3, min_support_ratio: float = 0.5,
                 protect_short_blocks: bool = True, short_block_max_words: int = 3,
                 min_link_density: float = 0.0, max_link_density: float = 1.0) -> None:
        if min_support_docs < 1:
            raise ValueError("min_support_docs must be >= 1")
        if not 0.0 <= min_support_ratio <= 1.0:
            raise ValueError("min_support_ratio must be within [0.0, 1.0]")
        if short_block_max_words < 0:
            raise ValueError("short_block_max_words must be >= 0")
        if not 0.0 <= min_link_density <= max_link_density <= 1.0:
            raise ValueError("link-density window must satisfy 0.0 <= min <= max <= 1.0")
        self.min_support_docs = int(min_support_docs)
        self.min_support_ratio = float(min_support_ratio)
        self.protect_short_blocks = bool(protect_short_blocks)
        self.short_block_max_words = int(short_block_max_words)
        self.min_link_density = float(min_link_density)
        self.max_link_density = float(max_link_density)

    def is_eligible(self, block: TextBlock) -> Tuple[bool, Optional[str]]:
        """Returns (eligible, veto_reason). ``veto_reason`` is None when the block may participate."""

        if self.protect_short_blocks and block.num_words <= self.short_block_max_words:
            return False, "short_block_protection"
        if not (self.min_link_density <= block.link_density <= self.max_link_density):
            return False, "link_density_out_of_range"
        return True, None

    def to_dict(self) -> Dict[str, float]:
        return {
            "min_support_docs": self.min_support_docs,
            "min_support_ratio": self.min_support_ratio,
            "protect_short_blocks": self.protect_short_blocks,
            "short_block_max_words": self.short_block_max_words,
            "min_link_density": self.min_link_density,
            "max_link_density": self.max_link_density
        }

    @classmethod
    def from_dict(cls, data: Dict[str, float]) -> 'TemplateFilterConfig':
        if not isinstance(data, dict) or set(data.keys()) != set(cls._FIELDS):
            raise TemplateModelError("invalid filter configuration in model")
        return cls(**data)


def block_signature(block: TextBlock, config: NormalizationConfig) -> Optional[str]:
    """
    Builds the deterministic signature of a block.

    The signature is a SHA-256 hash of the normalized text together with the configured structural context. Only the
    hash is ever stored, so the model never carries block text or HTML. Returns None for blocks whose normalized
    text is empty.
    """

    normalized = normalize_block_text(block.text, config.lowercase, config.collapse_digits,
                                      config.strip_punctuation)
    if not normalized:
        return None
    payload = {"text": normalized}
    if config.include_tag_level:
        payload["tag_level"] = block.tag_level
    if config.include_markup_labels:
        markup_labels = sorted(label for label in block.labels if label.startswith(DefaultLabels.MARKUP_PREFIX))
        payload["markup_labels"] = markup_labels
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _SignatureStats:
    """Mutable per-signature accumulator. One document contributes at most once to support_docs."""

    def __init__(self) -> None:
        self.support_docs: Set[str] = set()
        self.occurrences: int = 0
        self.min_num_words: Optional[int] = None
        self.max_num_words: int = 0
        self.min_link_density: float = 1.0
        self.max_link_density: float = 0.0
        self.short_block_vetoes: int = 0
        self.link_density_vetoes: int = 0
        self.examples_tag_levels: Set[int] = set()
        self.examples_markup_labels: Set[str] = set()

    def add_block(self, page_id: str, block: TextBlock) -> None:
        self.occurrences += 1
        if page_id not in self.support_docs:
            self.support_docs.add(page_id)
        self.min_num_words = block.num_words if self.min_num_words is None else min(self.min_num_words,
                                                                                     block.num_words)
        self.max_num_words = max(self.max_num_words, block.num_words)
        self.min_link_density = min(self.min_link_density, block.link_density)
        self.max_link_density = max(self.max_link_density, block.link_density)
        self.examples_tag_levels.add(block.tag_level)
        self.examples_markup_labels.update(
            label for label in block.labels if label.startswith(DefaultLabels.MARKUP_PREFIX))


class TemplateModelTrainer:
    """
    Accumulates repeated-block statistics over a batch of same-site pages.

    Every sample is keyed by a caller-supplied stable page-id. Adding the same page-id again replaces the previous
    contribution (idempotent re-training); removing a page-id revokes its contribution entirely. Blocks failing the
    short-block or link-density conditions are counted as veto evidence but never create support, so they cannot be
    judged templates.
    """

    def __init__(self, normalization_config: Optional[NormalizationConfig] = None,
                 filter_config: Optional[TemplateFilterConfig] = None) -> None:
        self.normalization_config = normalization_config or NormalizationConfig()
        self.filter_config = filter_config or TemplateFilterConfig()
        self._pages: Dict[str, Dict[str, Tuple[TextBlock, Optional[str]]]] = {}
        self._stats: Dict[str, _SignatureStats] = {}

    def _iter_doc_signatures(self, doc: TextDocument) -> Iterable[Tuple[str, TextBlock]]:
        seen: Set[str] = set()
        for block in doc.text_blocks:
            signature = block_signature(block, self.normalization_config)
            if signature is None or signature in seen:
                continue
            seen.add(signature)
            yield signature, block

    def add_document(self, page_id: str, doc: TextDocument) -> None:
        """Adds or replaces the contribution of one document identified by ``page_id``."""

        if not isinstance(page_id, str) or not page_id:
            raise ValueError("page_id must be a non-empty string")
        self.remove_document(page_id)
        page_signatures: Dict[str, Tuple[TextBlock, Optional[str]]] = {}
        for signature, block in self._iter_doc_signatures(doc):
            eligible, reason = self.filter_config.is_eligible(block)
            page_signatures[signature] = (block, None if eligible else reason)
            stats = self._stats.setdefault(signature, _SignatureStats())
            if not eligible:
                if reason == "short_block_protection":
                    stats.short_block_vetoes += 1
                else:
                    stats.link_density_vetoes += 1
                continue
            stats.add_block(page_id, block)
        self._pages[page_id] = page_signatures

    def remove_document(self, page_id: str) -> bool:
        """
        Revokes every contribution previously made by ``page_id`` and drops signatures that no longer have any
        observation. Returns True when a sample was actually removed.
        """

        page_signatures = self._pages.pop(page_id, None)
        if page_signatures is None:
            return False
        for signature in list(self._stats.keys()):
            stats = self._stats[signature]
            stats.support_docs.discard(page_id)
            observation = page_signatures.get(signature)
            if observation is None:
                continue
            _block, veto_reason = observation
            if veto_reason == "short_block_protection":
                stats.short_block_vetoes = max(0, stats.short_block_vetoes - 1)
            elif veto_reason == "link_density_out_of_range":
                stats.link_density_vetoes = max(0, stats.link_density_vetoes - 1)
            else:
                stats.occurrences = max(0, stats.occurrences - 1)
            if stats.occurrences == 0 and stats.short_block_vetoes == 0 and stats.link_density_vetoes == 0:
                del self._stats[signature]
        return True

    @property
    def page_ids(self) -> List[str]:
        return sorted(self._pages.keys())

    def build_model(self) -> 'TemplateModel':
        """Freezes the accumulated statistics into a serializable, decision-evidence-carrying model."""

        num_docs = len(self._pages)
        signatures = {}
        for signature, stats in self._stats.items():
            support_docs = len(stats.support_docs)
            support_ratio = (support_docs / num_docs) if num_docs else 0.0
            meets_count = support_docs >= self.filter_config.min_support_docs
            meets_ratio = support_ratio >= self.filter_config.min_support_ratio
            is_template = bool(stats.support_docs) and meets_count and meets_ratio
            signatures[signature] = {
                "support_docs": support_docs,
                "support_ratio": support_ratio,
                "occurrences": stats.occurrences,
                "is_template": is_template,
                "decision": {
                    "meets_min_support_docs": meets_count,
                    "meets_min_support_ratio": meets_ratio,
                    "threshold_min_support_docs": self.filter_config.min_support_docs,
                    "threshold_min_support_ratio": self.filter_config.min_support_ratio
                },
                "features": {
                    "min_num_words": stats.min_num_words,
                    "max_num_words": stats.max_num_words,
                    "min_link_density": stats.min_link_density,
                    "max_link_density": stats.max_link_density,
                    "tag_levels": sorted(stats.examples_tag_levels),
                    "markup_labels": sorted(stats.examples_markup_labels)
                },
                "vetoes": {
                    "short_block_protection": stats.short_block_vetoes,
                    "link_density_out_of_range": stats.link_density_vetoes
                }
            }
        return TemplateModel(self.normalization_config, self.filter_config, num_docs, signatures)


class TemplateModel:
    """
    Versioned, JSON-serializable snapshot of repeated-block statistics plus the actual template decisions.

    The persisted document contains only signature hashes, numeric evidence and configuration. It never contains
    HTML, block text or page identifiers.
    """

    def __init__(self, normalization_config: NormalizationConfig, filter_config: TemplateFilterConfig,
                 num_training_docs: int, signatures: Dict[str, Dict]) -> None:
        self.normalization_config = normalization_config
        self.filter_config = filter_config
        self.num_training_docs = int(num_training_docs)
        self.signatures = signatures

    @property
    def template_signatures(self) -> Set[str]:
        return {signature for signature, record in self.signatures.items() if record["is_template"]}

    def evidence_for(self, signature: str) -> Optional[Dict]:
        """Returns the persisted decision evidence for one signature (support counts, thresholds, features, vetoes)."""

        return self.signatures.get(signature)

    def to_dict(self) -> Dict:
        return {
            "format": MODEL_FORMAT,
            "version": MODEL_VERSION,
            "normalization_config": self.normalization_config.to_dict(),
            "filter_config": self.filter_config.to_dict(),
            "num_training_docs": self.num_training_docs,
            "signatures": self.signatures
        }

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as model_file:
            json.dump(self.to_dict(), model_file, sort_keys=True, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Dict,
                  expected_normalization: Optional[NormalizationConfig] = None) -> 'TemplateModel':
        if not isinstance(data, dict):
            raise TemplateModelError("template model must be a JSON object")
        if data.get("format") != MODEL_FORMAT:
            raise TemplateModelError(f"unknown template model format: {data.get('format')!r}")
        if data.get("version") != MODEL_VERSION:
            raise TemplateModelError(f"unsupported template model version: {data.get('version')!r}")
        normalization_config = NormalizationConfig.from_dict(data.get("normalization_config"))
        if expected_normalization is not None and normalization_config != expected_normalization:
            raise TemplateModelError("normalization configuration mismatch: refusing to load model")
        filter_config = TemplateFilterConfig.from_dict(data.get("filter_config"))
        num_training_docs = data.get("num_training_docs")
        signatures = data.get("signatures")
        if not isinstance(num_training_docs, int) or isinstance(num_training_docs, bool) or num_training_docs < 0:
            raise TemplateModelError("invalid num_training_docs in model")
        if not isinstance(signatures, dict) or not all(isinstance(key, str) for key in signatures.keys()):
            raise TemplateModelError("invalid signatures section in model")
        return cls(normalization_config, filter_config, num_training_docs, signatures)

    @classmethod
    def load(cls, path: str,
             expected_normalization: Optional[NormalizationConfig] = None) -> 'TemplateModel':
        try:
            with open(path, "r", encoding="utf-8") as model_file:
                data = json.load(model_file)
        except (OSError, ValueError) as ex:
            raise TemplateModelError(f"cannot read template model {path!r}: {ex}") from ex
        return cls.from_dict(data, expected_normalization=expected_normalization)


class TemplateBoilerplateFilter(BoilerpipeFilter):
    """
    Applies a trained :class:`TemplateModel` to new documents.

    Blocks whose signature is a learned template are marked non-content and labeled with
    :data:`TEMPLATE_LABEL`. The filter performs no training and never modifies the model's statistics, so it can be
    inserted anywhere in a :class:`boilerpy3.filters.FilterChain`.
    """

    def __init__(self, model, add_label: bool = True) -> None:
        if isinstance(model, str):
            model = TemplateModel.load(model)
        if not isinstance(model, TemplateModel):
            raise TemplateModelError("TemplateBoilerplateFilter requires a TemplateModel or model path")
        self.model = model
        self.add_label = add_label
        self.last_evidence: List[Dict] = []

    def process(self, doc: TextDocument) -> bool:
        changes = False
        evidence: List[Dict] = []
        template_signatures = self.model.template_signatures
        seen: Set[str] = set()
        for block in doc.text_blocks:
            signature = block_signature(block, self.model.normalization_config)
            if signature is None:
                continue
            if signature in template_signatures:
                if block.set_is_content(False):
                    changes = True
                if self.add_label:
                    block.add_label(TEMPLATE_LABEL)
                if signature not in seen:
                    seen.add(signature)
                    record = self.model.evidence_for(signature)
                    evidence.append({"signature": signature, "evidence": record})
        self.last_evidence = evidence
        return changes
