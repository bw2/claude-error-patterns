#!/usr/bin/env python3
"""Analyze Claude Code session logs to find recurring fail-then-fix patterns.

Scans JSONL session transcripts, detects error chains (failed tool calls
followed by successful corrections), and reports them grouped by error category.

Usage:
    # Human-readable markdown table
    python3 error_patterns_cli.py --days 14

    # JSON output for programmatic use
    python3 error_patterns_cli.py --json --days 14

    # Filter by minimum occurrence count
    python3 error_patterns_cli.py --min-count 5

    # Exclude specific categories
    python3 error_patterns_cli.py --exclude generic_exit_code,generic_error

    # List available error categories
    python3 error_patterns_cli.py --list-categories
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

DEFAULT_PROJECTS_DIR = os.path.expanduser("~/.claude/projects/")
DEFAULT_DAYS = 14

# Error categories: (category_name, pattern_in_output, description)
# Order matters — first match wins.
ERROR_CATEGORIES = [
    # Tool-level errors (is_error=true from Claude Code harness)
    ("edit_before_read", r"File has not been read yet", "Edit/Write before reading file"),
    ("edit_ambiguous_match", r"Found \d+ matches.*replace_all is false", "Edit matched multiple strings"),
    ("edit_no_match", r"did not match any content|String to replace not found", "Edit old_string not found"),
    ("safety_net_blocked", r"BLOCKED by Safety Net", "Blocked by Safety Net hook"),
    ("hook_blocked", r"hook error.*Blocked", "Blocked by pre-commit/hook"),
    ("sibling_error", r"Sibling tool call errored", "Parallel tool call sibling failed"),
    ("cancelled", r"Cancelled:", "Tool call cancelled"),

    # Command not found
    ("cmd_not_found_python", r"command not found: python\b", "python (not python3) not found"),
    ("cmd_not_found_pip", r"command not found: pip\b", "pip (not pip3) not found"),
    ("cmd_not_found_zcat", r"command not found.*zcat|zcat.*command not found", "zcat not found (use gzcat)"),
    ("cmd_not_found_other", r"command not found", "Command not found"),

    # Python errors
    ("python_syntax", r"SyntaxError", "Python SyntaxError"),
    ("python_module", r"ModuleNotFoundError|ImportError", "Python missing module"),
    ("python_type", r"TypeError", "Python TypeError"),
    ("python_name", r"NameError", "Python NameError"),
    ("python_index", r"IndexError", "Python IndexError"),
    ("python_key", r"KeyError", "Python KeyError"),
    ("python_value", r"ValueError", "Python ValueError"),
    ("python_attribute", r"AttributeError", "Python AttributeError"),
    ("python_file_not_found", r"FileNotFoundError", "Python FileNotFoundError"),
    ("python_os_error", r"OSError|IOError", "Python OS/IO Error"),
    ("python_runtime", r"RuntimeError", "Python RuntimeError"),
    ("python_connection", r"ConnectionError|ConnectionRefusedError|TimeoutError|ReadTimeoutError", "Python connection error"),
    ("python_other_traceback", r"Traceback \(most recent call last\)", "Python traceback (other)"),

    # File system
    ("no_such_file", r"No such file or directory", "File/directory not found"),
    ("is_a_directory", r"EISDIR|Is a directory|illegal operation on a directory", "Tried to read a directory"),
    ("permission_denied", r"Permission denied|EACCES", "Permission denied"),
    ("no_space", r"No space left on device", "Disk full"),

    # Shell/awk/grep syntax
    ("grep_invalid_option", r"grep:.*invalid option", "grep invalid option (e.g. -P on macOS)"),
    ("awk_syntax", r"awk: syntax error", "awk syntax error"),

    # Git errors
    ("git_fatal", r"fatal:", "Git fatal error"),

    # Cloud/API errors
    ("gcloud_not_found", r"No URLs matched|BucketNotFoundException", "GCS object not found"),
    ("api_rate_limit", r"rate limit|HTTP 429|status.?429|Too Many Requests", "API rate limit"),
    ("api_auth", r"HTTP 401|HTTP 403|403.*Forbidden|Unauthorized|authentication.*failed", "API auth error"),

    # Build errors
    ("npm_error", r"npm ERR!", "npm error"),
    ("cargo_error", r"cargo error|error\[E\d+\]", "Rust cargo error"),
    ("compile_error", r"error:.*expected|undefined reference|linker command failed", "Compilation error"),

    # Test failures
    ("test_failure", r"\d+ failed|FAILURES|FAIL:.*test_|test session starts.*\d+ failed|FAILED \(errors=|FAILED \(failures=", "Test failure"),

    # Generic (catch-all, must be last)
    ("generic_exit_code", r"Exit code [1-9]", "Non-zero exit code"),
    ("generic_error", r"[Ee]rror:|ERROR:", "Generic error"),
]

# Category assigned programmatically when is_error=True but no regex matches.
# Listed here so --list-categories and --exclude can reference it.
TOOL_ERROR_UNKNOWN = ("tool_error_unknown", "Unknown tool error")

# Suggested CLAUDE.md rules for common error categories.
# Maps category -> (rule_text, target_section_in_claude_md)
RULE_SUGGESTIONS = {
    "cmd_not_found_python": (
        "Use `python3` and `pip3` — `python` and `pip` are not available on this machine",
        "macOS-Specific Commands",
    ),
    "cmd_not_found_pip": (
        "Use `pip3` — `pip` is not available on this machine",
        "macOS-Specific Commands",
    ),
    "cmd_not_found_zcat": (
        "Use `gzcat` rather than `zcat` on macOS",
        "macOS-Specific Commands",
    ),
    "grep_invalid_option": (
        "Use `grep -E` (extended regex) — `grep -P` (Perl regex) is not supported on macOS",
        "macOS-Specific Commands",
    ),
    "awk_syntax": (
        "For complex text processing, prefer `python3` over `awk` — BSD awk on macOS has limited syntax vs GNU awk",
        "macOS-Specific Commands",
    ),
    "python_syntax": (
        "For multi-line Python one-liners, use heredoc (`python3 << 'EOF'`) instead of `python3 -c \"...\"`"
        " — avoids quoting/escaping issues that cause SyntaxErrors",
        "macOS-Specific Commands",
    ),
    "edit_before_read": (
        "Always Read a file before using Edit or Write on it",
        "General Instructions",
    ),
    "edit_ambiguous_match": (
        "When using Edit, include enough surrounding context in old_string to ensure a unique match",
        "General Instructions",
    ),
    "edit_no_match": (
        "When using Edit, verify the exact string exists in the file (it may have changed since last read)",
        "General Instructions",
    ),
    "hook_blocked": (
        "Do not include Co-Authored-By lines in commit messages — a pre-commit hook will block the commit",
        "Git",
    ),
    "safety_net_blocked": (
        "Respect Safety Net restrictions — use safe alternatives (see Safety & Destructive Commands section)",
        "Safety & Destructive Commands",
    ),
    "no_such_file": (
        "Verify file/directory paths exist before operating on them (use ls or Glob first)",
        "General Instructions",
    ),
}


def find_jsonl_files(projects_dir, cutoff_date):
    """Find all JSONL files modified since cutoff_date."""
    result = []
    for root, dirs, files in os.walk(projects_dir):
        for fname in files:
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(root, fname)
            try:
                mtime = os.path.getmtime(fpath)
                mod_date = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
                if mod_date >= cutoff_date:
                    result.append((os.path.getsize(fpath), mod_date, fpath))
            except OSError:
                pass
    result.sort(reverse=True)
    return result


def classify_error(content):
    """Classify an error by matching against ERROR_CATEGORIES. Returns (category, description)."""
    for cat_name, pattern, desc in ERROR_CATEGORIES:
        if re.search(pattern, content):
            return cat_name, desc
    return None, None


def _build_entry(result_block, tool_uses, session_id, timestamp):
    """Build a tool result entry from a tool_result block."""
    tu = tool_uses.get(result_block.get("tool_use_id", ""), {})
    result_content = result_block.get("content", "")
    if isinstance(result_content, list):
        result_content = " ".join(
            str(x.get("text", x.get("content", "")))
            for x in result_content if isinstance(x, dict)
        )
    return {
        "tool_name": tu.get("name", "unknown"),
        "tool_input": tu.get("input", {}),
        "result_content": str(result_content)[:2000],
        "is_error": result_block.get("is_error", False),
        "session_id": session_id,
        "timestamp": tu.get("timestamp", timestamp),
    }


def extract_tool_calls_and_results(fpath):
    """Extract ordered tool call/result pairs from a JSONL file."""
    entries = []
    tool_uses = {}
    session_id = None

    try:
        with open(fpath) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(d, dict):
                    continue

                if not session_id:
                    session_id = d.get("sessionId", "")

                timestamp = d.get("timestamp", "")

                # Collect messages from both top-level and progress-wrapped
                messages_to_check = []
                msg_type = d.get("type")
                if msg_type in ("assistant", "user"):
                    messages_to_check.append(d.get("message", {}))
                elif msg_type == "progress":
                    data = d.get("data", {})
                    if isinstance(data, dict):
                        inner_msg = data.get("message", {})
                        if isinstance(inner_msg, dict):
                            messages_to_check.append(inner_msg.get("message", {}))

                for msg in messages_to_check:
                    if not isinstance(msg, dict):
                        continue
                    content = msg.get("content", [])
                    if not isinstance(content, list):
                        continue
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "tool_use":
                            tool_uses[c.get("id", "")] = {
                                "name": c.get("name", ""),
                                "input": c.get("input", {}),
                                "timestamp": timestamp,
                            }
                        elif c.get("type") == "tool_result":
                            entries.append(_build_entry(
                                c, tool_uses, session_id, timestamp))

                tur = d.get("toolUseResult")
                if isinstance(tur, dict) and tur.get("type") == "tool_result":
                    entries.append(_build_entry(
                        tur, tool_uses, session_id, timestamp))

    except Exception as e:
        print(f"  Warning: error reading {fpath}: {e}", file=sys.stderr)

    return entries


def _extract_error_line(content, category=None):
    """Find the most informative error line from output content.

    If category is given, prefer lines matching that category's pattern.
    Falls back to any error-pattern-matching line, then first 200 chars.
    """
    # First try to find a line matching the specific classified category
    if category:
        cat_pattern = next((p for name, p, _ in ERROR_CATEGORIES if name == category), None)
        if cat_pattern:
            for line in content.split("\n"):
                line = line.strip()
                if re.search(cat_pattern, line):
                    return line[:200]
    # Fall back to any error-matching line
    for line in content.split("\n"):
        line = line.strip()
        if any(re.search(p, line) for _, p, _ in ERROR_CATEGORIES):
            return line[:200]
    return content[:200]


def detect_error(entry):
    """Detect if an entry is an error and classify it.

    Uses a conservative, two-tier approach to minimize false positives:
      Tier 1: is_error=True (harness definitively flagged the tool call as failed)
      Tier 2: Bash output contains "Exit code N" (non-zero exit, even if
              is_error wasn't set)

    If neither signal is present, the command is assumed to have succeeded.
    Any error-like text in its output is just content being displayed, not
    an actual failure.

    Returns (error_category, error_description, error_snippet) or (None, None, None).
    """
    content = entry["result_content"]

    # Tier 1: Harness explicitly flagged as error
    if entry["is_error"]:
        cat, desc = classify_error(content)
        if cat in ("cancelled", "sibling_error"):
            return None, None, None
        if cat:
            return cat, desc, _extract_error_line(content, cat)
        return TOOL_ERROR_UNKNOWN[0], TOOL_ERROR_UNKNOWN[1], content[:200]

    # Tier 2: Bash command with non-zero exit code anywhere in output
    if entry["tool_name"] == "Bash" and re.search(r"Exit code [1-9]", content):
        cat, desc = classify_error(content)
        if cat:
            return cat, desc, _extract_error_line(content, cat)

    # No definitive error signal — skip to avoid false positives
    return None, None, None


def get_command_summary(entry):
    """Get a short summary of what a tool call does."""
    if entry["tool_name"] == "Bash":
        cmd = entry["tool_input"].get("command", "")
        return cmd[:100] + "..." if len(cmd) > 100 else cmd
    elif entry["tool_name"] == "Edit":
        return f"Edit {os.path.basename(entry['tool_input'].get('file_path', '?'))}"
    elif entry["tool_name"] == "Write":
        return f"Write {os.path.basename(entry['tool_input'].get('file_path', '?'))}"
    return f"{entry['tool_name']}(...)"


def find_fail_fix_chains(entries):
    """Find sequences of failed attempts followed by success."""
    chains = []
    i = 0
    while i < len(entries):
        entry = entries[i]

        if entry["tool_name"] not in ("Bash", "Edit", "Write"):
            i += 1
            continue

        cat, desc, snippet = detect_error(entry)
        if not cat:
            i += 1
            continue

        # Start a chain
        chain_entries = [(entry, cat, desc, snippet)]

        j = i + 1
        found_success = False
        while j < min(i + 20, len(entries)):
            next_entry = entries[j]
            if next_entry["tool_name"] not in ("Bash", "Edit", "Write"):
                j += 1
                continue

            next_cat, next_desc, next_snippet = detect_error(next_entry)
            if next_cat:
                chain_entries.append((next_entry, next_cat, next_desc, next_snippet))
                j += 1
                continue

            # Success
            chain_entries.append((next_entry, None, None, None))
            found_success = True
            break

        if found_success and len(chain_entries) >= 2:
            first_entry, first_cat, first_desc, first_snippet = chain_entries[0]
            last_entry = chain_entries[-1][0]

            chains.append({
                "error_category": first_cat,
                "error_description": first_desc,
                "error_snippet": first_snippet,
                "first_cmd": get_command_summary(first_entry),
                "final_cmd": get_command_summary(last_entry),
                "chain_length": len(chain_entries),
                "source_file": None,  # set by caller
            })
            i = j + 1
        else:
            i += 1

    return chains


def normalize_command(cmd):
    """Normalize a command for deduplication, preserving key identifiers.

    Less aggressive than typical normalization — keeps filenames, script names,
    and short arguments so that genuinely different commands stay distinct.
    """
    # For Edit/Write tool summaries, keep as-is (already short and specific)
    if cmd.startswith("Edit ") or cmd.startswith("Write "):
        return cmd
    s = cmd
    # Replace long quoted strings (30+ chars) but keep short ones
    s = re.sub(r'"[^"]{30,}"', '"<str>"', s)
    s = re.sub(r"'[^']{30,}'", "'<str>'", s)
    # Replace directory prefixes but keep the filename
    s = re.sub(r"/[\w./~-]+/([^/\s\"']+)", r"<dir>/\1", s)
    # Replace UUIDs and long hex strings (12+ chars)
    s = re.sub(r"\b[0-9a-f]{12,}\b", "<hex>", s)
    # Replace large numbers (4+ digits) but keep small ones
    s = re.sub(r"\b\d{4,}\b", "<N>", s)
    # Collapse repeated whitespace
    s = re.sub(r"\s+", " ", s)
    # Truncate to first 120 chars for grouping
    return s[:120]


def find_wheel_spinning(entries):
    """Find sequences of 3+ repeated similar commands that keep failing.

    Iterates through Bash/Edit/Write tool results, tracking runs of consecutive
    entries with the same normalized command. Non-Bash/Edit/Write entries are
    skipped (they don't break the run). When a run has 3+ entries where all but
    possibly the last are errors, it's recorded as a wheel-spinning loop.

    Returns list of dicts with keys: normalized_cmd, loop_length, first_cmd,
    last_cmd, resolved, error_snippets, error_category, source_file.
    """
    loops = []
    relevant = [(i, e) for i, e in enumerate(entries)
                if e["tool_name"] in ("Bash", "Edit", "Write")]

    if len(relevant) < 3:
        return loops

    run_start = 0
    while run_start < len(relevant):
        _, start_entry = relevant[run_start]
        start_norm = normalize_command(get_command_summary(start_entry))

        run_end = run_start + 1
        while run_end < len(relevant):
            _, next_entry = relevant[run_end]
            if normalize_command(get_command_summary(next_entry)) != start_norm:
                break
            run_end += 1

        run_length = run_end - run_start
        if run_length >= 3:
            run_entries = [relevant[k][1] for k in range(run_start, run_end)]
            # Classify each entry once
            error_results = [detect_error(e) for e in run_entries]

            # All-but-last must be errors (last may or may not be)
            non_last_errors = sum(1 for cat, _, _ in error_results[:-1] if cat)
            if non_last_errors == run_length - 1:
                first_cat = next((cat for cat, _, _ in error_results if cat), None)
                error_snippets = list(dict.fromkeys(
                    snippet for _, _, snippet in error_results if snippet
                ))[:5]
                loops.append({
                    "normalized_cmd": start_norm,
                    "loop_length": run_length,
                    "first_cmd": get_command_summary(run_entries[0]),
                    "last_cmd": get_command_summary(run_entries[-1]),
                    "resolved": error_results[-1][0] is None,
                    "error_snippets": error_snippets,
                    "error_category": first_cat or "tool_error_unknown",
                    "source_file": None,  # set by caller
                })

        run_start = run_end

    return loops


def deduplicate_wheel_spinning(loops):
    """Deduplicate wheel-spinning loops by (normalized_cmd, error_category).

    Groups loops, picks the longest as the best example, adds dedup_count
    and aggregated stats.
    """
    groups = defaultdict(list)
    for loop in loops:
        key = (loop["normalized_cmd"], loop["error_category"])
        groups[key].append(loop)

    result = []
    for group_loops in groups.values():
        best = max(group_loops, key=lambda l: l["loop_length"])
        best["dedup_count"] = len(group_loops)
        best["all_loop_lengths"] = [l["loop_length"] for l in group_loops]
        best["resolved_count"] = sum(1 for l in group_loops if l["resolved"])
        result.append(best)
    return result


def build_wheel_spinning_data(loops):
    """Build output-ready wheel-spinning pattern entries from deduplicated loops.

    Returns list of dicts sorted by count (most frequent first).
    """
    cat_desc_map = {name: desc for name, _, desc in ERROR_CATEGORIES}
    cat_desc_map[TOOL_ERROR_UNKNOWN[0]] = TOOL_ERROR_UNKNOWN[1]

    patterns = []
    for loop in loops:
        count = loop.get("dedup_count", 1)
        lengths = loop.get("all_loop_lengths", [loop["loop_length"]])
        resolved_count = loop.get("resolved_count", 1 if loop["resolved"] else 0)
        rule, section = RULE_SUGGESTIONS.get(loop["error_category"], (None, None))

        entry = {
            "category": loop["error_category"],
            "description": cat_desc_map.get(loop["error_category"], "Unknown"),
            "normalized_cmd": loop["normalized_cmd"],
            "count": count,
            "avg_loop_length": round(sum(lengths) / len(lengths), 1),
            "resolved_pct": round(100 * resolved_count / count) if count else 0,
            "first_cmd": loop["first_cmd"][:200],
            "error_snippets": [s[:200] for s in loop.get("error_snippets", [])],
        }
        if rule:
            entry["suggested_rule"] = rule
            entry["suggested_section"] = section

        patterns.append(entry)

    patterns.sort(key=lambda x: -x["count"])
    return patterns


def format_wheel_spinning_markdown(ws_patterns):
    """Format wheel-spinning patterns as a markdown section."""
    if not ws_patterns:
        return ""

    total_wasted = sum(
        p["count"] * p["avg_loop_length"] for p in ws_patterns
    )

    lines = []
    lines.append("\n## Wheel-Spinning Patterns (Repeated Failing Attempts)\n")
    lines.append(f"**Loops found:** {sum(p['count'] for p in ws_patterns)}")
    lines.append(f"**Total wasted attempts:** {int(total_wasted)}\n")

    lines.append("| # | Count | Avg Length | Resolved? | Command Pattern | Error |")
    lines.append("|---|-------|-----------|-----------|----------------|-------|")

    for i, p in enumerate(ws_patterns):
        cmd = p["normalized_cmd"][:50].replace("|", "\\|").replace("\n", " ")
        error_snip = (p["error_snippets"][0][:50] if p["error_snippets"] else "").replace("|", "\\|").replace("\n", " ")
        resolved_str = f"{p['resolved_pct']}% yes"

        lines.append(
            f"| {i + 1} | {p['count']} | {p['avg_loop_length']} attempts "
            f"| {resolved_str} | `{cmd}` | {error_snip} |"
        )

    return "\n".join(lines)


def normalize_error(snippet):
    """Normalize an error snippet for deduplication, preserving key details."""
    if not snippet:
        return ""
    s = snippet
    # Replace file paths but keep filename
    s = re.sub(r"/[\w./~-]+/([^/\s\"':]+)", r"<dir>/\1", s)
    # Replace line numbers
    s = re.sub(r"line \d+", "line <N>", s)
    # Replace large numbers
    s = re.sub(r"\b\d{4,}\b", "<N>", s)
    return s[:120]


def deduplicate_chains(chains):
    """Deduplicate near-identical chains into specific patterns.

    Groups by (category, normalized_cmd, normalized_error) so that the same
    error type on different commands/files stays distinct. Only collapses
    truly identical patterns (same command template + same error template).

    Returns list of dicts with added 'dedup_count' field.
    """
    groups = defaultdict(list)
    for chain in chains:
        key = (
            chain["error_category"],
            normalize_command(chain["first_cmd"]),
            normalize_error(chain.get("error_snippet", "")),
        )
        groups[key].append(chain)

    result = []
    for group_chains in groups.values():
        # Pick the chain with the longest error snippet as the most informative
        best = max(group_chains, key=lambda c: len(c.get("error_snippet") or ""))
        best["dedup_count"] = len(group_chains)
        result.append(best)
    return result


def _pattern_label(chain):
    """Generate a short, specific label for a pattern from its command and error."""
    cmd = chain["first_cmd"]
    snippet = chain.get("error_snippet", "")

    # For Edit/Write tools, use the filename
    if cmd.startswith("Edit ") or cmd.startswith("Write "):
        return f"{cmd} — {snippet[:60]}"

    # Extract the script/command name (first meaningful token)
    cmd_name = cmd.split()[0] if cmd else "unknown"
    # If it's python3 running a script, use the script name
    if cmd_name == "python3" and len(cmd.split()) > 1:
        arg = cmd.split()[1]
        if arg.startswith("-"):
            cmd_name = "python3 (inline)"
        elif arg == "<<":
            cmd_name = "python3 (heredoc)"
        else:
            cmd_name = os.path.basename(arg)

    # Extract the key error detail
    error_detail = ""
    if snippet:
        # Find the most specific part of the error
        for line in snippet.split("\n"):
            line = line.strip()
            if line and not line.startswith("Exit code") and not line.startswith("Traceback"):
                error_detail = line[:80]
                break
        if not error_detail:
            error_detail = snippet.split("\n")[0][:80]

    return f"{cmd_name}: {error_detail}" if error_detail else cmd_name


def format_markdown(patterns, meta):
    """Format results as a human-readable markdown report."""
    lines = []
    lines.append("# Fail-Then-Fix Pattern Analysis\n")
    lines.append(f"**Date range:** {meta['cutoff_date']} to {meta['end_date']}")
    lines.append(f"**Files analyzed:** {meta['files_analyzed']}")
    lines.append(f"**Recurring patterns found:** {len(patterns)}")
    lines.append(f"**Total occurrences:** {meta['total_chains']}\n")

    lines.append("| # | Count | Category | Pattern | Failed Command | Fix |")
    lines.append("|---|-------|----------|---------|---------------|-----|")

    for i, p in enumerate(patterns):
        label = p["label"][:60].replace("|", "\\|").replace("\n", " ")
        failed = p["first_cmd"][:60].replace("|", "\\|").replace("\n", " ")
        fixed = p["final_cmd"][:60].replace("|", "\\|").replace("\n", " ")

        lines.append(f"| {i + 1} | {p['count']} | `{p['category']}` | {label} | `{failed}` | `{fixed}` |")

    lines.append(f"\n**Total occurrences:** {meta['total_chains']}")
    return "\n".join(lines)


def build_pattern_data(all_chains):
    """Build specific pattern entries from deduplicated chains.

    Each deduplicated chain with dedup_count >= 1 becomes its own pattern.
    Patterns are sorted by count (most frequent first).
    """
    patterns = []
    for chain in all_chains:
        count = chain.get("dedup_count", 1)
        rule, section = RULE_SUGGESTIONS.get(chain["error_category"], (None, None))

        entry = {
            "category": chain["error_category"],
            "description": chain["error_description"],
            "label": _pattern_label(chain),
            "count": count,
            "avg_chain_length": round(chain["chain_length"], 1),
            "first_cmd": chain["first_cmd"][:200],
            "final_cmd": chain["final_cmd"][:200],
            "error_snippet": (chain.get("error_snippet") or "")[:200],
        }
        if rule:
            entry["suggested_rule"] = rule
            entry["suggested_section"] = section

        patterns.append(entry)

    patterns.sort(key=lambda x: -x["count"])
    return patterns


def main():
    parser = argparse.ArgumentParser(
        description="Analyze Claude Code session logs for recurring fail-then-fix patterns."
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Number of days to look back (default: {DEFAULT_DAYS})")
    parser.add_argument("--min-count", type=int, default=2,
                        help="Minimum occurrence count to report a category (default: 2)")
    parser.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR,
                        help=f"Override projects directory (default: {DEFAULT_PROJECTS_DIR})")
    parser.add_argument("--exclude", default="",
                        help="Comma-separated list of categories to exclude")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output JSON to stdout (default: markdown)")
    parser.add_argument("--list-categories", action="store_true",
                        help="Print available error category names and exit")
    parser.add_argument("--no-wheel-spinning", action="store_true",
                        help="Skip wheel-spinning detection (only report fail-then-fix patterns)")
    args = parser.parse_args()

    if args.list_categories:
        for cat_name, _, desc in ERROR_CATEGORIES:
            print(f"  {cat_name:30s} {desc}")
        print(f"  {TOOL_ERROR_UNKNOWN[0]:30s} {TOOL_ERROR_UNKNOWN[1]}")
        return

    cutoff_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    exclude_set = set(c.strip() for c in args.exclude.split(",") if c.strip())

    # Phase 1: Find files
    print("Phase 1: Finding JSONL files...", file=sys.stderr)
    files = find_jsonl_files(args.projects_dir, cutoff_date)
    total_size = sum(s for s, _, _ in files)
    print(f"  Found {len(files)} files, {total_size / 1048576:.1f} MB total", file=sys.stderr)

    # Phase 2: Extract patterns
    print("\nPhase 2: Extracting fail-then-fix patterns...", file=sys.stderr)
    all_chains = []
    all_wheel_spinning = []

    for idx, (size, mod_date, fpath) in enumerate(files):
        if idx % 100 == 0:
            print(f"  Processing file {idx + 1}/{len(files)}...", file=sys.stderr)

        entries = extract_tool_calls_and_results(fpath)
        if not entries:
            continue

        for chain in find_fail_fix_chains(entries):
            chain["source_file"] = fpath
            all_chains.append(chain)

        if not args.no_wheel_spinning:
            for loop in find_wheel_spinning(entries):
                loop["source_file"] = fpath
                all_wheel_spinning.append(loop)

    print(f"  Found {len(all_chains)} raw chains", file=sys.stderr)
    if not args.no_wheel_spinning:
        print(f"  Found {len(all_wheel_spinning)} raw wheel-spinning loops", file=sys.stderr)

    if not all_chains and not all_wheel_spinning:
        print("\nNo fail-then-fix patterns found.", file=sys.stderr)
        if args.json_output:
            output = {"meta": {"days": args.days, "cutoff_date": cutoff_date,
                               "end_date": datetime.now().strftime("%Y-%m-%d"),
                               "files_analyzed": len(files), "total_chains": 0},
                       "patterns": []}
            if not args.no_wheel_spinning:
                output["wheel_spinning_patterns"] = []
            json.dump(output, sys.stdout, indent=2)
        return

    # Phase 3: Deduplicate
    print("\nPhase 3: Deduplicating...", file=sys.stderr)
    all_chains = deduplicate_chains(all_chains)
    print(f"  {len(all_chains)} unique patterns after deduplication", file=sys.stderr)

    # Phase 4: Build specific patterns and output
    print("\nPhase 4: Building pattern data...", file=sys.stderr)
    patterns = build_pattern_data(all_chains)

    # Phase 5: Wheel-spinning dedup and build
    ws_patterns = []
    if not args.no_wheel_spinning and all_wheel_spinning:
        print("\nPhase 5: Deduplicating wheel-spinning loops...", file=sys.stderr)
        deduped_ws = deduplicate_wheel_spinning(all_wheel_spinning)
        print(f"  {len(deduped_ws)} unique wheel-spinning patterns after deduplication", file=sys.stderr)
        ws_patterns = build_wheel_spinning_data(deduped_ws)

    # Apply filters
    if exclude_set:
        patterns = [p for p in patterns if p["category"] not in exclude_set]
        ws_patterns = [p for p in ws_patterns if p["category"] not in exclude_set]
    patterns = [p for p in patterns if p["count"] >= args.min_count]
    ws_patterns = [p for p in ws_patterns if p["count"] >= args.min_count]

    meta = {
        "days": args.days,
        "cutoff_date": cutoff_date,
        "end_date": datetime.now().strftime("%Y-%m-%d"),
        "files_analyzed": len(files),
        "total_chains": sum(p["count"] for p in patterns),
    }

    if args.json_output:
        output = {"meta": meta, "patterns": patterns}
        if not args.no_wheel_spinning:
            output["wheel_spinning_patterns"] = ws_patterns
        json.dump(output, sys.stdout, indent=2)
        print(file=sys.stdout)  # trailing newline
    else:
        print(format_markdown(patterns, meta))
        if not args.no_wheel_spinning:
            print(format_wheel_spinning_markdown(ws_patterns))

    print("\nDone.", file=sys.stderr)


if __name__ == "__main__":
    main()
