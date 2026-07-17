"""Absorbed scraper-service — general-purpose scraping engines, collectors, and routes.

Moved in-process from the standalone scraper-service (retired). Imports rooted at
app.scraper.* Served via app/routers/{scrape,collect,stream}_router.py and called
in-process by app/services/scraper_client.py.
"""
