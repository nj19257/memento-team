# Memento Team

Multi-agent orchestration system that decomposes complex tasks into parallel subtasks and executes them using skill-based worker agents.

## Architecture

```
User Query
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
| `main.py` | Entry point — initializes LLM, starts orchestrator, runs interactive loop |
| `orchestrator/orchestrator_agent.py` | LangChain-based orchestrator that decomposes tasks and dispatches to workers via MCP |
| `Memento-S/mcp_server.py` | FastMCP server exposing `execute_subtasks` tool — runs up to 5 workers in parallel |
| `Memento-S/agent.py` | Worker agent facade — re-exports all core modules, provides CLI REPL |
| `Memento-S/core/config.py` | Centralized configuration from environment variables |
| `Memento-S/core/llm.py` | LLM client (OpenRouter / Anthropic-compatible endpoints) |
| `Memento-S/core/router.py` | Skill routing — semantic pre-filter + LLM-based skill selection |
| `Memento-S/core/skill_engine/` | Skill planning, execution, catalog management, dynamic fetch |

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

1. **User** submits a task via `main.py`
2. **Orchestrator** LLM decomposes it into self-contained subtasks
3. **Orchestrator** calls `execute_subtasks()` on the MCP server
4. **MCP server** runs each subtask through a Memento-S worker:
   - `route_skill()` — semantic pre-filter (BM25/embeddings) + LLM picks the best skill
   - `run_one_skill_loop()` — loads `SKILL.md`, generates a JSON operation plan, executes bridge ops, loops until done
5. **MCP server** returns aggregated results
6. **Orchestrator** synthesizes worker results into a final response

## Setup

### Prerequisites

- Python 3.11+
- API keys for LLM provider and (optionally) SerpAPI

### Install Dependencies

```bash
# Orchestrator dependencies
pip install langchain langchain-openai langchain-mcp-adapters fastmcp

# Memento-S worker dependencies
pip install -r Memento-S/requirements.txt
```

### Environment Variables

Create a `.env` file in the project root:

```env
# Required — LLM for orchestrator (via OpenRouter)
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=anthropic/claude-sonnet-4-5    # or any OpenRouter model
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1

# Optional — web search
SERPAPI_API_KEY=...

# Optional — debugging
DEBUG=false
```

### Run

```bash
python main.py
```

Enter a task at the prompt. The orchestrator will decompose it and dispatch to workers automatically.

## Configuration

All configuration is centralized in `Memento-S/core/config.py` and read from environment variables. Key settings:

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | — | API key for LLM calls (required) |
| `OPENROUTER_MODEL` | `anthropic/claude-3.5-sonnet` | Model for Memento-S workers |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | LLM API base URL |
| `SERPAPI_API_KEY` | — | API key for web search skill |
| `SEMANTIC_ROUTER_ENABLED` | `true` | Enable semantic skill pre-filtering |
| `SEMANTIC_ROUTER_TOP_K` | `4` | Number of candidate skills for LLM routing |
| `SKILL_DYNAMIC_FETCH_ENABLED` | `true` | Auto-fetch missing skills from catalog |
| `DEBUG` | `false` | Enable debug logging |

## Project Structure

```
memento-team/
├── main.py                          # Entry point
├── orchestrator/
│   └── orchestrator_agent.py        # LangChain orchestrator agent
├── Memento-S/
│   ├── mcp_server.py                # FastMCP server (execute_subtasks)
│   ├── agent.py                     # Worker facade + CLI REPL
│   ├── core/
│   │   ├── config.py                # Configuration & constants
│   │   ├── llm.py                   # LLM client
│   │   ├── router.py                # Skill routing logic
│   │   ├── utils/                   # JSON, path, logging utilities
│   │   └── skill_engine/            # Skill planning, execution, catalog
│   ├── skills/                      # Built-in skills
│   │   ├── filesystem/
│   │   ├── terminal/
│   │   ├── web-search/
│   │   ├── uv-pip-install/
│   │   └── skill-creator/
│   └── cli/                         # CLI REPL with slash commands
└── multiagent-workflow.md           # Detailed architecture notes
```
