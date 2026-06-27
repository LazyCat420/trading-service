from app.db.connection import get_db

def get_trending(limit=5):
    with get_db() as db:
        # Trending in News
        news_trends = db.execute("""
            SELECT ticker, COUNT(*) as cnt 
            FROM news_articles 
            WHERE ticker IS NOT NULL 
            GROUP BY ticker 
            ORDER BY cnt DESC 
            LIMIT 10
        """).fetchall()
        
        # Trending on Reddit
        reddit_trends = db.execute("""
            SELECT ticker, COUNT(*) as cnt 
            FROM reddit_posts 
            WHERE ticker IS NOT NULL 
            GROUP BY ticker 
            ORDER BY cnt DESC 
            LIMIT 10
        """).fetchall()
        
        print("News trends:", news_trends)
        print("Reddit trends:", reddit_trends)

get_trending()
