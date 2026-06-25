import asyncio
import os
import sys

# Ensure app is in path
sys.path.insert(0, os.path.abspath('.'))

from app.v3.shared_desk import SharedDesk
from app.v3.agent_runner import run_v3_agent

async def main():
    desk = SharedDesk(ticker="MSFT")
    from app.v3.agents import fundamental_analyst as fa_module
    try:
        await run_v3_agent(desk, fa_module)
        print("\n\nSUCCESS!")
        print("Fundamental Report:")
        print(desk.fundamental_report)
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
