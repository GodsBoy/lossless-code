#!/usr/bin/env python3
"""
Lossless Dream — pattern extraction, DAG consolidation, and reporting.

Analyzes vault history to extract recurring patterns (corrections, preferences,
anti-patterns, conventions, decisions), consolidates redundant DAG nodes,
and generates dream reports. All operations are lossless — nothing is deleted.
"""

import fcntl
import json
import logging
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import summarise as summarise_mod

# ---------------------------------------------------------------------------
# Logging (file-based, not stdout — critical for background hook execution)
# ---------------------------------------------------------------------------

DREAM_DIR = db.VAULT_DIR / "dream"
LOG_FILE = DREAM_DIR / "dream.log"


def _get_logger() -> logging.Logger:
    logger = logging.getLogger("lossless-dream")
    if not logger.handlers:
        DREAM_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(LOG_FILE))
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


# ---------------------------------------------------------------------------
# Dream model config helper
# ---------------------------------------------------------------------------

def _dream_llm_cfg(config: dict) -> dict:
    """Build a config dict for call_llm using the dream-specific model."""
    return {
        "summaryProvider": config.get("summaryProvider", "anthropic"),
        "summaryModel": config.get("dreamModel", config.get("summaryModel", "claude-haiku-4-5-20251001")),
    }


# ---------------------------------------------------------------------------
# Pattern extraction
# ---------------------------------------------------------------------------

PATTERN_PROMPT = """\
Analyze the following conversation history and extract recurring patterns.
Categorize each pattern as one of: CORRECTION, PREFERENCE, ANTI_PATTERN, CONVENTION, DECISION.

For each pattern, output a line in this exact format:
[CATEGORY] Pattern description. (Source: {source_ids})

Where {source_ids} are the message/summary IDs included in the evidence.

Rules:
- Only extract patterns that appear in 2+ instances or have strong evidence
- Keep descriptions to 1-2 sentences
- Preserve source IDs exactly as provided
- Output ONLY the pattern lines, no preamble or headers

Conversation history:
"""

PATTERN_CATEGORIES = ["CORRECTION", "PREFERENCE", "ANTI_PATTERN", "CONVENTION", "DECISION"]


def _format_for_pattern_extraction(messages: list[dict], summaries: list[dict]) -> list[tuple[str, str]]:
    """Format messages and summaries into text chunks with source IDs.

    Returns list of (text_chunk, source_ids_str) tuples.
    """
    items = []
    for m in messages:
        content = m["content"]
        if len(content) > 4000:
            content = content[:3800] + "\n... [truncated]"
        source_id = f"msg:{m['id']}"
        items.append((f"[{m['role']}] {content}", source_id))

    for s in summaries:
        content = s["content"]
        if len(content) > 4000:
            content = content[:3800] + "\n... [truncated]"
        items.append((content, s["id"]))

    return items


def extract_patterns(
    messages: list[dict],
    summaries: list[dict],
    config: dict,
) -> list[dict]:
    """Extract recurring patterns from messages and summaries via LLM.

    Returns list of pattern dicts: {category, description, source_ids}
    """
    items = _format_for_pattern_extraction(messages, summaries)
    if not items:
        return []

    chunk_size = config.get("chunkSize", 20)
    all_patterns = []
    cfg = _dream_llm_cfg(config)

    for i in range(0, len(items), chunk_size):
        chunk = items[i:i + chunk_size]
        text_parts = []
        for text, sid in chunk:
            text_parts.append(f"[{sid}] {text}")
        chunk_text = "\n\n".join(text_parts)

        full_prompt = PATTERN_PROMPT + chunk_text
        response = summarise_mod.call_llm(full_prompt, cfg)
        if response:
            patterns = _parse_pattern_response(response)
            all_patterns.extend(patterns)

    # If LLM produced nothing useful, try extractive fallback
    if not all_patterns:
        all_patterns = _extractive_pattern_fallback(messages)

    return all_patterns


def _parse_pattern_response(response: str) -> list[dict]:
    """Parse LLM response into pattern dicts."""
    patterns = []
    for line in response.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        for cat in PATTERN_CATEGORIES:
            prefix = f"[{cat}]"
            if line.startswith(prefix):
                desc = line[len(prefix):].strip()
                # Extract source IDs from (Source: ...) suffix
                source_ids = ""
                if "(Source:" in desc:
                    idx = desc.rindex("(Source:")
                    source_ids = desc[idx + 8:].rstrip(")")
                    desc = desc[:idx].strip()
                patterns.append({
                    "category": cat,
                    "description": desc,
                    "source_ids": source_ids.strip(),
                })
                break
    return patterns


def _extractive_pattern_fallback(messages: list[dict]) -> list[dict]:
    """Heuristic pattern extraction when LLM is unavailable."""
    patterns = []
    correction_keywords = ["don't", "no,", "wrong", "should be", "instead of"]
    preference_keywords = ["always", "never", "prefer", "avoid"]

    for m in messages:
        content_lower = m["content"].lower()
        source_id = f"msg:{m['id']}"

        for kw in correction_keywords:
            if kw in content_lower:
                for sentence in m["content"].split("."):
                    if kw in sentence.lower():
                        patterns.append({
                            "category": "CORRECTION",
                            "description": sentence.strip()[:200],
                            "source_ids": source_id,
                        })
                        break
                break

        for kw in preference_keywords:
            if kw in content_lower and m["role"] == "user":
                for sentence in m["content"].split("."):
                    if kw in sentence.lower():
                        patterns.append({
                            "category": "PREFERENCE",
                            "description": sentence.strip()[:200],
                            "source_ids": source_id,
                        })
                        break
                break

    # Deduplicate by description
    seen = set()
    unique = []
    for p in patterns:
        key = p["description"][:80]
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique[:20]


# ---------------------------------------------------------------------------
# DAG Consolidation
# ---------------------------------------------------------------------------

def consolidate_dag(config: dict) -> dict:
    """Find and merge redundant summaries at each depth level.

    Returns {depth: {"consolidated": N}} stats.
    """
    max_depth = db.get_max_summary_depth()
    stats = {}

    for depth in range(max_depth + 1):
        pairs = db.get_overlapping_summaries(depth)
        if not pairs:
            continue

        clusters = _cluster_overlapping(pairs)
        consolidated_in_depth = 0

        for cluster in clusters:
            summaries_in_cluster = []
            for sid in cluster:
                s = db.get_summary(sid)
                if s:
                    summaries_in_cluster.append(s)

            if len(summaries_in_cluster) < 2:
                continue

            merged_content = _merge_summaries(summaries_in_cluster, config)
            if not merged_content:
                continue

            # Collect union of all sources
            all_sources = []
            seen_sources = set()
            for s in summaries_in_cluster:
                sources = db.get_summary_sources(s["id"])
                for src in sources:
                    key = (src["source_type"], src["source_id"])
                    if key not in seen_sources:
                        seen_sources.add(key)
                        all_sources.append(key)

            new_id = db.gen_summary_id()
            session_ids = set(s["session_id"] for s in summaries_in_cluster if s["session_id"])
            sid = session_ids.pop() if len(session_ids) == 1 else None

            db.store_summary(
                summary_id=new_id,
                content=merged_content,
                depth=depth,
                source_ids=list(all_sources),
                session_id=sid,
                token_count=summarise_mod.estimate_tokens(merged_content),
            )

            db.mark_consolidated([s["id"] for s in summaries_in_cluster])
            consolidated_in_depth += len(summaries_in_cluster)

        if consolidated_in_depth > 0:
            stats[depth] = {"consolidated": consolidated_in_depth}

    return stats


def _cluster_overlapping(pairs: list[tuple[str, str]]) -> list[set[str]]:
    """Group overlapping pairs into connected clusters using union-find."""
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in pairs:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        union(a, b)

    clusters_map = {}
    for node in parent:
        root = find(node)
        clusters_map.setdefault(root, set()).add(node)

    return list(clusters_map.values())


def _merge_summaries(summaries: list[dict], config: dict) -> str:
    """Merge multiple summaries into one via LLM, with fallback."""
    texts = [s["content"] for s in summaries]
    combined = "\n\n---\n\n".join(texts)

    prompt = (
        "Merge the following summaries into one concise summary that preserves "
        "all unique information. Remove redundant content but keep all distinct "
        "facts, decisions, file paths, and commands. Output ONLY the merged summary.\n\n"
        f"{combined}"
    )

    result = summarise_mod.call_llm(prompt, _dream_llm_cfg(config))

    # If LLM returned nothing, fall back to dedup merge
    if not result or result == combined:
        return _dedup_merge(texts)

    return result


def _dedup_merge(texts: list[str]) -> str:
    """Simple deduplication merge — concatenate and remove identical sentences."""
    seen = set()
    lines = []
    for text in texts:
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pattern output
# ---------------------------------------------------------------------------

def write_patterns(
    patterns: list[dict],
    project_hash_val: str,
    working_dir: str,
    scope: str,
) -> str:
    """Write patterns to the appropriate patterns.md file. Returns the file path."""
    if scope == "global":
        out_dir = DREAM_DIR / "global"
    else:
        out_dir = DREAM_DIR / "projects" / project_hash_val

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "patterns.md"

    # Group patterns by category
    by_category: dict[str, list[dict]] = {}
    for p in patterns:
        by_category.setdefault(p["category"], []).append(p)

    project_name = os.path.basename(working_dir) if working_dir else "global"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"# Dream Patterns — {project_name}",
        f"# Last updated: {now_str}",
        f"# Source: lossless-code dream cycle",
        "",
    ]

    category_titles = {
        "CORRECTION": "Corrections",
        "PREFERENCE": "Preferences",
        "ANTI_PATTERN": "Anti-Patterns",
        "CONVENTION": "Conventions",
        "DECISION": "Decisions",
    }

    for cat in PATTERN_CATEGORIES:
        items = by_category.get(cat, [])
        if not items:
            continue
        lines.append(f"## {category_titles.get(cat, cat)}")
        for p in items:
            source = f" (Source: {p['source_ids']})" if p["source_ids"] else ""
            lines.append(f"- {p['description']}{source}")
        lines.append("")

    out_path.write_text("\n".join(lines))
    return str(out_path)


# ---------------------------------------------------------------------------
# Dream report
# ---------------------------------------------------------------------------

def generate_report(
    patterns: list[dict],
    consolidation_stats: dict,
    scope: str,
    working_dir: str,
    sessions_analyzed: int,
    duration_seconds: float,
) -> str:
    """Generate a dream report markdown file. Returns the file path."""
    reports_dir = DREAM_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = reports_dir / f"{timestamp}-dream.md"

    by_cat = {}
    for p in patterns:
        by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1

    total_consolidated = sum(s.get("consolidated", 0) for s in consolidation_stats.values())

    lines = [
        f"# Dream Report — {timestamp}",
        "",
        f"- **Scope:** {scope}",
        f"- **Working directory:** {working_dir}",
        f"- **Sessions analyzed:** {sessions_analyzed}",
        f"- **Duration:** {duration_seconds:.1f}s",
        "",
        "## Patterns Extracted",
        "",
        f"- **Total:** {len(patterns)}",
    ]
    for cat, count in sorted(by_cat.items()):
        lines.append(f"- {cat}: {count}")

    lines.extend([
        "",
        "## DAG Consolidation",
        "",
        f"- **Total nodes consolidated:** {total_consolidated}",
    ])
    for depth, stats in sorted(consolidation_stats.items()):
        lines.append(f"- Depth {depth}: {stats.get('consolidated', 0)} nodes merged")

    lines.append("")
    report_path.write_text("\n".join(lines))
    return str(report_path)


# ---------------------------------------------------------------------------
# Auto-trigger check
# ---------------------------------------------------------------------------

def check_auto_trigger(config: dict, working_dir: str) -> bool:
    """Check if automatic dream should run. Fast — DB queries only, no LLM."""
    if not config.get("autoDream", True):
        return False

    # Check if another dream is already running
    lock_path = DREAM_DIR / ".lock"
    if lock_path.exists():
        try:
            lock_fd = open(lock_path, "r")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except (OSError, IOError):
            return False  # Lock is held — another dream is running

    phash = db.project_hash(working_dir)
    last = db.get_last_dream(phash)

    if last is None:
        session_count = db.count_sessions_since(0, working_dir)
        return session_count >= config.get("dreamAfterSessions", 5)

    last_time = last["dreamed_at"]
    now = int(time.time())
    hours_since = (now - last_time) / 3600

    if hours_since >= config.get("dreamAfterHours", 24):
        return True

    sessions_since = db.count_sessions_since(last_time, working_dir)
    if sessions_since >= config.get("dreamAfterSessions", 5):
        return True

    return False


# ---------------------------------------------------------------------------
# Main dream cycle
# ---------------------------------------------------------------------------

def run_dream(scope: str, working_dir: str, config: dict) -> str:
    """Run the full dream cycle. Returns a report summary string."""
    log = _get_logger()
    start_time = time.time()

    # Acquire file lock to prevent concurrent dreams
    lock_path = DREAM_DIR / ".lock"
    DREAM_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        msg = "Another dream cycle is already running. Skipping."
        log.info(msg)
        lock_fd.close()
        return msg

    try:
        return _run_dream_locked(scope, working_dir, config, log, start_time)
    except Exception:
        log.exception("Dream cycle failed")
        return "Dream cycle failed — see dream.log for details."
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _run_dream_locked(scope: str, working_dir: str, config: dict, log, start_time: float) -> str:
    """Inner dream cycle, runs under file lock."""
    phash = db.project_hash(working_dir) if scope != "global" else "global"
    log.info(f"Dream cycle starting: scope={scope} dir={working_dir} hash={phash}")

    # Phase 0: Ensure all messages are summarised first
    log.info("Phase 0: Running summarisation pass")
    summarise_mod.run_full_summarisation()

    # Check for new data since last dream
    last = db.get_last_dream(phash)
    since_ts = last["dreamed_at"] if last else 0

    if scope == "global":
        messages = db.get_messages_since(since_ts)
        summaries = db.get_summaries_since(since_ts)
        sessions_analyzed = db.count_sessions_since(since_ts)
    else:
        messages = db.get_messages_since(since_ts, working_dir)
        summaries = db.get_summaries_since(since_ts, working_dir)
        sessions_analyzed = db.count_sessions_since(since_ts, working_dir)

    if not messages and not summaries:
        msg = "No new data since last dream. Nothing to dream about."
        log.info(msg)
        return msg

    log.info(f"Found {len(messages)} messages and {len(summaries)} summaries since last dream")

    # Phase 1: Pattern extraction
    log.info("Phase 1: Extracting patterns")
    patterns = extract_patterns(messages, summaries, config)
    log.info(f"Extracted {len(patterns)} patterns")

    # Phase 2: DAG consolidation
    log.info("Phase 2: Consolidating DAG")
    consolidation_stats = consolidate_dag(config)
    total_consolidated = sum(s.get("consolidated", 0) for s in consolidation_stats.values())
    log.info(f"Consolidated {total_consolidated} summary nodes")

    # Write pattern files
    if patterns:
        pattern_path = write_patterns(patterns, phash, working_dir, scope)
        log.info(f"Patterns written to {pattern_path}")

    # Phase 3: Generate report
    duration = time.time() - start_time
    report_path = generate_report(
        patterns, consolidation_stats, scope, working_dir,
        sessions_analyzed, duration,
    )
    log.info(f"Dream report written to {report_path}")

    # Update dream log (only on success)
    db.store_dream_log(
        project_hash_val=phash,
        scope=scope,
        patterns_found=len(patterns),
        consolidations=total_consolidated,
        sessions_analyzed=sessions_analyzed,
        report_path=report_path,
    )

    summary = (
        f"Dream complete ({duration:.1f}s)\n"
        f"  Patterns: {len(patterns)}\n"
        f"  Consolidated: {total_consolidated} nodes\n"
        f"  Sessions analyzed: {sessions_analyzed}\n"
        f"  Report: {report_path}"
    )
    log.info(summary)
    return summary


# ---------------------------------------------------------------------------
# CLI entry points (for hook and standalone use)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--check-trigger" in sys.argv:
        cwd = os.getcwd()
        if "--cwd" in sys.argv:
            idx = sys.argv.index("--cwd")
            if idx + 1 < len(sys.argv):
                cwd = sys.argv[idx + 1]
        cfg = db.load_config()
        print("true" if check_auto_trigger(cfg, cwd) else "false")
    elif "--run" in sys.argv:
        project = os.getcwd()
        if "--project" in sys.argv:
            idx = sys.argv.index("--project")
            if idx + 1 < len(sys.argv):
                project = sys.argv[idx + 1]
        scope = "global" if "--global" in sys.argv else "project"
        cfg = db.load_config()
        result = run_dream(scope, project, cfg)
        print(result)
    else:
        print("Usage: dream.py --run [--project DIR] [--global]")
        print("       dream.py --check-trigger [--cwd DIR]")
