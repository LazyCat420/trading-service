"""
agent_persona.py — Pydantic models for Agent Studio persona configuration.

Validates persona data before storage and API responses.
"""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Literal
import uuid


class AvatarConfig(BaseModel):
    """Visual customization for agent rendering (Three.js + SVG)."""
    skin_color: str = Field("#fde68a", description="Skin tone hex color")
    hair_color: str = Field("#1e293b", description="Hair hex color")
    outfit_color: str = Field("#3b82f6", description="Primary outfit hex color")
    accent_color: str = Field("#f59e0b", description="Secondary accent hex color")
    accessory: Optional[str] = Field(
        None,
        description="Accessory type: glasses, hat, tie, labcoat, headset, none",
    )


class AgentPersona(BaseModel):
    """Full agent persona profile for the Agent Studio."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique persona ID")
    name: str = Field(..., description="Internal agent name (e.g., 'Dr. Aris')")
    display_name: str = Field(..., description="Display name shown in UI")
    role: str = Field(..., max_length=50, description="Agent role key (e.g. QUANT, CUSTOM_AUDITOR)")
    system_prompt: str = Field(..., description="Full system prompt for LLM calls")
    voice_pitch: float = Field(1.0, ge=0.5, le=2.0, description="TTS voice pitch multiplier")
    voice_rate: float = Field(1.0, ge=0.5, le=2.0, description="TTS voice rate multiplier")
    voice_accent: Optional[str] = Field(None, description="BCP-47 language tag (e.g. en-GB) for TTS voice selection")
    avatar_config: AvatarConfig = Field(default_factory=AvatarConfig, description="Visual customization")
    allowed_tools: list[str] = Field(default_factory=list, description="Tool IDs this agent can use")
    execution_order: int = Field(1, ge=1, le=10, description="Pipeline execution priority (1=first)")
    is_active: bool = Field(True, description="Whether agent participates in cycles")
    max_tokens: int = Field(8192, ge=128, le=65536, description="Max LLM response tokens")
    temperature: float = Field(0.7, ge=0.0, le=2.0, description="LLM temperature")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AgentPersonaCreate(BaseModel):
    """Request body for creating a new persona."""
    name: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=100)
    role: str = Field(..., min_length=1, max_length=50)
    system_prompt: str = Field(..., min_length=10, max_length=50000)
    voice_pitch: float = Field(1.0, ge=0.5, le=2.0)
    voice_rate: float = Field(1.0, ge=0.5, le=2.0)
    voice_accent: Optional[str] = None
    avatar_config: Optional[AvatarConfig] = None
    allowed_tools: list[str] = Field(default_factory=list)
    execution_order: int = Field(1, ge=1, le=10)
    is_active: bool = True
    max_tokens: int = Field(8192, ge=128, le=65536)
    temperature: float = Field(0.7, ge=0.0, le=2.0)


class AgentPersonaUpdate(BaseModel):
    """Request body for updating an existing persona (all fields optional)."""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    display_name: Optional[str] = Field(None, min_length=1, max_length=100)
    role: Optional[str] = Field(None, min_length=1, max_length=50)
    system_prompt: Optional[str] = Field(None, min_length=10, max_length=50000)
    voice_pitch: Optional[float] = Field(None, ge=0.5, le=2.0)
    voice_rate: Optional[float] = Field(None, ge=0.5, le=2.0)
    voice_accent: Optional[str] = None
    avatar_config: Optional[AvatarConfig] = None
    allowed_tools: Optional[list[str]] = None
    execution_order: Optional[int] = Field(None, ge=1, le=10)
    is_active: Optional[bool] = None
    max_tokens: Optional[int] = Field(None, ge=128, le=65536)
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
