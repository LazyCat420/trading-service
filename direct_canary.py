import asyncio
import logging
from app.v3.orchestrator import run_v3_pipeline
from app.v3.desk_persistence import load_desk

logging.basicConfig(level=logging.INFO)

async def test_canary():
    cycle_id = "canary_test_direct_007"
    print(f"Triggering direct canary cycle: {cycle_id}")
    
    def emit_cb(phase, step, detail, **kwargs):
        print(f"[{phase}] {step}: {detail}")
        
    result = await run_v3_pipeline("AAPL", cycle_id=cycle_id, emit=emit_cb)
    print("Pipeline result:", result)

    from app.services.result_saver import save_analysis_result
    save_analysis_result("AAPL", cycle_id, result)
    
    desk = load_desk(cycle_id, "AAPL")
    if desk:
        print("\n--- Desk Data ---")
        print("Phase Outcomes:", desk.phase_outcomes)
        if desk.final_decision:
            print("Final Decision:", desk.final_decision)
    else:
        print("Desk not found.")

if __name__ == "__main__":
    asyncio.run(test_canary())
