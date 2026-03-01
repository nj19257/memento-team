# Plan: Workboard MCP + Orchestrator Approval Flow

## Goal

Migrate from old ops-based `Memento-S_old` to new MCP-based `Memento_S`. Add a workboard MCP server with an **orchestrator approval flow** for edit requests. Workers connect to both the core MCP server and the workboard MCP server. The orchestrator reviews worker edit requests via multi-turn conversation.

---

## How It Works: The Three Phases

### Phase 1 — Agent runs, calls execute_subtasks, STOPS

The orchestrator is a custom agent loop (not `create_agent()`). It calls the LLM with tools bound (`execute_subtasks`, `read_files`, `run_command`). The LLM reasons and eventually calls `execute_subtasks`.

**`execute_subtasks` is rewritten to be non-blocking:** it starts workers and returns immediately (no `asyncio.gather`, no awaiting results). It returns a simple acknowledgment like `{"status": "started"}`.

**The agent loop intercepts this.** Instead of appending the ToolMessage and calling the LLM again, we STOP. The agent's message state at this point:

```
messages = [
  SystemMessage(orchestrator_prompt),
  HumanMessage(user_task),
  AIMessage("I'll decompose this into subtasks..." + tool_call: execute_subtasks({subtasks: [...], workboard: "..."}))
  # NO ToolMessage yet — agent is frozen here
]
```

The agent is frozen. It knows it requested `execute_subtasks` but has no result yet.

**What `execute_subtasks` does (fire-and-forget):**
```python
async def execute_subtasks(subtasks: list[str], workboard: str = "") -> WorkerJob:
    # 1. Write workboard file (if provided)
    if workboard.strip():
        write_board(workboard)

    # 2. Start each worker as an asyncio.create_task (non-blocking)
    job = WorkerJob(subtasks=subtasks)
    for idx, subtask in enumerate(subtasks):
        task = asyncio.create_task(run_single_worker(idx, subtask))
        job.tasks.append(task)

    # 3. Return immediately — do NOT await workers
    return job
```

The agent loop gets back the `WorkerJob` handle (not a string result). It does NOT create a ToolMessage yet. Instead, it enters Phase 2.

### Phase 2 — Multi-turn review conversation (separate from agent)

While workers run, edit requests queue up. We run a **separate multi-turn conversation** with the same orchestrator LLM to review them.

This review conversation has its own message list:

```
review_messages = [
  SystemMessage("You are reviewing workboard edit requests from workers.
    For each request, respond with JSON:
    - Approved: {"status": "success", "feedback": ""}
    - Rejected: {"status": "failure", "feedback": "reason and suggestions"}"),
]
```

As workers submit edit requests:

```
review_messages += [
  HumanMessage("Worker 2 requests an edit:
    Edit type: replace
    Old text: '| 2 | Worker 2 | Build API | pending |'
    New text: '| 2 | Worker 2 | Build API | done |'
    Reason: 'Finished building the API endpoint'
    Current workboard:
    ```markdown
    ... full workboard content ...
    ```"),
  AIMessage('{"status": "success", "feedback": ""}'),      ← LLM reviews

  HumanMessage("Worker 0 requests an edit: ..."),
  AIMessage('{"status": "failure", "feedback": "Don't mark as done — tests are still failing"}'),
]
```

Each `AIMessage` response is parsed → `resolve_request()` → worker unblocked.

This loop continues until all workers finish.

### Phase 3 — Agent RESUMES with full context

All workers are done. We now build a `ToolMessage` for the original `execute_subtasks` tool call that contains everything:

```
ToolMessage(
  tool_call_id = original_tc.id,
  content = """
## Worker Results
### Worker 0 (subtask: "Build the frontend")
Result: Created src/App.tsx, src/components/Header.tsx ...

### Worker 1 (subtask: "Build the API")
Result: Created src/api/server.py, src/api/routes.py ...

## Edit Review Log
During execution, you reviewed the following workboard edit requests:

1. Worker 2 requested: replace '| 2 | ... | pending |' → '| 2 | ... | done |'
   Reason: "Finished building the API endpoint"
   Your decision: APPROVED

2. Worker 0 requested: replace '| 0 | ... | pending |' → '| 0 | ... | done |'
   Reason: "Frontend is complete"
   Your decision: REJECTED — "Don't mark as done — tests are still failing"

## Current Workboard
```markdown
... final workboard state ...
```
""")
```

The agent's messages are now:

```
messages = [
  SystemMessage(orchestrator_prompt),
  HumanMessage(user_task),
  AIMessage(tool_call: execute_subtasks),       ← the original call
  ToolMessage(results + review log + workboard) ← everything that happened
]
```

The agent resumes. The LLM sees the full context: what it asked for, what the workers produced, what edits it approved/rejected, and the current workboard. It can now:
- Synthesize a final response
- Call `run_command` to verify the code
- Call `execute_subtasks` again for a FIX round (which triggers the same stop/review/resume cycle)

---

## Phase 1: Using `astream()` to Detect and Terminate

We use the **standard LangChain agent** (`create_agent()`) with tools registered, and **stream** its execution. The `execute_subtasks` tool is registered like any other tool — but it's **fire-and-forget** (starts workers, returns `{"status": "started"}` immediately). We observe the stream and **terminate when we detect the execute_subtasks return**.

```python
# Build standard agent
agent_graph = create_agent(model=model, tools=tools, system_prompt=prompt)

# Stream the agent — observe updates
collected_messages = []
job = None

async for chunk in agent_graph.astream(
    {"messages": [HumanMessage(query)]},
    stream_mode="updates",
):
    # Collect messages from the stream
    for node_name, node_output in chunk.items():
        if "messages" in node_output:
            for msg in node_output["messages"]:
                collected_messages.append(msg)

    # IF CONDITION: detect execute_subtasks tool return
    if job is None:
        # Check if the latest tool message is from execute_subtasks
        for msg in collected_messages:
            if isinstance(msg, ToolMessage) and '"status": "started"' in msg.content:
                job = _active_job   # set as side-effect by execute_subtasks
                break

    if job is not None:
        break   # ← TERMINATE the agent stream
```

At this point `collected_messages` contains:
```
[AIMessage("I'll decompose..." + tool_call: execute_subtasks),
 ToolMessage('{"status": "started"}')]
```

The agent is terminated. Workers are running in the background.

## Phase 2: Queue-Based Edit Review

Workers append edit requests to a **shared list**. The orchestrator processes them **one by one, in order**.

### The Queue

```python
# Module-level in workboard_mcp.py
_edit_queue: list[EditRequest] = []    # workers append here

# Worker calls edit_board → creates EditRequest → appends to _edit_queue → awaits event
# Orchestrator pops from front → reviews → resolves → worker unblocked
```

### The Review Loop

```python
review_messages = [SystemMessage(review_system_prompt)]

while not job.all_done():
    # Check the queue
    if _edit_queue:
        request = _edit_queue.pop(0)    # ← pop first item (FIFO)

        # Present to LLM as a conversation turn
        review_messages.append(HumanMessage(content=format_edit_request(request)))
        response = await model.ainvoke(review_messages)
        review_messages.append(response)

        # Parse {"status": "success"/"failure", "feedback": "..."}
        decision = parse_decision(response.content)
        await resolve_request(request, decision)
        # → request.event.set() → worker unblocks

        review_log.append({
            "worker": request.worker_idx,
            "edit_type": request.edit_type,
            "reason": request.reason,
            "decision": decision,
        })
    else:
        await asyncio.sleep(0.5)    # no pending edits, wait briefly
```

Workers append to `_edit_queue` from their async tasks. The review loop pops one at a time. Since everything runs on the same asyncio event loop (single-threaded), no lock is needed for the list — `append()` and `pop(0)` are safe.

## Phase 3: Resume the Agent with Full Context

After all workers finish, we build the full result and **resume the agent**:

```python
# Build the full ToolMessage content
full_result = build_result_message(job, review_log)

# Replace the placeholder ToolMessage in collected_messages
# (swap '{"status": "started"}' with the full result)
for i, msg in enumerate(collected_messages):
    if isinstance(msg, ToolMessage) and '"status": "started"' in msg.content:
        collected_messages[i] = ToolMessage(
            content=full_result,
            tool_call_id=msg.tool_call_id,
        )
        break

# Resume the agent by calling ainvoke with the updated messages
result = await agent_graph.ainvoke({"messages": collected_messages})
final_output = extract_output(result)
```

The agent resumes with messages:
```
[AIMessage(tool_call: execute_subtasks)]
[ToolMessage(full results + review log + workboard state)]
```

The LLM sees everything: what workers produced, what edits were reviewed, the final workboard. It can then synthesize, verify (call read_files/run_command), or dispatch more subtasks (another execute_subtasks → same stop/review/resume cycle).

## Full Flow Diagram

```
OrchestratorAgent.run(query):
│
├─ Phase 1: STREAM agent with astream()
│   ├─ LLM reasons, may call read_files/run_command (normal tool flow)
│   ├─ LLM calls execute_subtasks → tool starts workers, returns {"status":"started"}
│   ├─ IF detected execute_subtasks return → BREAK stream
│   └─ collected_messages saved (includes AIMessage + placeholder ToolMessage)
│
├─ Phase 2: REVIEW LOOP (while workers running)
│   ├─ review_messages = [SystemMessage(review prompt)]
│   ├─ while not all workers done:
│   │   ├─ if _edit_queue has items:
│   │   │   ├─ pop first request
│   │   │   ├─ review_messages += HumanMessage(edit details)
│   │   │   ├─ response = model.ainvoke(review_messages)
│   │   │   ├─ review_messages += response
│   │   │   ├─ parse decision → resolve → worker unblocked
│   │   │   └─ append to review_log
│   │   └─ else: await asyncio.sleep(0.5)
│   └─ all workers done
│
├─ Phase 3: RESUME agent
│   ├─ Build full ToolMessage (results + review log + workboard)
│   ├─ Replace placeholder ToolMessage in collected_messages
│   ├─ agent_graph.ainvoke(collected_messages) → LLM continues
│   └─ LLM synthesizes / verifies / dispatches fix round
│
└─ Return final output
```

---

## Files to Create / Modify

### 1. CREATE `Memento_S/core/workboard_mcp.py`

FastMCP server with workboard tools + shared approval queue.

**MCP Tools (used by workers):**

| Tool | Behavior |
|------|----------|
| `read_board()` | Direct read — returns workboard markdown |
| `write_board(content)` | Direct write — creates/overwrites workboard |
| `edit_board(old_text, new_text, reason)` | Queued — submits request, `await event.wait()`, returns `{"status":"success/failure","feedback":"..."}` |
| `append_board(text, reason)` | Queued — same approval flow |
| `cleanup_board()` | Direct — deletes workboard file |

**Approval Queue (module-level, shared state):**

```python
@dataclass
class EditRequest:
    request_id: str              # uuid4
    worker_idx: int              # from ContextVar
    edit_type: str               # "edit" or "append"
    params: dict                 # {"old_text":..., "new_text":...} or {"text":...}
    reason: str                  # worker's reason
    board_snapshot: str          # workboard at request time
    event: asyncio.Event         # set when resolved
    result: dict | None = None   # set by resolve_request()

_pending: dict[str, EditRequest] = {}
```

**Worker context via `contextvars.ContextVar`:**
- `_current_worker_idx: ContextVar[int]` — set before each worker task starts
- `edit_board` reads it to tag the request

**Python API (called by orchestrator, not MCP):**
- `get_pending_requests() → list[EditRequest]` — pop all unresolved requests
- `resolve_request(req, approved, feedback)` — apply edit if approved, set result, `event.set()`
- `set_worker_context(idx) → Token` — set ContextVar for current task
- `write_board(content)` / `cleanup_board()` — direct file ops (also usable outside MCP)

**edit_board flow:**
1. Read current board snapshot
2. Create `EditRequest` with `asyncio.Event()`
3. Add to `_pending` dict
4. `await request.event.wait()` — worker yields to event loop here
5. Event fires (set by `resolve_request`) → worker reads `request.result`
6. Return `json.dumps(request.result)` to worker LLM

---

### 2. MODIFY `Memento_S/core/mcp_client.py`

Make `MCPToolManager` support multiple in-process FastMCP servers.

**Changes:**
- `__init__(extra_servers=None)` — `list[tuple[FastMCP, configure_fn | None]]`
- `_clients: list[Client]` instead of single `_client`
- `_tool_to_client: dict[str, Client]` — routes tool calls to correct client
- `start()` — connect core server, then each extra server
- `shutdown()` — close all clients
- `call_tool()` — lookup client by tool name
- `reconfigure()` — call all configure functions

**Backward compatible:** `MCPToolManager()` with no args = 7 core tools (unchanged).

---

### 3. MODIFY `Memento_S/core/mcp_agent.py`

- Add `extra_servers` param to `MCPAgent.__init__`, forward to `MCPToolManager`
- Update `DEFAULT_SYSTEM_PROMPT` to mention workboard tools

---

### 4. REWRITE `orchestrator/orchestrator_agent.py`

Replace `create_agent()` + MCP subprocess with a custom agent loop.

**Key components:**

**`OrchestratorAgent.__init__`:**
- Accepts `model: BaseChatModel`
- No `start()`/`close()` needed — no MCP subprocess
- Builds the agent graph: `create_agent(model, tools, system_prompt)`
- Tools: `execute_subtasks` (fire-and-forget), `read_files`, `run_command`

**`execute_subtasks(subtasks, workboard)` — the non-blocking tool function:**
- Registered as a LangChain `StructuredTool`
- Writes workboard file if provided
- Starts each worker as `asyncio.create_task()` (non-blocking)
- Stores `WorkerJob` on a module-level `_active_job` variable (so the stream loop can access it)
- Returns `{"status": "started"}` immediately — does NOT await workers

**`OrchestratorAgent.run(query)`:**
- Phase 1: `astream()` the agent graph, collecting messages from the stream
- When the stream yields a ToolMessage containing `"status": "started"` → break the stream (agent terminated)
- Phase 2: `_review_loop(job)` → processes edit queue list one by one as multi-turn conversation
- Phase 3: Replace placeholder ToolMessage with full result → `ainvoke()` the agent graph with updated messages (agent resumes)

**`_run_single_worker(idx, subtask)`:**
- Sets `workboard_mcp.set_worker_context(idx)` (ContextVar)
- Creates `MCPAgent(model=build_chat_model(), extra_servers=[(wb_mcp, wb_configure)])`
- `await agent.start()` → `await agent.run(subtask)` → `await agent.close()`
- Stores result in job

**`_review_loop(job) → list[dict]`:**
- Own `review_messages` list with a review-specific system prompt
- While not all workers done:
  - Pop pending edits from `workboard_mcp.get_pending_requests()`
  - For each: append HumanMessage → `model.ainvoke(review_messages)` → parse → resolve
  - Sleep 0.5s if no pending
- Returns review log (list of dicts for each reviewed request)

**`_build_tool_message(job, review_log, tool_call_id) → ToolMessage`:**
- Formats worker results, review log, and current workboard into the ToolMessage content
- Uses the tool_call_id from the original `execute_subtasks` call

**`WorkerJob` dataclass:**
- `subtasks: list[str]`
- `tasks: list[asyncio.Task]`
- `results: dict[int, str]`
- `errors: dict[int, str]`
- `all_done() → bool`

**System prompt:**
- Describes: plan → execute_subtasks → verification workflow
- `execute_subtasks` is a tool: pass subtasks list + workboard markdown
- After execute_subtasks returns, LLM sees results + edit review log
- Can call read_files/run_command to verify, or call execute_subtasks again for fixes
- Workers are stateless — subtasks must be self-contained

---

### 5. SIMPLIFY `main.py`

- Remove MCP subprocess setup
- `sys.path` points to `Memento_S` (not `Memento-S`)
- Import `workboard_mcp.cleanup_board()` for cleanup
- Create `OrchestratorAgent(model=model)` — no `start()`/`close()`
- `await orchestrator.run(task)` → print result

---

### 6. `orchestrator/mcp_server.py` — DEPRECATED

No longer imported or used. Kept in repo for reference only.

---

### 7. ADD TESTS in `Memento_S/tests/test_mcp_servers.py`

**TestWorkboardServer:**
- `test_read_empty`, `test_write_and_read`, `test_cleanup`

**TestMultiServerClient:**
- `test_discovers_all_tools` (7 core + 5 workboard)
- `test_call_workboard_read`

**TestEditApprovalFlow:**
- `test_edit_approved` — worker submits, orchestrator approves, board updated
- `test_edit_rejected` — worker submits, orchestrator rejects, board unchanged, feedback returned
- `test_multiple_queued` — multiple workers queue edits

---

## Threading / Async Model

Everything runs in **one process, one event loop**:

- Workers are `asyncio.create_task()` — concurrent coroutines
- When a worker calls `edit_board`, it `await event.wait()` — **yields** to the event loop (does NOT block it)
- The review loop runs on the same event loop — it makes LLM calls (async via httpx)
- `resolve_request()` calls `event.set()` — the waiting worker resumes
- Sync tool functions (bash_tool, str_replace) are handled by FastMCP's internal thread executor

No threading.Lock needed for the approval queue since asyncio is single-threaded. File I/O for the workboard file does use `threading.Lock` since FastMCP may run sync tools in threads.

---

## Verification

```bash
# Unit tests
cd Memento_S && python -m pytest tests/test_mcp_servers.py -v

# Backward compat
cd Memento_S && python -m pytest tests/test_mcp_agent.py -v

# End-to-end
python main.py
# Enter a multi-part task → observe:
# 1. Orchestrator plans and calls execute_subtasks
# 2. Workers run, edit requests appear in review conversation
# 3. Orchestrator resumes with results + review log
# 4. Orchestrator synthesizes or verifies
```
