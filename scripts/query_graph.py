import os
import psycopg
from dotenv import load_dotenv

load_dotenv()
db_url = "postgresql://admin:admin@10.0.0.16:5433/trading_bot"
try:
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ontology_nodes;")
            nodes = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ontology_edges;")
            edges = cur.fetchone()[0]
            print(f"Nodes: {nodes}, Edges: {edges}")
except Exception as e:
    print(f"Error: {e}")
