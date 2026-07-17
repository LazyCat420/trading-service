"""
leafly_collector.py — Scrapes strain terpene profiles directly from Leafly.
Uses Leafly's pre-rendered Next.js JSON (__NEXT_DATA__) inside HTML.
"""

import logging
import re
import json
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger(__name__)

class LeaflyCollector:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

    async def get_strain(self, query: str) -> Optional[Dict[str, Any]]:
        """Scrape strain data by querying/slugifying the strain name."""
        # Normalize the slug
        slug = query.lower().strip()
        slug = re.sub(r'[^a-z0-9\s\-_]', '', slug)
        slug = re.sub(r'[\s_]+', '-', slug)
        slug = re.sub(r'-+', '-', slug)
        
        slugs_to_try = [slug]
        slug_no_hyphens = slug.replace("-", "")
        if slug_no_hyphens != slug:
            slugs_to_try.append(slug_no_hyphens)
            
        html = None
        used_slug = slug
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for s in slugs_to_try:
                url = f"https://www.leafly.com/strains/{s}"
                logger.info(f"[leafly] Scraping {s} at {url}")
                try:
                    response = await client.get(url, headers=self.headers)
                    if response.status_code == 404:
                        logger.warning(f"[leafly] Strain not found (404) for slug: {s}")
                        continue
                    response.raise_for_status()
                    html = response.text
                    used_slug = s
                    break
                except Exception as e:
                    logger.error(f"[leafly] Failed to fetch {url}: {e}")
                    
        if not html:
            return None
            
        url = f"https://www.leafly.com/strains/{used_slug}"
                
        # Parse __NEXT_DATA__ script tag
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not match:
            logger.warning(f"[leafly] __NEXT_DATA__ script tag not found on page {url}")
            return None
            
        try:
            next_json = json.loads(match.group(1))
            strain_data = next_json.get("props", {}).get("pageProps", {}).get("strain", {})
            if not strain_data:
                logger.warning(f"[leafly] Strain data missing from pageProps on page {url}")
                return None
                
            name = strain_data.get("name", query)
            raw_terps = strain_data.get("terps", {}) or {}
            
            # Format terpenes: { name: score }
            terpenes = {}
            for key, val in raw_terps.items():
                if isinstance(val, dict) and "score" in val:
                    # Leafly's score is a float value representing relative terpene weight/profile
                    terpenes[key] = val["score"]
                    
            return {
                "name": name,
                "slug": used_slug,
                "source_url": url,
                "terpenes": terpenes,
            }
        except Exception as e:
            logger.error(f"[leafly] Failed to parse __NEXT_DATA__ from {url}: {e}")
            return None
