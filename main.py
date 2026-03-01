"""Entry point for the multi-agent workflow."""

import asyncio
import os
import sys

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from orchestrator.orchestrator_agent import OrchestratorAgent

load_dotenv()

# Ensure Memento_S is on the import path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "Memento_S"))


async def main():
    # Clean up old workboard from previous runs
    try:
        from core.workboard_mcp import cleanup_board_sync
        cleanup_board_sync()
    except Exception:
        pass  # Non-fatal

    model = ChatOpenAI(
        model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5"),
        openai_api_key=os.getenv("OPENROUTER_API_KEY"),
        openai_api_base=os.getenv("OPENROUTER_BASE_URL"),
        temperature=0,
    )
    orchestrator = OrchestratorAgent(model=model)

    task = input("Enter your task: ")
    result = await orchestrator.run(task)

    print("\n=== Final Result ===")
    print(result["output"])
    print("\n(Tip: Run `python logs/view_trajectory.py` to see worker execution logs)")


if __name__ == "__main__":
    asyncio.run(main())
