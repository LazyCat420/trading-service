import os

import requests

url = "http://10.0.0.16:7777/agent?stream=false"
# Prism attributes requests by these HEADERS, not by body fields.
HEADERS = {
    "Content-Type": "application/json",
    "x-project": os.getenv("PRISM_PROJECT", "vllm-trading-bot"),
    "x-username": os.getenv("PRISM_USERNAME", "lazy-trader"),
}
models_to_try = [
    ("cyankiwi/MiniMax-M2.7-AWQ-4bit", "vllm-2")
]

for model, prov in models_to_try:
    payload = {
        "model": model,
        "provider": prov,
        "maxTokens": 8192,
        "messages": [{"role": "user", "content": "Hello"}],
        "agentName": "v3_junior_analyst"
    }
    try:
        resp = requests.post(url, json=payload, headers=HEADERS, timeout=20)
        print(f"Model {model} on {prov}: {resp.status_code}")
        if resp.status_code != 200:
            print(f"  {resp.text}")
        else:
            print(f"  SUCCESS!")
            print(resp.json())
    except Exception as e:
        print(f"Model {model}: failed - {e}")
