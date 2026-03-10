# Memento Teams

Multi-agent orchestration system that decomposes complex tasks into parallel subtasks and executes them using skill-based worker agents.

## Quick Start

```bash
curl -sSL https://raw.githubusercontent.com/nj19257/memento-team/demo/install.sh | bash
```

Then launch the TUI:

```bash
memento-teams
```

## Architecture

```
User Query (TUI)
    |
    v
Orchestrator Agent (LangChain)
    |  LLM decomposes task into subtasks
    |
    +-- calls MCP tool: execute_subtasks(["subtask1", "subtask2", ...])
                |
                v
        Memento-S MCP Server (FastMCP, stdio transport)
            |-- Worker 0: route_skill() -> run_one_skill_loop()
            |-- Worker 1: route_skill() -> run_one_skill_loop()
            +-- Worker N: route_skill() -> run_one_skill_loop()
                |
                v
        Aggregated results returned to orchestrator
                |
                v
        Orchestrator synthesizes final response
```

## Key Components

| File | Purpose |
|---|---|
| [tui_app.py](tui_app.py) | Textual TUI — primary interface for submitting tasks and inspecting workers |
| [orchestrator/orchestrator_agent.py](orchestrator/orchestrator_agent.py) | LangChain-based orchestrator that decomposes tasks and dispatches to workers via MCP |
| [Memento-S/mcp_server.py](Memento-S/mcp_server.py) | FastMCP server exposing `execute_subtasks` tool — runs up to 5 workers in parallel |
| [Memento-S/agent.py](Memento-S/agent.py) | Worker agent facade — re-exports all core modules |
| [Memento-S/core/config.py](Memento-S/core/config.py) | Centralized configuration from environment variables |
| [Memento-S/core/router.py](Memento-S/core/router.py) | Skill routing — semantic pre-filter + LLM-based skill selection |
| [Memento-S/core/skill_engine/](Memento-S/core/skill_engine/) | Skill planning, execution, catalog management, dynamic fetch |

## Built-in Skills

| Skill | Description |
|---|---|
| `filesystem` | Read, write, edit, search, and manage files and directories |
| `terminal` | Execute shell commands with safety checks |
| `web-search` | Google search via SerpAPI + URL fetching |
| `uv-pip-install` | Python package management via uv/pip |
| `skill-creator` | Dynamically create new skills at runtime |

Workers automatically select the best skill for each subtask via semantic routing. If no existing skill matches, the system can dynamically fetch or create new skills on demand.

## How It Works

1. **User** submits a task via the TUI
2. **Orchestrator** LLM decomposes it into self-contained subtasks
3. **Orchestrator** calls `execute_subtasks()` on the MCP server
4. **MCP server** runs each subtask through a Memento-S worker:
   - `route_skill()` — semantic pre-filter (BM25/embeddings) + LLM picks the best skill
   - `run_one_skill_loop()` — loads `SKILL.md`, generates a JSON operation plan, executes bridge ops, loops until done
5. **MCP server** returns aggregated results
6. **Orchestrator** synthesizes worker results into a final response

## Setup

### One-Click Install

```bash
curl -sSL https://raw.githubusercontent.com/nj19257/memento-team/demo/install.sh | bash
```

The installer will:
- Install `uv` (if not present)
- Clone the repo (branch `demo`)
- Install all dependencies (`Memento-S` + orchestrator)
- Download router assets
- Configure `.env` interactively (API keys)
- Create the `memento-teams` command

### Manual Setup

Prerequisites: Python 3.12+, `uv`, git

```bash
git clone --branch demo https://github.com/nj19257/memento-team.git
cd memento-team

# Install Memento-S worker dependencies
cd Memento-S && uv sync --python 3.12 && cd ..

# Install orchestrator dependencies
uv sync --python 3.12
```

Create a `.env` file in the project root:

```env
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=anthropic/claude-sonnet-4-5
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
SERPAPI_API_KEY=...
```

### Run

```bash
memento-teams
```

Or directly:

```bash
uv run --project Memento-S --with textual --with rich python -c "import sys; sys.path.insert(0, '.'); from tui_app import MementoTeams; MementoTeams().run()"
```

## TUI
```bash
uv run python tui_app.py
```
- Submit tasks directly from the interface (`Ctrl+Enter` or **Run Task**)
- Session-scoped worker list from `logs/worker-*.jsonl` (current task only)
- Per-worker status label (`live` / `finished`)
- Click any worker row to inspect execution steps/events
- Live workboard view from `Memento-S/workspace/.workboard.md` (or `WORKSPACE_DIR`)
- Workboard history is preserved per session as `.workboard-<session_id>.md`
- Final orchestrator output panel

Controls:

- `Ctrl+Enter`: Run task
- `r`: Refresh worker list
- `q`: Quit

## Configuration

All configuration is centralized in [Memento-S/core/config.py](Memento-S/core/config.py) and read from environment variables. Key settings:

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | — | API key for LLM calls (required) |
| `OPENROUTER_MODEL` | `anthropic/claude-sonnet-4-5` | Model for Memento-S workers |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | LLM API base URL |
| `SERPAPI_API_KEY` | — | API key for web search skill |
| `SEMANTIC_ROUTER_ENABLED` | `true` | Enable semantic skill pre-filtering |
| `SEMANTIC_ROUTER_TOP_K` | `4` | Number of candidate skills for LLM routing |
| `SKILL_DYNAMIC_FETCH_ENABLED` | `true` | Auto-fetch missing skills from catalog |
| `DEBUG` | `false` | Enable debug logging |
| `WORKSPACE_DIR` | `Memento-S/workspace` | Workboard location shown in TUI |

## Project Structure

```
memento-team/
├── install.sh                          # One-click installer
├── pyproject.toml                      # Root project (orchestrator deps + entry point)
├── tui_app.py                          # Textual TUI
├── main.py                             # Standalone entry point (non-TUI)
├── orchestrator/
│   └── orchestrator_agent.py           # LangChain orchestrator agent
├── Memento-S/
│   ├── pyproject.toml                  # Worker dependencies
│   ├── mcp_server.py                   # FastMCP server (execute_subtasks)
│   ├── agent.py                        # Worker facade
│   ├── core/
│   │   ├── config.py                   # Configuration & constants
│   │   ├── router.py                   # Skill routing logic
│   │   ├── utils/                      # JSON, path, logging utilities
│   │   └── skill_engine/               # Skill planning, execution, catalog
│   ├── skills/                         # Built-in skills
│   │   ├── filesystem/
│   │   ├── terminal/
│   │   ├── web-search/
│   │   ├── uv-pip-install/
│   │   └── skill-creator/
│   └── cli/                            # CLI REPL with slash commands
└── multiagent-workflow.md              # Detailed architecture notes
```
