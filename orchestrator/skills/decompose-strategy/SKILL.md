---
name: decompose-strategy
description: Strategy for decomposing complex tasks into independent, self-contained subtasks for parallel worker execution.
---

# Decomposition Strategy

## Core Principles
- One focused goal per subtask — maximize parallelism
- Each subtask must be SELF-CONTAINED with full context
- Workers are STATELESS — never write "use the result from subtask 1"
- Keep subtasks atomic and bounded
- If the task has many parts, split into bounded slices
- When asked to search for information about elements in a broad category (e.g. "comprehensive list of X"), first search for the list of relevant elements, then in the next turn create separate subtasks for each element to find details and verify information.

## CRITICAL: Workers are STATELESS
- Write SELF-CONTAINED descriptions with full details
- Never write "find details for the above" — workers have no context
- GOOD: "Read the file /home/user/project/config.py and extract the database URL"
- BAD: "Read the config file mentioned earlier"

## Worker Capabilities
Each worker is a Memento-S agent powered by Agent Skills — capable of handling most tasks
including file operations, shell commands, web search, package management, and more.
Workers automatically select the best skill for each subtask and can dynamically
acquire new skills on demand. Each worker handles complex tasks iteratively.
Based on this, focus on decomposing the task into clear, self-contained subtasks.
