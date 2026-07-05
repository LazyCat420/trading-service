import cloudscraper
import sys

scraper = cloudscraper.create_scraper()
res = scraper.get("https://www.capitoltrades.com/trades?page=1&txType=buy&txType=sell&assetType=stock")
print("Status Code:", res.status_code)
print("Length:", len(res.text))
