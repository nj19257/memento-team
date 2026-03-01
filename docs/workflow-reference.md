# Detailed 3-Phase Workflow Reference (for Debugging)

## Context

This document traces the exact execution flow of the Memento Team orchestrator through all 3 phases, with code paths, data shapes, and what to look for when debugging.

---

## Phase 1: PLAN — Orchestrator Decomposes Task

**Entry:** `orchestrator/orchestrator_agent.py` — `run(query)`

### Step-by-step

```
1. Messages initialized:
   messages = [SystemMessage(system_prompt), HumanMessage(query)]

2. while True loop begins:
   turn += 1
   response = await self.model.bind_tools(self._tools).ainvoke(messages)
   messages.append(response)

3. Three branches are checked IN ORDER:

   A) response.tool_calls?
      → Orchestrator LLM called read_files or run_command
      → Executes each tool, appends ToolMessage, loops back to step 2
      → This can repeat multiple times before any plan

   B) _extract_plan(text) succeeds?
      → Plan found → enters Phase 2
      → Plan shape: {"subtasks": ["...", "..."], "workboard": "# Board\n..."}

   C) Neither?
      → Final answer → return immediately (skips Phase 2/3 entirely)
```

### What the orchestrator LLM sees

The system prompt (`_build_default_system_message()`) tells the LLM to output a JSON code block:
```json
{
  "subtasks": [
    "Create file `project/main.py` with ...",
    "Create file `project/utils.py` with ..."
  ],
  "workboard": "# Task Board\n## Subtasks\n- [ ] 1: main.py\n- [ ] 2: utils.py\n## Shared Context\n..."
}
```

### Plan extraction (`_extract_plan`)

Two regex strategies tried in order:
1. Fenced code block: `` ```json { "subtasks": [...] } ``` ``
2. Bare JSON with `"subtasks"` key

Returns `None` if no valid plan found → triggers branch C (final answer).

### Debugging Phase 1

- **Trajectory event**: `llm_turn_start` (each LLM call), `orch_tool_call`/`orch_tool_result` (if tools used), `plan_extracted` (when plan found)
- **Log file**: `logs/orchestrator-*.jsonl`
- **Common issue**: LLM doesn't output valid JSON → `_extract_plan` returns None → goes straight to final answer without workers
- **Common issue**: LLM calls tools many times before planning → long Phase 1

---

## Phase 2: EXECUTE + REVIEW — Workers Run in Parallel

### Step 2a: Worker Launch (`_start_workers`)

```python
# If workboard content provided, write it to disk:
if workboard.strip():
    write_board_sync(workboard)        # → workspace/.workboard.md

# Create one asyncio task per subtask:
for idx, subtask in enumerate(subtasks):
    task = asyncio.create_task(
        self._run_single_worker(idx, subtask, job)
    )
    job.tasks.append(task)
```

**Data structure** — `WorkerJob`:
```python
job.subtasks = ["subtask 0 text", "subtask 1 text", ...]
job.tasks    = [asyncio.Task, asyncio.Task, ...]       # parallel handles
job.results  = {}   # will be: {0: "result text", 1: "result text"}
job.errors   = {}   # will be: {0: "error text"} if worker crashes
```

### Step 2b: Inside Each Worker (`_run_single_worker`)

```
1. set_worker_context(worker_idx=idx)     # ContextVar, tags edit requests
2. worker_model = build_chat_model()      # fresh LLM from env vars
3. agent = MCPAgent(model, base_dir, extra_servers=[(wb_mcp, wb_configure)])
4. await agent.start()                    # connects to core + workboard MCP servers

5. result = await agent.run(subtask)      # LangChain agent loop:
   │
   │  Inside agent.run() (mcp_agent.py):
   │  create_agent graph does:
   │    LLM → picks tool → call tool → get result → LLM → picks tool → ... → final text
   │
   │  Available tools (merged from both MCP servers):
   │    Core:      bash_tool, str_replace, file_create, view,
   │               list_local_skills, read_skill, search_cloud_skills
   │    Workboard: read_board, write_board, edit_board, append_board, cleanup_board
   │
   │  recursion_limit = 150 (max tool-call rounds)
   │
   └→ Returns: {"messages": [HumanMessage, AIMessage, ToolMessage, AIMessage, ...]}

6. Extract result:
   - Takes LAST message's .content from result["messages"]
   - Stores as string in job.set_result(idx, result_preview)
   - On exception: job.set_error(idx, error_string)

7. Finally block:
   - Saves trajectory to logs/worker-{idx}-{timestamp}.jsonl
```

### Step 2c: Review Loop (`_review_loop`)

Runs **concurrently** with workers (orchestrator awaits this while workers run).

```
while not job.all_done():
    pending = await get_pending_requests()   # snapshot of unresolved requests

    for request in pending:
        │
        │  1. Format review prompt:
        │     "Worker {idx} requests a workboard edit:
        │      - Old text: `...`
        │      - New text: `...`
        │      - Reason: ...
        │      Current workboard: ```...```"
        │
        │  2. LLM reviews with structured output:
        │     review_model = self.model.with_structured_output(ReviewDecision)
        │     decision = await review_model.ainvoke(review_messages)
        │     → Returns: ReviewDecision(status="success"|"failure", feedback="...")
        │
        │  3. Resolve request:
        │     await resolve_request(request, approved=..., feedback=...)
        │       │
        │       ├─ If approved + edit type:
        │       │    Read board → replace old_text with new_text → write board
        │       │    (If old_text NOT found → return failure, don't silently succeed)
        │       │
        │       ├─ If approved + append type:
        │       │    Read board → append text → write board
        │       │
        │       ├─ If rejected:
        │       │    result = {"status": "failure", "feedback": "..."}
        │       │
        │       └─ request.event.set()   ← UNBLOCKS the waiting worker
        │
        └─ Worker receives JSON: {"status": "success"/"failure", "feedback": "..."}

    if no pending and workers still running:
        await asyncio.sleep(0.5)             # poll interval
```

### The Queue Mechanism (workboard_mcp.py)

```
Worker calls edit_board(old, new, reason)
    │
    ▼
submit_edit():
    1. snapshot = read_board_sync()           # capture board state for review context
    2. request = EditRequest(
           request_id = uuid4(),
           worker_idx = _current_worker_idx.get(),   # from ContextVar
           edit_type = "edit",
           params = {"old_text": old, "new_text": new},
           reason = reason,
           board_snapshot = snapshot,
           event = asyncio.Event(),          # BLOCKING mechanism
       )
    3. _pending[request.request_id] = request  # ADD to queue
    4. await request.event.wait()              # WORKER BLOCKS HERE
    5. _pending.pop(request.request_id)        # worker removes itself after unblocked
    6. return json.dumps(request.result)       # {"status": ..., "feedback": ...}
```

### Debugging Phase 2

- **Trajectory events** (per worker): `worker_start`, `tool_call`/`tool_result` (real-time MCP calls), `worker_end`
- **Trajectory events** (orchestrator): `workers_dispatched`, `review_loop_start`, `workboard_review`, `review_loop_end`
- **Log files**: `logs/worker-{idx}-*.jsonl` (one per worker), `logs/orchestrator-*.jsonl`
- **Common issue**: Worker blocks forever on edit_board → orchestrator not polling (check review_loop is running)
- **Common issue**: Worker crashes → `job.set_error(idx, ...)` but other workers continue
- **Common issue**: edit_board approved but old_text not found → returns failure (not silent success)

---

## Phase 2→3 Transition

**Trigger:** `job.all_done()` returns `True`

```python
def all_done(self) -> bool:
    return all(t.done() for t in self.tasks)    # ALL asyncio.Tasks must be done
```

A task is "done" when `_run_single_worker` returns (success) or raises (error). Both cases are handled — `set_result` or `set_error`.

**After review loop exits** (back in `run()`):

```python
await self._review_loop(job, messages)     # ← returns when all workers done
self._update_workboard(job)                # ← mechanical checkbox updates
messages.append(
    HumanMessage(content=self._format_results(job))   # ← results to LLM
)
continue                                   # ← back to top of while True
```

---

## Phase 3: AGGREGATE — Results Back to Orchestrator

### Step 3a: Workboard Update (`_update_workboard`)

Mechanical (no LLM) — modifies `workspace/.workboard.md`:
1. Converts `- [ ]` to `- [x]` for each completed/errored worker
2. Appends a `## Results` section with each worker's result summary

### Step 3b: Results Formatted (`_format_results`)

Produces text like:
```
All workers completed. Results:

**Worker 0** (Research Johnnie Walker...):
Based on research into the core and permanent product ranges...

**Worker 1** (Research Smirnoff and Grey Goose...):
The core product ranges for Smirnoff...
```

This text is appended as a `HumanMessage` to the conversation.

### Step 3c: LLM Processes Results (back to top of while True)

The LLM now sees the full conversation: system prompt → original query → (any pre-plan tool calls) → plan output → worker results. It can:

| LLM Action | What Happens | Code Path |
|---|---|---|
| Call `read_files` | Reads worker-created files to verify | Branch A → loops back |
| Call `run_command` | Runs code to test | Branch A → loops back |
| Output another plan | Dispatches FIX round → back to Phase 2 | Branch B → new workers |
| Output plain text | Final answer → exits | Branch C → return |

### What `run()` returns

```python
return {"output": output, "messages": messages}
```
- `output`: The final text response (string)
- `messages`: The FULL conversation list (SystemMessage, HumanMessage, AIMessage, ToolMessage, ...)

### Debugging Phase 3

- **Trajectory events**: `workers_completed`, then `llm_turn_start` (aggregation turn), possibly `orch_tool_call`/`orch_tool_result`, finally `orchestrator_end`
- **Common issue**: LLM outputs another plan instead of final answer → unexpected extra worker round
- **Common issue**: LLM truncates worker results in synthesis → data lost in final output
- **Common issue**: `_update_workboard` regex fails to find `- [ ]` → checkboxes not updated

---

## Complete Data Flow Diagram

```
                         ┌─────────────────────────────────────────┐
                         │        OrchestratorAgent.run()          │
                         │                                         │
  query ──────────►      │  messages = [System, Human(query)]      │
                         │                                         │
                    ┌────│  while True:                            │
                    │    │    LLM.bind_tools().ainvoke(messages)    │
                    │    │         │                                │
                    │    │    ┌────┴────────────┐                   │
                    │    │    │   tool_calls?   │──► execute ──► loop
                    │    │    │   plan found?   │──► Phase 2 below │
                    │    │    │   plain text?   │──► RETURN ────────┼──► {"output": ..}
                    │    │    └─────────────────┘                   │
                    │    └─────────────────────────────────────────┘
                    │                    │ plan found
                    │                    ▼
                    │    ┌─────────────────────────────────────┐
                    │    │  _start_workers(subtasks, workboard)│
                    │    │  write_board_sync(workboard)        │
                    │    │  create_task × N                    │
                    │    └───────┬───────────┬─────────────────┘
                    │            │           │
                    │    ┌───────▼──┐  ┌─────▼────┐
                    │    │ Worker 0 │  │ Worker 1 │  ...
                    │    │          │  │          │
                    │    │ MCPAgent │  │ MCPAgent │       ┌──────────────────┐
                    │    │ .run()   │  │ .run()   │       │  _review_loop()  │
                    │    │          │  │          │       │                  │
                    │    │ edit_ ───┼──┼──────────┼──────►│ get_pending()    │
                    │    │ board()  │  │          │       │ for req:         │
                    │    │ BLOCKS   │  │          │       │   LLM reviews    │
                    │    │          │  │          │◄──────│   resolve_req()  │
                    │    │ UNBLOCKS │  │          │       │   EDITS BOARD    │
                    │    │          │  │          │       │                  │
                    │    │ result ──┼──┼──► job.results    └──────────────────┘
                    │    └──────────┘  └──────────┘
                    │                    │ all tasks done
                    │                    ▼
                    │    ┌─────────────────────────────────────┐
                    │    │  _update_workboard(job)             │
                    │    │  _format_results(job)               │
                    │    │  messages.append(results)           │
                    │    │  continue ──────────────────────────┼──► back to LLM
                    │    └─────────────────────────────────────┘
                    │
                    └── (LLM may dispatch another round or output final answer)
```

---

## Trajectory Files Quick Reference

| File | Contains | Key Events |
|---|---|---|
| `logs/orchestrator-*.jsonl` | Full orchestrator lifecycle | `orchestrator_start`, `llm_turn_start`, `plan_extracted`, `workers_dispatched`, `workboard_review`, `workers_completed`, `orchestrator_end` |
| `logs/worker-{idx}-*.jsonl` | Per-worker tool calls + LLM turns | `worker_start`, `tool_call`, `tool_result`, `llm_tool_call`, `tool_message`, `llm_response_text`, `worker_end` |

**View latest session:** `python logs/view_trajectory.py`
**View orchestrator only:** `python logs/view_trajectory.py --orch`
**List all files:** `python logs/view_trajectory.py --list`
