"""Entry point for the multi-agent workflow."""

import asyncio
import os
import sys

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from orchestrator.orchestrator_agent import OrchestratorAgent

load_dotenv()

# Ensure Memento-S is on the import path for workboard cleanup
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "Memento-S"))


async def main():
    # Clean up old workboard from previous runs
    try:
        from core.workboard import cleanup_board
        cleanup_board()
    except Exception:
        pass  # Non-fatal
    model = ChatOpenAI(
        model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5"),
        openai_api_key=os.getenv("OPENROUTER_API_KEY"),
        openai_api_base=os.getenv("OPENROUTER_BASE_URL"),
        temperature=0,
    )
    orchestrator = OrchestratorAgent(model=model)

    await orchestrator.start()

    task = input("Enter your task: ")
    result = await orchestrator.run(task)

    print("\n=== Final Result ===")
    print(result["output"])

    await orchestrator.close()


if __name__ == "__main__":
    asyncio.run(main())
