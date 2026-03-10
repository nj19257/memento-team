# Memento Teams

Multi-agent orchestration system that decomposes complex tasks into parallel subtasks, coordinates workers via a shared workboard (tag protocol), and synthesizes results.

## Quick Start

```bash
curl -sSL https://raw.githubusercontent.com/nj19257/memento-team/tag-protocol/install.sh | bash
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
    |  1. Reads orchestrator_skills (task-router, decompose-*, search-strategy)
    |  2. LLM decomposes task into subtasks
    |  3. Creates workboard with tagged slots per worker
    |
    +-- calls MCP tool: execute_subtasks(subtasks=["..."], workboard="...")
                |
                v
        MCP Server (FastMCP, stdio transport)
            |-- Worker 0 (MementoSAgent): search → edit_workboard("t1_result", data)
            |-- Worker 1 (MementoSAgent): search → edit_workboard("t2_result", data)
            +-- Worker N (MementoSAgent): search → edit_workboard("tN_result", data)
                |
                v
        Workers write results to shared .workboard.md via tag protocol
                |
                v
        Orchestrator reads workboard, synthesizes final response
```

## Key Components

| File | Purpose |
|---|---|
| [tui_app.py](tui_app.py) | Textual TUI — task submission, worker monitoring, workboard view, final output |
| [orchestrator/orchestrator_agent.py](orchestrator/orchestrator_agent.py) | LangChain orchestrator — decomposes tasks and dispatches to workers via MCP |
| [orchestrator/mcp_server.py](orchestrator/mcp_server.py) | FastMCP server — `execute_subtasks` tool, worker pool, workboard management, worker timeout |
| [orchestrator_skills/](orchestrator_skills/) | Orchestrator skills — task routing, decomposition strategies, search strategy |
| [Memento-S/core/agent/memento_s_agent.py](Memento-S/core/agent/memento_s_agent.py) | Worker agent — ReAct loop with skill routing and tool execution |
| [Memento-S/core/llm/client.py](Memento-S/core/llm/client.py) | LLM client — litellm-based, supports OpenRouter/OpenAI/Anthropic |
| [Memento-S/core/config/config.py](Memento-S/core/config/config.py) | Centralized configuration from environment variables |
| [Memento-S/core/tools/builtins.py](Memento-S/core/tools/builtins.py) | Worker tools — file ops, web search/fetch, workboard read/edit |

## Workboard (Tag Protocol)

Workers coordinate via a shared `.workboard.md` file using tagged sections:

```markdown
# Task Board
## Subtasks
- [ ] 1 (t1): Search for X
- [ ] 2 (t2): Search for Y
## Worker Slots
### t1
<t1_status></t1_status>
<t1_result></t1_result>
### t2
<t2_status></t2_status>
<t2_result></t2_result>
```

Each worker writes only to its assigned tag via `edit_workboard("tN_result", content)`. A shared `threading.Lock` prevents concurrent read-write corruption.

## Orchestrator Skills

| Skill | Purpose |
|---|---|
| `task-router` | Identifies task type and routes to the correct decompose strategy |
| `decompose-split-by-time-period` | Decomposes tasks organized by chronological range |
| `decompose-split-by-entity` | Decomposes tasks organized by distinct entities |
| `decompose-split-by-category` | Decomposes tasks organized by categories |
| `decompose-split-by-rank-segment` | Decomposes tasks organized by ranking segments |
| `decompose-geographic-registries` | Decomposes tasks involving geographic registry data |
| `search-strategy` | Determines optimal search approach (hybrid/index/keyword) for workers |
| `workboard` | Workboard usage reference |

## Built-in Worker Skills

| Skill | Description |
|---|---|
| `web-search` | Google search via Serper API + URL fetching via crawl4ai |
| `skill-creator` | Dynamically create new skills at runtime |
| `uv-pip-install` | Python package management via uv/pip |
| `pdf` | PDF reading and extraction |
| `docx` | Word document operations |
| `xlsx` | Spreadsheet operations |
| `pptx` | Presentation operations |
| `image-analysis` | Image analysis |
| `mcp-builder` | MCP server builder |

Workers automatically select the best skill via semantic routing (`route_skill()`).

## How It Works

1. **User** submits a task via the TUI
2. **Orchestrator** reads skills: `list_orchestrator_skills()` → `read_orchestrator_skill("task-router")` → decompose strategy → search strategy
3. **Orchestrator** decomposes the task into subtasks and creates a workboard with tagged slots
4. **Orchestrator** calls `execute_subtasks(subtasks=[...], workboard="...")`
5. **MCP server** initializes the workboard, then runs each subtask through a MementoSAgent worker (up to 10 in parallel):
   - Worker reads workboard for context
   - `route_skill()` → selects the best skill
   - Executes search/fetch/analysis
   - Writes results to workboard via `edit_workboard("tN_result", data)`
6. **MCP server** returns worker results (with 300s per-worker timeout)
7. **Orchestrator** reads the workboard, concatenates all worker results, and synthesizes a final response

## Setup

### One-Click Install

```bash
curl -sSL https://raw.githubusercontent.com/nj19257/memento-team/tag-protocol/install.sh | bash
```

The installer will:
- Install `uv` (if not present)
- Clone the repo (branch `tag-protocol`)
- Install all dependencies (`Memento-S` + orchestrator)
- Download router assets
- Configure `.env` interactively (API keys)
- Create the `memento-teams` command

### Manual Setup

Prerequisites: Python 3.12+, `uv`, git

```bash
git clone --branch tag-protocol https://github.com/nj19257/memento-team.git
cd memento-team

# Install Memento-S worker dependencies
cd Memento-S && uv sync --python 3.12 && cd ..

# Install orchestrator dependencies
uv sync --python 3.12
```

Create a `.env` file in the project root:

```env
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=google/gemini-3-flash-preview
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
SERPAPI_API_KEY=...
WORKER_TIMEOUT=360
```

### Run

```bash
memento-teams
```

Or directly:

```bash
uv run python tui_app.py
```

## TUI

- Submit tasks (`Ctrl+Enter` or **Run Task**)
- Select orchestrator and worker models from dropdown
- Set number of parallel workers (default 5, max 10)
- Live worker status table (click to inspect execution steps)
- Real-time workboard view
- Orchestrator workflow / trajectory tracking
- Final output panel with copy support (`c`)
- Load example tasks from dropdown

Controls:

| Key | Action |
|---|---|
| `Ctrl+Enter` | Run task |
| `c` | Copy final output |
| `r` | Refresh worker list |
| `q` | Quit |

## Configuration

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | — | API key for LLM calls (required) |
| `OPENROUTER_MODEL` | `google/gemini-3-flash-preview` | Model for workers |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | LLM API base URL |
| `SERPAPI_API_KEY` | — | API key for web search (Serper) |
| `FIRECRAWL_API_KEY` | — | API key for Firecrawl (optional) |
| `WORKER_TIMEOUT` | `300` | Per-worker timeout in seconds |
| `MAX_WORKERS` | `10` | Maximum parallel workers |

## Project Structure

```
memento-team/
├── install.sh                          # One-click installer
├── pyproject.toml                      # Root project (orchestrator deps + entry point)
├── tui_app.py                          # Textual TUI
├── main.py                             # Standalone entry point (non-TUI)
├── .env                                # API keys and configuration
├── orchestrator/
│   ├── orchestrator_agent.py           # LangChain orchestrator agent
│   └── mcp_server.py                   # FastMCP server (worker pool + workboard)
├── orchestrator_skills/                # Orchestrator-level skills
│   ├── task-router/
│   ├── decompose-split-by-time-period/
│   ├── decompose-split-by-entity/
│   ├── decompose-split-by-category/
│   ├── decompose-split-by-rank-segment/
│   ├── decompose-geographic-registries/
│   ├── search-strategy/
│   └── workboard/
├── Memento-S/                          # Worker agent system
│   ├── pyproject.toml                  # Worker dependencies
│   ├── core/
│   │   ├── agent/                      # MementoSAgent (ReAct loop)
│   │   ├── llm/                        # LLM client (litellm)
│   │   ├── config/                     # Settings from env vars
│   │   ├── tools/                      # Built-in tools (file, web, workboard)
│   │   ├── skills/                     # Skill router and manager
│   │   └── evolve/                     # Skill evolution system
│   ├── builtin/skills/                 # Built-in skill definitions
│   │   ├── web-search/
│   │   ├── skill-creator/
│   │   ├── uv-pip-install/
│   │   ├── pdf/
│   │   ├── docx/
│   │   ├── xlsx/
│   │   ├── pptx/
│   │   ├── image-analysis/
│   │   └── mcp-builder/
│   └── workspace/                      # Worker workspace (.workboard.md lives here)
├── eval/                               # Evaluation framework
├── logs/                               # Worker trajectories and orchestrator logs
└── docs/                               # Documentation
```
