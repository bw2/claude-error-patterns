# claude-error-patterns

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin that analyzes your session logs to find recurring mistakes and proposes CLAUDE.md rules to prevent them.

## What it does

- **Fail-then-fix detection**: Finds patterns where Claude makes an error then corrects itself (e.g., using `python` instead of `python3`, editing a file before reading it)
- **Wheel-spinning detection**: Finds cases where Claude retries the same failing command 3+ times without progress
- **Rule suggestions**: Proposes CLAUDE.md rules for well-known error categories and helps you insert them

The script scans your JSONL session logs in `~/.claude/projects/`, classifies errors into 37+ categories, detects fail-then-fix chains and wheel-spinning loops, deduplicates similar patterns, and reports them sorted by frequency.

## Install

```
/plugin install bw2/claude-error-patterns
```

## Usage

Invoke with `/claude-error-patterns:error-patterns` or just ask Claude to "analyze my error patterns" or "what mistakes do you keep making".

You can also run the script directly:

```bash
# Human-readable markdown
python3 scripts/error_patterns_cli.py --days 14

# JSON output
python3 scripts/error_patterns_cli.py --json --days 14

# Filter options
python3 scripts/error_patterns_cli.py --min-count 5
python3 scripts/error_patterns_cli.py --exclude generic_exit_code,generic_error

# Skip wheel-spinning analysis
python3 scripts/error_patterns_cli.py --no-wheel-spinning

# List available error categories
python3 scripts/error_patterns_cli.py --list-categories
```

## Error categories

Includes 37+ built-in categories covering:

- Tool errors (edit before read, ambiguous match, safety net blocked)
- Command not found (python, pip, zcat on macOS)
- Python exceptions (SyntaxError, ModuleNotFoundError, TypeError, etc.)
- File system errors (no such file, permission denied)
- Shell/grep/awk syntax issues
- Git, cloud/API, build, and test errors

## Requirements

- Python 3 (stdlib only, no dependencies)
