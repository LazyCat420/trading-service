import asyncio
import logging
logging.basicConfig(level=logging.INFO)
from app.autoresearch.reflection import _rule_based_reflection
from app.utils.text_utils import parse_json_response

def main():
    print("Testing parse_json_response with malformed json")
    res = parse_json_response("Just some prose here, no JSON")
    print("Parsed result:", res)
    if res is None:
        logging.warning("[AUTORESEARCH] parse_json_response returned None, falling back to rule-based")
        
    print("Testing _rule_based_reflection fallback")
    audit_bundle = {"cycle_id": "test-123"}
    fb = _rule_based_reflection(audit_bundle)
    print("Fallback dict:", fb)

if __name__ == "__main__":
    main()
