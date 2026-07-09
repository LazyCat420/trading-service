from app.services.verdict_service import get_latest_verdicts
try:
    v = get_latest_verdicts(5)
    print("Success! Latest 5 verdicts:", [x['ticker'] for x in v])
except Exception as e:
    print("Error:", e)
