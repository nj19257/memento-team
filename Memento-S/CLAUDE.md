# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Memento-S is a self-evolving skills runner — an intelligent agent CLI built in Python that enables multi-step workflow execution through composable "skills." Skills are self-contained packages that provide specialized knowledge and workflows, and can self-optimize based on task failures.

## Commands

```bash
# Install dependencies
uv sync --python 3.12

# Run the CLI (interactive REPL)
memento
# or
uv run python -m cli

# Single-turn mode
python -m cli "your prompt here"

# Run all contract tests
uv run pytest tests/

# Run a single test file
uv run pytest tests/contract/test_public_api_contract.py

# Run a specific test
uv run pytest tests/contract/test_public_api_contract.py::TestClassName::test_method -v
```

## Architecture

### Execution Flow

```
User Input (CLI) → Router (skill selection) → Plan Generation (LLM) → Bridge Dispatch (tool execution) → Output/Summarization → Response
```

The system uses a **plan → execute → feedback** loop: the LLM generates a JSON plan with `tool_calls`, the bridge dispatcher executes them, and failures can trigger auto-retry or skill self-optimization via the `skill-creator` skill.

### Key Modules

- **`cli/main.py`** — Main REPL loop, slash-command parsing, session history management
- **`cli/workflow_runner.py`** — Multi-step workflow orchestration (chains of skill invocations)
- **`core/config.py`** — All environment/config constants, loaded from `.env` via dotenv
- **`core/llm.py`** — LLM transport (OpenRouter/Anthropic/OpenAI) with retry logic
- **`core/router.py`** — Skill routing via semantic selection (BM25, TF-IDF, or embedding-based)
- **`core/skill_engine/`** — Planning, execution, bridge dispatch, catalog management
- **`agent.py`** — Re-export facade for stable public imports (do not add logic here)

### Bridge Dispatcher

`core/skill_engine/bridge/dispatcher.py` routes tool calls to execution backends:
- **Filesystem** — read_file, write_file, edit_file, etc.
- **Terminal** — shell command execution with safety checks
- **Web** — web_search, fetch_url
- **UV/Pip** — Python package management
- **Skill Meta** — dynamic skill creation/updates

### Skill System

Skills live in `skills/<skill-name>/` and must contain a `SKILL.md` with YAML frontmatter (`name`, `description`) plus markdown instructions. Skills can bundle `scripts/`, `references/`, and `assets/` directories. The five base skills (always available): `filesystem`, `terminal`, `web-search`, `uv-pip-install`, `skill-creator`.

### Facade Pattern

`agent.py` and `core/skill_engine/skill_runner.py` are facades providing stable import paths. When refactoring, add new logic in focused submodules — not the facades. All public symbols should remain importable from `agent.py` to avoid breaking downstream code.

## Configuration

Environment variables are parsed in `core/config.py` from a `.env` file. Key settings:
- `LLM_API` — Provider selection (`openrouter`, `anthropic`, `openai`)
- `SEMANTIC_ROUTER_METHOD` — Routing strategy (`bm25`, `tfidf`, `qwen`, `memento_qwen`)
- `CLI_CREATE_ON_MISS` — Auto-create skills when no match found
- `DEBUG` — Verbose logging

## Conventions

- **Skills**: kebab-case names (`web-search`, `skill-creator`)
- **Modules**: snake_case filenames
- **Imports**: Keep CLI-facing imports stable; use `from agent import ...` as the public API
- **Logging**: Use `core.utils.logging_utils.log_event()` for diagnostics, not print statements from core modules
- **Python**: Requires >=3.12; uses `from __future__ import annotations` throughout
