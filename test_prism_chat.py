import asyncio
from app.services.prism_agent_caller import call_prism_agent
from app.config import settings

async def main():
    try:
        response, tokens, elapsed = await call_prism_agent(
            agent_id="TICKER_VALIDATION_AGENT",
            user_message='TICKER CANDIDATES TO VALIDATE:\n[{"symbol": "TEST", "snippet": "this is a test"}]',
            fallback_system_prompt="See app.agents.custom.ticker_validator_agent",
            fallback_agent_name="ticker_validator",
            temperature=0.1,
            max_tokens=1024,
        )
        print("RESPONSE:", response)
        print("TOKENS:", tokens)
    except Exception as e:
        print("ERROR:", e)

if __name__ == "__main__":
    asyncio.run(main())
