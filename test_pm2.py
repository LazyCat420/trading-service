import asyncio
import os
import sys

from app.v3.agents.portfolio_manager import SYSTEM_PROMPT, AGENT_NAME
from app.agents.base_agent import run_agent

async def main():
    try:
        res = await run_agent(
            agent_name=AGENT_NAME,
            ticker='WATCHLIST',
            cycle_id='test1234',
            bot_id='cycle-backend',
            system_prompt=SYSTEM_PROMPT,
            user_prompt='Here is the active watchlist snapshot:\n\n[]',
            enable_tools=False
        )
        print('RESULT:', res)
    except Exception as e:
        print('ERROR:', type(e).__name__, e)

if __name__ == '__main__':
    asyncio.run(main())
