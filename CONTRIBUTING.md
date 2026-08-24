# Contributing to team-360-cli

Thanks for your interest in contributing. This document covers the basics.

## Getting Started

1. Fork the repository
2. Clone your fork and install dependencies:

```bash
git clone https://github.com/your-user/team-360-cli.git
cd team-360-cli
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Set up env vars with `python main.py --setup`
4. Run a test report to verify your setup: `python main.py --test`

## Project Structure

```
team-360-cli/
  main.py              # CLI entry point and orchestration
  config.py            # Team configuration parser (markdown + Confluence)
  heuristics.py        # Analysis engine (stale detection, workload, risks)
  report.py            # Markdown report generator
  setup.py             # Interactive env var setup wizard
  teams.json           # Team definitions (Confluence page references)
  collectors/
    jira.py            # JIRA board, backlog, bugs, strats, epics
    github.py          # GitHub PR collection (via gh CLI)
    gitlab.py          # GitLab MR collection (via glab CLI)
    calendar.py        # Google Calendar PTO/absence collection
  publishers/
    confluence.py      # Confluence REST API publisher
    vault.py           # Obsidian vault file writer
```

## How to Contribute

### Reporting Bugs

Open an issue with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Relevant logs (`--verbose` output)

### Suggesting Features

Open an issue describing the use case. Focus on the problem you're solving, not just the solution.

### Submitting Changes

1. Create a branch from `main`:

```bash
git checkout -b feature/your-feature
```

2. Make your changes. Follow the conventions below.
3. Test with `--test` mode to verify reports generate correctly.
4. Commit with a clear message:

```bash
git commit -m "Add support for custom report templates"
```

5. Push and open a Pull Request against `main`.

## Conventions

- **Python 3.10+** — use type hints, f-strings, `Path` over `os.path`
- **No unnecessary dependencies** — if the stdlib or `requests` can do it, don't add a package
- **Credentials from env vars only** — never hardcode tokens, URLs, or secrets
- **No comments unless the "why" is non-obvious** — let clear naming do the work
- **Test with `--test` mode** before submitting — verify the report generates without errors

## Adding a New Collector

1. Create `collectors/your_source.py` with a `collect_*` function that returns structured data
2. Import and call it in `main.py` (add to the parallel collection block)
3. Handle the data in `heuristics.py` if analysis is needed
4. Add the relevant section in `report.py`

## Adding a New Publisher

1. Create `publishers/your_target.py` with a publish function
2. Call it from `main.py` after report generation
3. Add any required env vars to `setup.py`'s `ENV_VARS` list

## Code of Conduct

Be respectful. Be constructive. Focus on the work. We're here to build useful tools, not to argue.

## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.
