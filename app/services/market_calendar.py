import pytz
from datetime import datetime, timedelta, time

class MarketCalendar:
    @staticmethod
    def _to_et(dt: datetime = None) -> datetime:
        et = pytz.timezone("US/Eastern")
        if dt is None:
            return datetime.now(et)
        if dt.tzinfo is None:
            return et.localize(dt)
        return dt.astimezone(et)

    @staticmethod
    def get_market_state(now: datetime = None) -> str:
        """Return 'pre_market', 'open', 'after_hours', 'closed', or 'holiday'."""
        now_et = MarketCalendar._to_et(now)
        
        if now_et.weekday() >= 5:
            return "closed"
            
        month, day = now_et.month, now_et.day
        # Very basic static holiday approximation
        if (month == 1 and day == 1) or (month == 7 and day == 4) or (month == 12 and day == 25):
            return "holiday"
            
        current_time = now_et.time()
        
        if time(4, 0) <= current_time < time(9, 30):
            return "pre_market"
        elif time(9, 30) <= current_time < time(16, 0):
            return "open"
        elif time(16, 0) <= current_time < time(20, 0):
            return "after_hours"
        else:
            return "closed"
            
    @staticmethod
    def is_market_open(now: datetime = None) -> bool:
        return MarketCalendar.get_market_state(now) == "open"

    @staticmethod
    def get_next_window(window: str, from_time: datetime = None) -> datetime:
        """
        Map a policy window (e.g. 'next_open', 'pre_close') to a specific future datetime.
        """
        now = MarketCalendar._to_et(from_time)
        
        # Advance to the next non-weekend/holiday if it's currently a weekend/holiday
        def advance_to_trading_day(dt: datetime) -> datetime:
            while dt.weekday() >= 5 or MarketCalendar.get_market_state(dt.replace(hour=12, minute=0)) == "holiday":
                dt += timedelta(days=1)
            return dt

        base_day = now
        
        if window == "next_pre_market":
            if now.time() >= time(9, 30):
                base_day += timedelta(days=1)
            base_day = advance_to_trading_day(base_day)
            return base_day.replace(hour=8, minute=0, second=0, microsecond=0)
            
        elif window == "next_open":
            if now.time() >= time(9, 30):
                base_day += timedelta(days=1)
            base_day = advance_to_trading_day(base_day)
            return base_day.replace(hour=9, minute=30, second=0, microsecond=0)
            
        elif window == "midday":
            if now.time() >= time(12, 0):
                base_day += timedelta(days=1)
            base_day = advance_to_trading_day(base_day)
            return base_day.replace(hour=12, minute=0, second=0, microsecond=0)
            
        elif window == "pre_close":
            if now.time() >= time(15, 30):
                base_day += timedelta(days=1)
            base_day = advance_to_trading_day(base_day)
            return base_day.replace(hour=15, minute=30, second=0, microsecond=0)
            
        elif window == "post_close":
            if now.time() >= time(16, 15):
                base_day += timedelta(days=1)
            base_day = advance_to_trading_day(base_day)
            return base_day.replace(hour=16, minute=15, second=0, microsecond=0)
            
        elif window == "next_trading_day":
            base_day += timedelta(days=1)
            base_day = advance_to_trading_day(base_day)
            return base_day.replace(hour=9, minute=30, second=0, microsecond=0)
            
        elif window == "next_week":
            days_ahead = 7 - now.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            base_day += timedelta(days=days_ahead)
            base_day = advance_to_trading_day(base_day)
            return base_day.replace(hour=9, minute=30, second=0, microsecond=0)
            
        # Default fallback
        return now + timedelta(hours=4)
