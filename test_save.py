import sys
sys.path.insert(0, '/app')
from app.cycle.orchestration.state_manager import PipelineStateMixin

# Try to load and save state manually to capture the error
print("Loading state...")
PipelineStateMixin.load_state()
print(f"Current cycle_id: {PipelineStateMixin._state.get('cycle_id')}")

# Modify it
PipelineStateMixin._state["cycle_id"] = "test-cycle"
PipelineStateMixin._state["status"] = "idle"

print("Saving state...")
PipelineStateMixin.save_state()

# Reload to check if it actually saved
PipelineStateMixin.load_state()
print(f"Reloaded cycle_id: {PipelineStateMixin._state.get('cycle_id')}")

