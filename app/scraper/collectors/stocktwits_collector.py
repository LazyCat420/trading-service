"""
stocktwits_collector.py — Domain-agnostic StockTwits symbol stream API client.
"""
import logging
import datetime
import httpx
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

@dataclass
class StockTwitsMessage:
    id: str
    body: str
    username: str
    display_name: str
    followers: int
    sentiment: str | None
    created_at: datetime.datetime

class StockTwitsCollector:
    async def get_symbol_stream(self, symbol: str, limit: int = 30) -> list[StockTwitsMessage]:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol.upper()}.json"
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            logger.error(f"[stocktwits] Failed to fetch symbol stream for {symbol}: {e}")
            return []

        messages = data.get("messages", [])
        results = []
        for msg in messages[:limit]:
            msg_id = str(msg.get("id"))
            body = msg.get("body", "")
            created_at_str = msg.get("created_at")
            
            user = msg.get("user", {})
            username = user.get("username", "")
            display_name = user.get("name", "")
            followers = user.get("followers", 0)
            
            sentiment = msg.get("sentiment", {}).get("basic") if msg.get("sentiment") else None
            
            # Parse created_at ISO 8601 string
            try:
                created_at = datetime.datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            except Exception:
                created_at = datetime.datetime.now(datetime.timezone.utc)

            results.append(StockTwitsMessage(
                id=msg_id,
                body=body,
                username=username,
                display_name=display_name,
                followers=int(followers) if followers else 0,
                sentiment=sentiment,
                created_at=created_at
            ))
        return results

def _serialize_message(msg: StockTwitsMessage) -> dict:
    d = asdict(msg)
    d["created_at"] = msg.created_at.isoformat()
    return d
