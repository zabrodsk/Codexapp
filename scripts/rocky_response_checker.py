#!/usr/bin/env python3
"""
Rocky Response Style Checker

Lightweight checker that flags response style violations based on
improvement/response_style_rules.md. This is a detection tool, not a
rewrite engine — it flags issues for review.

Usage:
    from rocky_response_checker import ResponseChecker
    checker = ResponseChecker()
    violations = checker.check_response(prompt, response)

Disable: simply don't import/call this module. No side effects.
"""

import re
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class StyleViolation:
    """A single style violation found in a response."""
    rule: str
    severity: str  # "ban" | "warn"
    detail: str
    matched_text: Optional[str] = None


# Banned opener patterns — must not appear at start of response
BANNED_OPENERS = [
    r"^great question",
    r"^that'?s a great question",
    r"^good question",
    r"^i'?d be happy to help",
    r"^i'?d love to help",
    r"^sure thing",
    r"^of course!",
    r"^absolutely!",
    r"^no problem!",
    r"^let me help you with that",
    r"^thanks for asking",
    r"^thanks for reaching out",
    r"^that'?s an interesting",
    r"^what a great",
    r"^i appreciate you",
    r"^here (?:are|is) (?:your|the)",
    r"^as you can see",
]

# Banned filler phrases — should not appear anywhere
BANNED_FILLER = [
    r"it'?s worth noting that",
    r"it'?s important to note that",
    r"as you may know",
    r"as mentioned earlier",
    r"at the end of the day",
    r"moving forward",
    r"going forward",
    r"with that being said",
    r"having said that",
    r"just to clarify",
    r"for what it'?s worth",
]

# Discouraged trailing padding
TRAILING_PADDING = [
    r"hope that helps!?",
    r"let me know if you need anything else!?",
    r"feel free to ask!?",
    r"don'?t hesitate to ask!?",
    r"happy to help further!?",
    r"i'?m here if you need me!?",
]

# Prompt categories for length thresholds
TRIVIAL_PROMPT_PATTERNS = [
    r"^(hi|hey|hello|yo|sup|what'?s up)\b",
    r"^thanks?\b",
    r"^thank you\b",
    r"^ok\b",
    r"^cool\b",
    r"^got it\b",
    r"^nice\b",
]

# Max sentences for trivial prompts
TRIVIAL_MAX_SENTENCES = 2
# Max sentences for simple questions
SIMPLE_MAX_SENTENCES = 4


def _count_sentences(text: str) -> int:
    """Count approximate number of sentences in text."""
    # Split on sentence-ending punctuation followed by space or end
    sentences = re.split(r'[.!?]+(?:\s|$)', text.strip())
    # Filter out empty strings
    return len([s for s in sentences if s.strip()])


def _is_trivial_prompt(prompt: str) -> bool:
    """Check if a prompt is trivial (greeting, thanks, etc.)."""
    prompt_lower = prompt.strip().lower()
    return any(re.match(p, prompt_lower) for p in TRIVIAL_PROMPT_PATTERNS)


class ResponseChecker:
    """
    Checks Rocky responses against style rules.
    Returns a list of StyleViolation objects.
    """

    def __init__(self, rules_path: Optional[Path] = None):
        """Initialize with optional custom rules path."""
        self.rules_path = rules_path

    def check_response(
        self,
        prompt: str,
        response: str,
        category: Optional[str] = None,
    ) -> list[StyleViolation]:
        """
        Check a response for style violations.

        Args:
            prompt: The user's prompt that triggered this response
            response: Rocky's response text
            category: Optional category override (greeting, thanks, etc.)

        Returns:
            List of StyleViolation objects found
        """
        violations = []
        response_lower = response.strip().lower()

        # Check banned openers
        violations.extend(self._check_banned_openers(response_lower))

        # Check banned filler
        violations.extend(self._check_banned_filler(response_lower))

        # Check trailing padding
        violations.extend(self._check_trailing_padding(response_lower))

        # Check length for trivial prompts
        if category == "greeting" or category == "thanks" or _is_trivial_prompt(prompt):
            violations.extend(self._check_trivial_length(response))

        # Check for user question restatement
        violations.extend(self._check_restatement(prompt, response))

        return violations

    def _check_banned_openers(self, response_lower: str) -> list[StyleViolation]:
        """Check for banned opener patterns at start of response."""
        violations = []
        for pattern in BANNED_OPENERS:
            match = re.match(pattern, response_lower, re.IGNORECASE)
            if match:
                violations.append(StyleViolation(
                    rule="banned_opener",
                    severity="ban",
                    detail=f"Response starts with banned opener pattern",
                    matched_text=match.group(0),
                ))
        return violations

    def _check_banned_filler(self, response_lower: str) -> list[StyleViolation]:
        """Check for banned filler phrases anywhere in response."""
        violations = []
        for pattern in BANNED_FILLER:
            match = re.search(pattern, response_lower, re.IGNORECASE)
            if match:
                violations.append(StyleViolation(
                    rule="banned_filler",
                    severity="ban",
                    detail=f"Response contains banned filler phrase",
                    matched_text=match.group(0),
                ))
        return violations

    def _check_trailing_padding(self, response_lower: str) -> list[StyleViolation]:
        """Check for discouraged trailing padding."""
        violations = []
        # Check the last ~100 chars for trailing padding
        tail = response_lower[-150:] if len(response_lower) > 150 else response_lower
        for pattern in TRAILING_PADDING:
            match = re.search(pattern, tail, re.IGNORECASE)
            if match:
                violations.append(StyleViolation(
                    rule="trailing_padding",
                    severity="warn",
                    detail=f"Response ends with empty politeness padding",
                    matched_text=match.group(0),
                ))
        return violations

    def _check_trivial_length(self, response: str) -> list[StyleViolation]:
        """Check if a trivial prompt got a too-long response."""
        violations = []
        sentence_count = _count_sentences(response)
        if sentence_count > TRIVIAL_MAX_SENTENCES:
            violations.append(StyleViolation(
                rule="trivial_too_long",
                severity="warn",
                detail=f"Trivial prompt got {sentence_count} sentences (max {TRIVIAL_MAX_SENTENCES})",
            ))
        return violations

    def _check_restatement(self, prompt: str, response: str) -> list[StyleViolation]:
        """Check if the response unnecessarily restates the user's question."""
        violations = []
        prompt_words = set(prompt.lower().split())
        # Remove very common words
        stop_words = {"the", "a", "an", "is", "are", "was", "were", "do", "does",
                      "did", "can", "could", "would", "should", "i", "you", "it",
                      "my", "your", "this", "that", "what", "how", "when", "where",
                      "why", "to", "of", "in", "on", "at", "for", "with", "and",
                      "or", "not", "be", "have", "has", "had"}
        meaningful_words = prompt_words - stop_words

        if len(meaningful_words) < 3:
            return violations

        # Check first sentence of response
        first_sentence = response.split(".")[0].lower() if "." in response else response.lower()
        first_words = set(first_sentence.split())

        # If >60% of meaningful prompt words appear in the first sentence, it's likely a restatement
        overlap = meaningful_words & first_words
        if len(meaningful_words) > 0 and len(overlap) / len(meaningful_words) > 0.6:
            violations.append(StyleViolation(
                rule="restatement",
                severity="warn",
                detail="Response appears to restate the user's question before answering",
                matched_text=response.split(".")[0] if "." in response else None,
            ))

        return violations


def check_response_from_fixture(fixture_path: str) -> dict:
    """
    Run the checker against all cases in a fixture file.
    Returns a summary dict.
    """
    checker = ResponseChecker()
    path = Path(fixture_path)
    with open(path) as f:
        data = json.load(f)

    results = {}
    for case in data.get("cases", []):
        case_id = case["id"]
        prompt = case["prompt"]

        # Check bad response (should have violations)
        bad_resp = case.get("bad_response")
        if bad_resp:
            bad_violations = checker.check_response(
                prompt, bad_resp, category=case.get("category")
            )
            results[f"{case_id}_bad"] = {
                "violations": len(bad_violations),
                "details": [v.detail for v in bad_violations],
            }

        # Check good response (should have few/no violations)
        good_resp = case.get("good_response")
        if good_resp:
            good_violations = checker.check_response(
                prompt, good_resp, category=case.get("category")
            )
            results[f"{case_id}_good"] = {
                "violations": len(good_violations),
                "details": [v.detail for v in good_violations],
            }

    return results


if __name__ == "__main__":
    import sys

    fixture_path = (
        Path(__file__).parent.parent
        / "improvement"
        / "fixtures"
        / "response_cases.json"
    )

    if not fixture_path.exists():
        print(f"Fixture file not found: {fixture_path}")
        sys.exit(1)

    results = check_response_from_fixture(str(fixture_path))
    print(json.dumps(results, indent=2))
