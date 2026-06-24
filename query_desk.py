import os
import psycopg
from dotenv import load_dotenv
load_dotenv()
with psycopg.connect(os.getenv("DATABASE_URL")) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT desk_data FROM shared_desk WHERE cycle_id = 'canary_test_direct_005'")
        row = cur.fetchone()
        if row:
            desk = row[0]
            print("Action:", desk.get("final_decision", {}).get("action"))
            print("Confidence:", desk.get("final_decision", {}).get("confidence"))
            print("Regime:", desk.get("regime_classification", {}).get("regime"))
            
            bull_conf = desk.get("bull_argument", {}).get("confidence", 0) if desk.get("bull_argument") else 0
            bear_conf = desk.get("bear_rebuttal", {}).get("confidence", 0) if desk.get("bear_rebuttal") else 0
            def_conf = desk.get("bull_defense", {}).get("final_confidence", bull_conf) if desk.get("bull_defense") else bull_conf
            
            winner = "bull" if def_conf > bear_conf else "bear" if bear_conf > def_conf else "tie"
            print("Debate Winner:", winner)
