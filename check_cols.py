from app.db.connection import get_db

with get_db() as db:
    yt = db.execute("SELECT * FROM youtube_transcripts LIMIT 0").description
    rd = db.execute("SELECT * FROM reddit_posts LIMIT 0").description
    print("YT:", [d[0] for d in yt])
    print("RD:", [d[0] for d in rd])
