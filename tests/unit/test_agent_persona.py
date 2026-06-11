import pytest
from pydantic import ValidationError
from app.schemas.agent_persona import AgentPersonaCreate, AgentPersonaUpdate
from app.db.agent_persona_store import _default_avatar_for_role, create_persona, list_personas, get_persona
import uuid

def test_default_avatar_for_technical_role():
    """Verify that TECHNICAL role defaults return correct avatar configuration."""
    avatar = _default_avatar_for_role("TECHNICAL")
    assert avatar["skin_color"] == "#f5deb3"
    assert avatar["hair_color"] == "#2d3748"
    assert avatar["outfit_color"] == "#0891b2"
    assert avatar["accent_color"] == "#06b6d4"
    assert avatar["accessory"] == "glasses"

def test_system_prompt_length_validation():
    """Verify system prompt limits (10 to 50000 characters)."""
    # 1. Standard prompt (valid)
    valid_data = {
        "name": "tech_analyst",
        "display_name": "Tech Analyst",
        "role": "TECHNICAL",
        "system_prompt": "This is a valid system prompt.",
        "voice_pitch": 1.0,
        "voice_rate": 1.0,
    }
    persona = AgentPersonaCreate(**valid_data)
    assert persona.system_prompt == "This is a valid system prompt."

    # 2. Short prompt (invalid, < 10 characters)
    short_data = valid_data.copy()
    short_data["system_prompt"] = "Too short"
    with pytest.raises(ValidationError) as exc_info:
        AgentPersonaCreate(**short_data)
    assert "system_prompt" in str(exc_info.value)

    # 3. Exactly 50,000 characters (valid)
    long_valid_prompt = "a" * 50000
    long_valid_data = valid_data.copy()
    long_valid_data["system_prompt"] = long_valid_prompt
    persona_long = AgentPersonaCreate(**long_valid_data)
    assert len(persona_long.system_prompt) == 50000

    # 4. Over 50,000 characters (invalid)
    too_long_prompt = "a" * 50001
    too_long_data = valid_data.copy()
    too_long_data["system_prompt"] = too_long_prompt
    with pytest.raises(ValidationError) as exc_info:
        AgentPersonaCreate(**too_long_data)
    assert "system_prompt" in str(exc_info.value)

@pytest.mark.asyncio
async def test_store_operations():
    """Verify basic CRUD store operations for agent personas."""
    test_id = str(uuid.uuid4())
    data = {
        "id": test_id,
        "name": "test_agent_crud",
        "display_name": "Test Agent CRUD",
        "role": "TECHNICAL",
        "system_prompt": "This is a valid system prompt for testing.",
    }
    
    try:
        # Create
        created = await create_persona(data)
        assert created["id"] == test_id
        assert created["name"] == "test_agent_crud"
        assert created["avatar_config"]["outfit_color"] == "#0891b2" # TECHNICAL default
        
        # Get
        fetched = await get_persona(test_id)
        assert fetched is not None
        assert fetched["name"] == "test_agent_crud"
        
        # List
        all_personas = await list_personas()
        assert any(p["id"] == test_id for p in all_personas)
    finally:
        from app.db.agent_persona_store import delete_persona
        await delete_persona(test_id)
