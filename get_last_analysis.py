from app.db.connection import get_db

with get_db() as db:
    cols = db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'pipeline_events'").fetchall()
    print("Columns:", [c[0] for c in cols])
    
    events = db.execute("""
        SELECT cycle_id, event_type, data
        FROM pipeline_events 
        WHERE event_type IN ('gatekeeper_decision', 'cycle_started')
        ORDER BY id DESC 
        LIMIT 10
    """).fetchall()
    
    print("Recent Events:")
    for e in events:
        print(f"Type: {e[1]}, Data: {str(e[2])[:300]}")
        
    news = db.execute("SELECT COUNT(*) FROM news_articles WHERE ticker = 'SWBI'").fetchone()[0]
    reddit = db.execute("SELECT COUNT(*) FROM reddit_posts WHERE ticker = 'SWBI'").fetchone()[0]
    print(f"\nSWBI mentions - News: {news}, Reddit: {reddit}")
