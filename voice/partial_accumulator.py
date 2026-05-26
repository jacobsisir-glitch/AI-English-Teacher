from __future__ import annotations

import re
from typing import Any, Callable

from voice.session_state import structured_voice_log


class PartialAccumulator:
    """Accumulates streaming online partials into a coherent text.

    Used as fallback when FunASR offline final doesn't arrive in time.
    Handles two partial styles:
    - Rewrite: new partial replaces old (e.g. "你好" → "你好老师")
    - Incremental: fragments append with overlap dedup (e.g. "你能" + "不能" → "你能不能")
    """

    _NOISE_WORDS = frozenset({"嗯", "啊", "哦", "呃", "额", "唔", "um", "uh", "er", "ah", "oh", "eh", "hm", "hmm", "mmm"})
    _NOISE_PATTERN = re.compile(r"^[\s.,!?，。！？、；;：:]+$")

    def __init__(
        self,
        log_context_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._accumulators: dict[str, str] = {}
        self._partial_counts: dict[str, int] = {}
        self._log_context_provider = log_context_provider

    def feed(self, utterance_id: str, text: str) -> str:
        """Feed a new online partial. Returns the current accumulated text."""
        if not utterance_id:
            return ""
        self._prune()
        cleaned = self._clean(text)
        if not cleaned:
            return self._accumulators.get(utterance_id, "")

        self._partial_counts[utterance_id] = self._partial_counts.get(utterance_id, 0) + 1
        existing = self._accumulators.get(utterance_id, "")

        if not existing:
            self._accumulators[utterance_id] = cleaned
            self._log("partial_accumulator.init", utterance_id=utterance_id, text=cleaned)
            return cleaned

        # Determine style: rewrite vs incremental
        if self._is_rewrite(existing, cleaned):
            self._accumulators[utterance_id] = cleaned
            self._log("partial_accumulator.rewrite", utterance_id=utterance_id, prev=existing, new=cleaned)
            return cleaned

        # Incremental: append with overlap dedup
        merged = self._merge_with_overlap_dedup(existing, cleaned)
        self._accumulators[utterance_id] = merged
        self._log("partial_accumulator.append", utterance_id=utterance_id, prev=existing, added=cleaned, merged=merged)
        return merged

    def get(self, utterance_id: str) -> str:
        return self._accumulators.get(utterance_id, "")

    # Short answers that are clearly meaningful — never reject these.
    _SHORT_ANSWER_WHITELIST: frozenset[str] = frozenset({
        "yes", "no", "ok", "okay", "yeah", "nope", "yep", "sure", "good",
        "bad", "well", "fine", "nice", "great", "right", "wrong", "true",
        "false", "maybe", "always", "never", "often", "seldom",
        "birds", "bird", "cat", "dog", "fish", "car", "bus", "pen",
        "book", "door", "tree", "sun", "moon", "star", "water", "fire",
        "好", "对", "错", "是", "否", "行", "能", "会", "有", "没",
        "主语", "谓语", "宾语", "定语", "状语", "补语", "名词",
        "动词", "形容词", "副词", "代词", "介词", "连词", "叹词",
        "过去", "现在", "将来", "完成", "进行", "一般",
    })

    def is_reliable(self, utterance_id: str, total_audio_ms: int = 0) -> bool:
        """Check if accumulated text is reliable enough to use as fallback.

        Chinese: at least 4 chars, relaxed to 2 for very short utterances.
        English: at least 2 words, relaxed to 1 for very short utterances.
        Short utterances (< 1.2s) with partial counts < 2 are unreliable
        unless the text is in the whitelist.
        """
        text = self._accumulators.get(utterance_id, "")
        if not text.strip():
            return False
        stripped = text.strip()
        lowered = stripped.lower()

        # Whitelist check — always reliable
        if lowered in self._SHORT_ANSWER_WHITELIST:
            return True

        partial_count = self._partial_counts.get(utterance_id, 0)
        has_chinese = bool(re.search(r'[一-鿿]', text))

        if has_chinese:
            chinese_len = len(stripped)
            if chinese_len < 2:
                return False
            if chinese_len < 4 and total_audio_ms < 2000 and partial_count < 2:
                return False
        else:
            words = stripped.split()
            word_count = len(words)
            if word_count < 1:
                return False
            if word_count < 2 and total_audio_ms < 2000 and partial_count < 2:
                return False

        if total_audio_ms < 1200 and partial_count < 2:
            return False
        return True

    def clear(self, utterance_id: str) -> None:
        self._accumulators.pop(utterance_id, None)
        self._partial_counts.pop(utterance_id, None)

    def reset(self) -> None:
        self._accumulators.clear()
        self._partial_counts.clear()

    # ── internal ─────────────────────────────────────────────────────

    @classmethod
    def _clean(cls, text: str) -> str:
        t = text.strip()
        if not t:
            return ""
        if cls._NOISE_PATTERN.match(t):
            return ""
        lowered = t.lower()
        if lowered in cls._NOISE_WORDS:
            return ""
        return t

    @staticmethod
    def _is_rewrite(existing: str, new_text: str) -> bool:
        """Detect if new_text is a rewrite (replacement) of existing."""
        if len(new_text) >= len(existing):
            if existing in new_text:
                return True
            if len(new_text) > len(existing) * 2:
                return True
        # For English: if the texts share a common start (prefix) but diverge,
        # treat as rewrite — avoids merging unrelated fragments.
        if _has_latin_chars(existing) or _has_latin_chars(new_text):
            common_prefix_len = _common_prefix_len(existing, new_text)
            if common_prefix_len >= 2:
                return True
        return False

    @staticmethod
    def _merge_with_overlap_dedup(existing: str, new_text: str) -> str:
        """Append new_text to existing, removing overlap at the boundary.

        For English text, requires cleaner word boundaries at the merge point
        to avoid producing non-words like 'singgoing'.
        """
        has_latin = _has_latin_chars(existing) or _has_latin_chars(new_text)
        max_overlap = min(len(existing), len(new_text), 8)
        min_overlap = 2 if has_latin else 1
        for i in range(max_overlap, min_overlap - 1, -1):
            if existing[-i:] == new_text[:i]:
                if has_latin and not _is_clean_english_merge(existing, new_text, i):
                    # Overlap doesn't produce clean word boundaries — treat as rewrite
                    return new_text if len(new_text) >= len(existing) else existing
                return existing + new_text[i:]
        # No overlap found. For English, prefer the longer text if they look unrelated.
        if has_latin and not _texts_are_related(existing, new_text):
            return new_text if len(new_text) >= len(existing) else existing
        return existing + new_text

    def _prune(self) -> None:
        if len(self._accumulators) > 32:
            keep_ids = list(self._accumulators.keys())[-16:]
            for uid in list(self._accumulators.keys()):
                if uid not in keep_ids:
                    self._accumulators.pop(uid, None)
                    self._partial_counts.pop(uid, None)

    def _log(self, event: str, **payload: Any) -> None:
        context = self._log_context_provider() if self._log_context_provider else {}
        structured_voice_log(event, **context, **payload)


# ── module-level helpers ─────────────────────────────────────────────

def _has_latin_chars(text: str) -> bool:
    return bool(re.search(r'[A-Za-z]', text))


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


def _is_clean_english_merge(existing: str, new_text: str, overlap: int) -> bool:
    """Check if merging two English texts at `overlap` produces clean word boundaries."""
    merge_point = len(existing) - overlap
    if merge_point > 0:
        before = existing[merge_point - 1]
        # Want whitespace or punctuation before the overlap
        if before.isalnum():
            return False
    after_overlap = overlap
    if after_overlap < len(new_text):
        after = new_text[after_overlap]
        # Overlap should end at a word boundary
        if after.isalnum() and new_text[after_overlap - 1:after_overlap + 1].isalpha():
            pass  # can continue within a word
    # Check the merged string doesn't produce nonsense subword repetition
    merged = existing + new_text[overlap:]
    # Quick check: no 3+ consecutive repeated chars (e.g. "goooing")
    if re.search(r'([A-Za-z])\1{3,}', merged):
        return False
    return True


def _texts_are_related(existing: str, new_text: str) -> bool:
    """Heuristic: two texts are related if they share significant word overlap."""
    existing_words = set(existing.lower().split())
    if not existing_words:
        return False
    new_words = set(new_text.lower().split())
    if not new_words:
        return False
    overlap_words = existing_words & new_words
    # At least half the words in the shorter set should overlap
    min_set_size = min(len(existing_words), len(new_words))
    return min_set_size > 0 and len(overlap_words) >= min_set_size / 2
