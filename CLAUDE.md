# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Memento Team is a multi-agent orchestration system that decomposes complex tasks into parallel subtasks, executing them using skill-based Memento-S worker agents. The orchestrator uses LangChain; workers use an ops-based architecture over MCP (Model Context Protocol).

## Running the System

```bash
# Install orchestrator dependencies
pip install langchain langchain-openai langchain-mcp-adapters fastmcp

# Install worker dependencies
pip install -r Memento-S_old/requirements.txt

# Run (requires .env with OPENROUTER_API_KEY)
python main.py
```

There is no formal test suite. The only test is `Test/system_test.ipynb` (end-to-end Jupyter notebook).

## Known Issue: Directory Naming

The code (`main.py`, `orchestrator/mcp_server.py`) references `Memento-S/` but the actual directory is `Memento-S_old/`. The README also references `Memento-S/`. If the system fails to import worker modules, this path mismatch is likely the cause.

## Architecture

### Two-Layer Agent System

1. **Orchestrator** (`orchestrator/orchestrator_agent.py`): LangChain agent that decomposes user tasks into subtasks and dispatches them via MCP. Uses `create_agent()` with a `ChatOpenAI` model pointed at OpenRouter.

2. **MCP Server** (`orchestrator/mcp_server.py`): FastMCP server over stdio transport exposing `execute_subtasks()`. Runs up to 5 workers in parallel via `asyncio.Semaphore`. Manages the shared workboard. Redirects all print/logging to stderr to preserve the MCP JSON-RPC protocol on stdout.

3. **Memento-S Workers** (`Memento-S_old/`): Skill-based agents using an ops architecture (not OpenAI function calling). The LLM generates JSON ops, a bridge executor routes them by type.

### Ops-Based Execution (Not Tool Calling)

Workers do **not** use standard tool/function calling. Instead:
- `planning.py` builds a system prompt listing available op types
- LLM returns `{"ops": [{"type": "read_file", "path": "..."}]}`
- `skill_executor.py` routes each op to the appropriate handler
- Results feed back to the LLM for multi-round execution

### Skill Routing

`core/router.py` performs two-stage routing:
1. Semantic pre-filter (BM25) narrows candidates
2. LLM selects the best skill from candidates

Built-in skills: `filesystem`, `terminal`, `web-search`, `uv-pip-install`, `skill-creator`. Skills are defined as `SKILL.md` markdown files in `Memento-S_old/skills/<name>/SKILL.md`.

### Workboard Coordination

Parallel workers coordinate via a shared markdown file at `workspace/.workboard.md`:
- Thread-safe via `threading.Lock` in `core/workboard.py`
- Orchestrator optionally creates it via `execute_subtasks(workboard="...")`
- Workers discover it autonomously through `read_workboard`/`edit_workboard` ops
- `skill_executor.py` pre-extracts workboard ops from any skill's plan before dispatching to the skill-specific executor

### Execution Flow

```
User → main.py → OrchestratorAgent.run(query)
  → LLM decomposes into subtasks
  → execute_subtasks(subtasks, workboard) via MCP
  → orchestrator/mcp_server.py writes workboard, spawns workers
  → Each worker: route_skill() → run_one_skill_loop()
    → loads SKILL.md → LLM generates ops → bridge executes → loops
  → Aggregated results → Orchestrator synthesizes final response
```

## Key Files

| File | Role |
|---|---|
| `main.py` | Entry point; initializes LLM, cleans up workboard, runs orchestrator |
| `orchestrator/orchestrator_agent.py` | LangChain orchestrator with task decomposition prompt |
| `orchestrator/mcp_server.py` | MCP server with workboard support (used by orchestrator) |
| `Memento-S_old/mcp_server.py` | Standalone MCP server (no workboard param) |
| `Memento-S_old/agent.py` | Worker facade re-exporting all core modules |
| `Memento-S_old/core/config.py` | All env vars, constants, op-type sets |
| `Memento-S_old/core/llm.py` | LLM client with retry logic |
| `Memento-S_old/core/router.py` | Semantic + LLM skill routing |
| `Memento-S_old/core/workboard.py` | Thread-safe shared workboard |
| `Memento-S_old/core/skill_engine/planning.py` | LLM system prompt with available op types |
| `Memento-S_old/core/skill_engine/execution.py` | Multi-round skill loop |
| `Memento-S_old/core/skill_engine/skill_executor.py` | Bridge op dispatch + workboard pre-extraction |

## Configuration

All config is via environment variables in `.env` (loaded by `python-dotenv`):

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | API key for all LLM calls |
| `OPENROUTER_MODEL` | No | Model ID (default: `anthropic/claude-3.5-sonnet`) |
| `OPENROUTER_BASE_URL` | No | API base URL (default: OpenRouter) |
| `SERPAPI_API_KEY` | No | For web-search skill |
| `DEBUG` | No | Enable debug logging |

## Logging

Worker execution trajectories are saved as JSONL files in `/logs/`. All logging uses stderr to avoid corrupting the MCP stdio protocol.
