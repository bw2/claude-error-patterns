---
name: error-patterns
description: >
  Analyze Claude Code session logs to find recurring fail-then-fix patterns
  and propose CLAUDE.md rules to prevent them. Use when the user says
  "analyze errors", "find error patterns", "the same errors over and over",
  "what errors does Claude keep making", "what errors do you keep making",
  "what errors do I keep making", "what mistakes does Claude keep making",
  "what mistakes do you keep making", "what mistakes do I keep making",
  or wants to improve CLAUDE.md based on session history.
---

# Error Patterns Skill

Scan Claude Code session logs, find recurring fail-then-fix patterns, and help the user add preventive rules to CLAUDE.md.

## Workflow

Execute these 3 steps in order:

### Step 1: Scan

Run the analysis script:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/error_patterns_cli.py --json --days 14
```

Adjust `--days` if the user specifies a different time range. Capture stdout (JSON) for parsing in Step 2.

If the user wants to exclude noisy categories (like `generic_exit_code` or `generic_error`), add `--exclude generic_exit_code,generic_error`.

**Project filtering:** If the user provides keywords after `/error-patterns` (e.g. `/error-patterns code assistant`), scope the analysis to a specific project:

1. List directories under `~/.claude/projects/`
2. Find the one(s) whose name contains ALL the given keywords (case-insensitive). The directory names use `-` as separators (e.g. `-Users-<username>-code-assistant`).
3. If exactly one match, add `--projects-dir ~/.claude/projects/<match>/` to the command.
4. If multiple matches, show them to the user and ask which one to use.
5. If no match, tell the user and fall back to scanning all projects.

### Step 2: Present Results

Parse the JSON output. Present two sections:

**A. Fail-then-fix patterns** (from the `patterns` array): recurring mistakes that Claude corrected.

**B. Wheel-spinning patterns** (from the `wheel_spinning_patterns` array): cases where Claude retried the same failing command 3+ times. Show the command pattern, average loop length, and whether it was eventually resolved.

For each section, show a numbered list (count >= 2):

```
Fail-then-fix patterns in the last 14 days (N files analyzed):

 #  | Count | Pattern                                              | Category
----|-------|------------------------------------------------------|------------------
 1  |   4   | Edit CLAUDE_HISTORY... — file modified since read     | tool_error_unknown
 2  |   4   | curl → AttributeError: 'NoneType'...                 | python_attribute
...

Wheel-spinning patterns (repeated failing attempts):

 #  | Count | Avg Length | Resolved? | Command Pattern
----|-------|-----------|-----------|----------------
 1  |   2   | 5 attempts | 50% yes  | gzcat cacna1...
...
```

Then ask the user:

> Which pattern(s) would you like to add as CLAUDE.md rules? Enter numbers (e.g. "1,3,5"), "all", or "none".

### Step 3: Apply Rules to CLAUDE.md

For each selected pattern:

1. **Check for suggested rule**: The JSON output includes `suggested_rule` and `suggested_section` fields for well-known patterns. Use these when available.

2. **Generate rule for unknown patterns**: If no `suggested_rule` exists, derive one from the fail-then-fix example. Format: "When doing X, use Y instead of Z" or "Before doing X, always do Y first".

3. **Check for duplicates**: Read `~/.claude/CLAUDE.md` and search for key terms from the proposed rule. If a similar rule already exists, tell the user and skip it.

4. **Insert the rule**: Find the appropriate section in CLAUDE.md (using `suggested_section` or inferring from context). Add the rule as a new bullet point (`- ...`) in that section.

5. **Confirm**: Show the user what was added and where.

## Notes

- Default lookback is 14 days. Users can ask for longer ranges (e.g., "last month" → `--days 30`).
- The script uses only Python stdlib (no pip install needed).
- Progress messages go to stderr; structured output goes to stdout.
- To see all available error categories: `python3 ${CLAUDE_SKILL_DIR}/scripts/error_patterns_cli.py --list-categories`
