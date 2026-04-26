"""Contract and decision extractors invoked from the dream cycle.

Lives at the top level of scripts/ rather than inside scripts/db/ because
this module owns extraction logic (LLM calls, prompt templates, regex
fallback), not pure data access. dream.py is at 721 of 800 lines per the
file-size cap, so this code cannot extend dream.py either (TD2).

Pipeline shape mirrors dream.extract_patterns:
1. Format messages and summaries into LLM-friendly text.
2. Try call_llm with json_mode=True.
3. Fall back to regex when the LLM is unavailable or returns malformed JSON.
4. Persist via the storage layer (db.contracts or db.summaries).

The dream cycle calls extract_contract_candidates and
extract_decision_candidates after pattern extraction completes, before
DAG consolidation. Each returns (candidates, mode) where mode is one of
``llm``, ``extractive``, or ``failed``. The combined mode is recorded in
dream_log for lcc_status to surface degraded-mode operation.
"""

import json
import re
import sys
from typing import Optional

# Caps on per-cycle output. The LLM may hallucinate many candidates; the
# rate limit prevents the Pending queue from being flooded by a single
# noisy cycle. Excess candidates are truncated rather than rejected so
# the mode signal still reads "llm" rather than "failed".
DEFAULT_CONTRACTS_PER_CYCLE = 10
DEFAULT_DECISIONS_PER_CYCLE = 15

# Conflict-detection threshold. Word-set Jaccard >= 0.5 between a new
# Pending body and an existing Active body of opposite kind triggers the
# conflicts_with annotation. Heuristic, not exhaustive; the TUI surfaces
# the link so the user has the final call. 0.5 catches paraphrased
# inversions (e.g. "em-dashes in human-facing text" vs "em-dashes for
# human-facing text emphasis") without flagging unrelated short bodies
# that share a few common words.
_CONFLICT_OVERLAP_THRESHOLD = 0.5

# Allowed contract kinds, mirrored from db.contracts._VALID_KINDS but kept
# local to avoid the circular-import cost of pulling that constant in.
_VALID_CONTRACT_KINDS = {"prefer", "forbid", "verify-before"}


CONTRACT_PROMPT = """\
Analyze the following conversation history. Identify durable behavioral
rules the user wants the AI to follow on future sessions. Return at most
{max} rules.

Each rule has a "kind" of:
- "prefer": the user wants this convention/style/approach
- "forbid": the user does not want this
- "verify-before": the user wants verification of a fact before acting

Return your response as JSON with this exact schema:
{{"rules": [{{"kind": "forbid", "body": "em-dashes in human-facing text"}}]}}

Rules:
- Only extract durable preferences the user clearly stated, not casual mentions
- "body" is a single short imperative sentence, no markdown or quotes
- Skip session-specific or one-off requests
- Output ONLY valid JSON, no preamble or commentary

Conversation history:
"""


DECISION_PROMPT = """\
Analyze the following conversation history. Identify durable decisions
made that future sessions should know about. Return at most {max}
decisions.

Each decision has:
- "summary": a single short sentence describing what was decided
- "session_id": the session id where the decision was made (use the
  message source id when uncertain)

Return your response as JSON with this exact schema:
{{"decisions": [{{"summary": "...", "session_id": "..."}}]}}

Rules:
- Only extract concrete decisions (architecture, naming, library, branch)
- Skip exploration that did not conclude
- Skip session-specific micro-decisions
- Output ONLY valid JSON, no preamble or commentary

Conversation history:
"""


def _contracts_llm_cfg(config: dict) -> dict:
    """Build a config dict for call_llm. Falls back contractsModel to
    dreamModel to summaryModel so users can override the contracts model
    independently if desired."""
    return {
        "summaryProvider": config.get("summaryProvider"),
        "summaryModel": config.get(
            "contractsModel",
            config.get(
                "dreamModel",
                config.get("summaryModel", "claude-haiku-4-5-20251001"),
            ),
        ),
        "anthropicBaseUrl": config.get("anthropicBaseUrl"),
        "openaiBaseUrl": config.get("openaiBaseUrl"),
    }


def _format_for_extraction(messages, summaries) -> str:
    """Format messages and summaries into a single chunk of text.

    Mirrors dream._format_for_pattern_extraction's truncation behavior so
    the prompts stay consistent across the dream cycle's three extractors.
    """
    parts = []
    for m in messages:
        content = m.get("content", "")
        if len(content) > 4000:
            content = content[:3800] + "\n... [truncated]"
        parts.append(f"--- msg:{m['id']} ({m.get('role', '?')}) ---\n{content}")
    for s in summaries:
        content = s.get("content", "")
        if len(content) > 4000:
            content = content[:3800] + "\n... [truncated]"
        parts.append(f"--- {s.get('id', '?')} ---\n{content}")
    return "\n\n".join(parts)


def _strip_code_fence(text: str) -> str:
    """Strip ``` and ```json fences if the LLM wraps the JSON response."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text


def _parse_contracts_json(response: str) -> list[dict]:
    """Parse the LLM's JSON contract list. Tolerant: returns [] on any
    parse failure rather than raising, so the caller can fall back to
    the regex extractor without exception handling sprawl."""
    if not response:
        return []
    try:
        data = json.loads(_strip_code_fence(response))
    except (json.JSONDecodeError, ValueError):
        return []
    rules = data.get("rules", []) if isinstance(data, dict) else []
    out: list[dict] = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        kind = r.get("kind")
        body = (r.get("body") or "").strip()
        if kind in _VALID_CONTRACT_KINDS and body:
            out.append({"kind": kind, "body": body})
    return out


def _parse_decisions_json(response: str) -> list[dict]:
    """Parse the LLM's JSON decision list. Same tolerance as contracts."""
    if not response:
        return []
    try:
        data = json.loads(_strip_code_fence(response))
    except (json.JSONDecodeError, ValueError):
        return []
    decisions = data.get("decisions", []) if isinstance(data, dict) else []
    out: list[dict] = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        summary = (d.get("summary") or "").strip()
        sid = (d.get("session_id") or "").strip() or None
        if summary:
            out.append({"summary": summary, "session_id": sid})
    return out


# Regex-based fallback patterns. Low precision, non-zero recall. Used
# only when call_llm returns empty (auth / connection failure).
_CONTRACT_REGEX_PATTERNS = [
    (r"\bnever\s+([^.!?\n]{5,80})", "forbid"),
    (r"\bdon'?t\s+(?:ever\s+)?([^.!?\n]{5,80})", "forbid"),
    (r"\bavoid\s+([^.!?\n]{5,80})", "forbid"),
    (r"\balways\s+([^.!?\n]{5,80})", "prefer"),
    (r"\bprefer\s+([^.!?\n]{5,80})", "prefer"),
    (r"\bmust\s+([^.!?\n]{5,80})", "prefer"),
]


def _extractive_contracts_fallback(messages: list[dict]) -> list[dict]:
    """Regex-based fallback when LLM is unavailable. Scans user messages
    for command-shaped phrasing. Low precision, but better than empty."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content") or ""
        for pattern, kind in _CONTRACT_REGEX_PATTERNS:
            for match in re.finditer(pattern, content, re.IGNORECASE):
                body = match.group(1).rstrip(".!?,;:").strip().lower()
                if 5 < len(body) < 120 and body not in seen:
                    seen.add(body)
                    out.append({"kind": kind, "body": body})
    return out


_DECISION_REGEX_PATTERNS = [
    r"\b(?:we|i'?ll?|let'?s|i decided|deciding to|decided to)\s+([^.!?\n]{10,200})",
    r"\bwe'?re\s+going\s+(?:to|with)\s+([^.!?\n]{10,200})",
    r"\bgoing with\s+([^.!?\n]{10,200})",
]


def _extractive_decisions_fallback(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for m in messages:
        content = m.get("content") or ""
        for pattern in _DECISION_REGEX_PATTERNS:
            for match in re.finditer(pattern, content, re.IGNORECASE):
                summary = match.group(1).rstrip(".!?,;:").strip()
                key = summary.lower()
                if 10 < len(summary) < 200 and key not in seen:
                    seen.add(key)
                    out.append({
                        "summary": summary,
                        "session_id": m.get("session_id"),
                    })
    return out


def _detect_conflicts(new_body: str, new_kind: str) -> Optional[str]:
    """Return the id of an existing Active contract whose body inverts
    the new candidate, or None when no conflict is detected.

    Heuristic: opposing kinds (prefer<->forbid) AND Jaccard word overlap
    on the body bodies of >= _CONFLICT_OVERLAP_THRESHOLD. Cheap, low
    precision but surfaces obvious cases like "PREFER em-dashes" vs
    "FORBID em-dashes". The TUI shows the link so the user has the
    final call.
    """
    if new_kind == "prefer":
        opposing = "forbid"
    elif new_kind == "forbid":
        opposing = "prefer"
    else:
        return None  # verify-before has no opposing kind
    new_words = set(re.findall(r"\w+", new_body.lower()))
    if len(new_words) < 2:
        return None
    import db
    actives = db.list_contracts(status="Active")
    for c in actives:
        if c.get("kind") != opposing:
            continue
        existing = c.get("body") or ""
        existing_words = set(re.findall(r"\w+", existing.lower()))
        if not existing_words:
            continue
        union = new_words | existing_words
        intersect = new_words & existing_words
        jaccard = len(intersect) / len(union) if union else 0.0
        if jaccard >= _CONFLICT_OVERLAP_THRESHOLD:
            return c["id"]
    return None


def extract_contract_candidates(
    messages: list[dict],
    summaries: list[dict],
    config: dict,
) -> tuple[list[dict], str]:
    """Return (candidates, mode) where mode is llm, extractive, noop, or failed.

    Modes:
    - 'noop'        empty input window; nothing was attempted
    - 'llm'         LLM extractor succeeded
    - 'extractive'  LLM path failed/empty; regex fallback found candidates
    - 'failed'      LLM and regex both returned no candidates

    On empty input the prior shape returned mode='llm', which silently
    misrepresented the dream cycle as having run an LLM extraction.
    'noop' surfaces the no-op honestly so lcc_status's lastDreamMode
    field reflects what actually happened.
    """
    chunk_text = _format_for_extraction(messages, summaries)
    if not chunk_text:
        return [], "noop"
    max_n = int(config.get("contractsPerCycleLimit", DEFAULT_CONTRACTS_PER_CYCLE))

    # LLM path. Imported lazily so test fixtures can monkey-patch
    # scripts.summarise.call_llm without forcing a heavy import at
    # module load.
    response = ""
    try:
        from summarise import call_llm
        response = call_llm(
            CONTRACT_PROMPT.format(max=max_n) + chunk_text,
            _contracts_llm_cfg(config),
            json_mode=True,
        )
    except Exception as e:  # noqa: BLE001  - log + fall back to regex
        print(
            f"[lossless-code] contract extraction LLM error: "
            f"{type(e).__name__}",
            file=sys.stderr,
        )

    if response:
        candidates = _parse_contracts_json(response)
        if candidates:
            return candidates[:max_n], "llm"

    # Fallback path
    candidates = _extractive_contracts_fallback(messages)
    if candidates:
        return candidates[:max_n], "extractive"
    return [], "failed"


def extract_decision_candidates(
    messages: list[dict],
    summaries: list[dict],
    config: dict,
) -> tuple[list[dict], str]:
    """Same shape and modes as extract_contract_candidates."""
    chunk_text = _format_for_extraction(messages, summaries)
    if not chunk_text:
        return [], "noop"
    max_n = int(config.get("decisionsPerCycleLimit", DEFAULT_DECISIONS_PER_CYCLE))

    response = ""
    try:
        from summarise import call_llm
        response = call_llm(
            DECISION_PROMPT.format(max=max_n) + chunk_text,
            _contracts_llm_cfg(config),
            json_mode=True,
        )
    except Exception as e:  # noqa: BLE001
        print(
            f"[lossless-code] decision extraction LLM error: "
            f"{type(e).__name__}",
            file=sys.stderr,
        )

    if response:
        candidates = _parse_decisions_json(response)
        if candidates:
            return candidates[:max_n], "llm"

    candidates = _extractive_decisions_fallback(messages)
    if candidates:
        return candidates[:max_n], "extractive"
    return [], "failed"


def store_extracted_contracts(
    candidates: list[dict],
    byline_session_id: Optional[str] = None,
    byline_model: Optional[str] = None,
) -> dict:
    """Persist contract candidates as Pending rows. Each candidate is
    annotated with conflicts_with when an opposing-kind Active contract
    overlaps significantly. Returns a dict of counts:
    ``{stored, deduped, conflicts_detected}``.
    """
    import db
    stats = {"stored": 0, "deduped": 0, "conflicts_detected": 0}
    for c in candidates:
        kind = c.get("kind")
        body = (c.get("body") or "").strip()
        if not kind or not body:
            continue
        conflict_id = _detect_conflicts(body, kind)
        try:
            cid = db.store_contract_candidate(
                kind=kind,
                body=body,
                byline_session_id=byline_session_id,
                byline_model=byline_model,
                conflicts_with=conflict_id,
            )
        except ValueError:
            # store_contract_candidate validates kind and non-empty body;
            # a parser bug or noisy LLM might let through invalid input.
            # Skip rather than fail the whole batch.
            continue
        if cid is None:
            stats["deduped"] += 1
        else:
            stats["stored"] += 1
            if conflict_id:
                stats["conflicts_detected"] += 1
    return stats


def store_extracted_decisions(candidates: list[dict]) -> int:
    """Persist decision candidates as summaries with kind='decision'.
    Returns the number of rows stored."""
    import db
    stored = 0
    for d in candidates:
        summary = (d.get("summary") or "").strip()
        if not summary:
            continue
        sid = db.gen_summary_id()
        db.store_summary(
            summary_id=sid,
            content=summary,
            depth=0,
            source_ids=[],
            session_id=d.get("session_id"),
            kind="decision",
        )
        stored += 1
    return stored


def combine_modes(contracts_mode: str, decisions_mode: str) -> str:
    """Combine the per-extractor mode signals into a single dream_log
    mode value. Used by the dream cycle to record degraded-mode operation
    that lcc_status surfaces in U13.

    'noop' is treated as transparent: if one extractor was a no-op and
    the other actually ran, the combined mode reflects what ran. Two
    noops collapse to 'noop'. Otherwise mismatched modes report 'mixed'.
    """
    if contracts_mode == decisions_mode:
        return contracts_mode
    if contracts_mode == "noop":
        return decisions_mode
    if decisions_mode == "noop":
        return contracts_mode
    return "mixed"


__all__ = [
    "extract_contract_candidates",
    "extract_decision_candidates",
    "store_extracted_contracts",
    "store_extracted_decisions",
    "combine_modes",
    "CONTRACT_PROMPT",
    "DECISION_PROMPT",
    "DEFAULT_CONTRACTS_PER_CYCLE",
    "DEFAULT_DECISIONS_PER_CYCLE",
]
