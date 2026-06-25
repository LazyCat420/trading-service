import asyncio
from app.v3.shared_desk import SharedDesk
from app.v3.agents.fundamental_analyst import FundamentalAnalyst

async def main():
    desk = SharedDesk(ticker="AAPL")
    agent = FundamentalAnalyst()
    try:
        await agent.run(desk)
        print("Success:", desk.fundamental_report)
    except Exception as e:
        import traceback
        traceback.print_exc()

asyncio.run(main())
