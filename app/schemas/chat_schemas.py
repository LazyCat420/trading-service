from pydantic import BaseModel, Field, validator
from typing import List, Optional, Dict, Any, Union
import re

DISALLOWED_IDENTIFIER_PATTERN = re.compile(r'[\x00]|\.\./|\.\.\\')

class ToolSchemaParameters(BaseModel):
    type: str
    properties: Dict[str, Any]
    required: Optional[List[str]] = None

class ToolSchemaSchema(BaseModel):
    name: str
    description: str
    isCustom: Optional[bool] = Field(None, alias="_isCustom")
    parameters: Optional[ToolSchemaParameters] = None

class ChatMessageContentSchema(BaseModel):
    type: str
    text: Optional[str] = None
    image_url: Optional[Dict[str, str]] = None

class ToolCallSchema(BaseModel):
    id: Optional[str] = None
    name: str
    args: Dict[str, Any]

class ChatMessageSchema(BaseModel):
    role: str
    content: Union[str, List[ChatMessageContentSchema]]
    name: Optional[str] = None
    images: Optional[List[str]] = None
    deleted: Optional[bool] = None
    toolCalls: Optional[List[ToolCallSchema]] = None
    thinking: Optional[str] = None
    thinkingSignature: Optional[str] = None

class ChatRequestSchema(BaseModel):
    provider: str
    model: Optional[str] = None
    messages: List[ChatMessageSchema]
    conversationId: Optional[str] = None
    agentSessionId: Optional[str] = None
    parentConversationId: Optional[str] = None
    parentAgentSessionId: Optional[str] = None
    conversationMeta: Optional[Dict[str, Any]] = None
    traceId: Optional[str] = None
    project: str = "any"
    username: str = "any"
    clientIp: Optional[str] = None
    agent: Optional[str] = None
    harness: Optional[str] = None
    topology: Optional[str] = None
    reasoningStrategy: Optional[str] = None

    # Generation options
    tools: Optional[List[ToolSchemaSchema]] = None
    temperature: Optional[float] = None
    maxTokens: Optional[int] = None
    topP: Optional[float] = None
    topK: Optional[int] = None
    frequencyPenalty: Optional[float] = None
    presencePenalty: Optional[float] = None
    stopSequences: Optional[List[str]] = None
    seed: Optional[Union[int, str]] = None
    minP: Optional[float] = None
    repeatPenalty: Optional[float] = None
    thinkingEnabled: Optional[bool] = None
    reasoningEffort: Optional[str] = None
    thinkingLevel: Optional[str] = None
    thinkingBudget: Optional[Union[int, str]] = None
    webSearch: Optional[Union[bool, str]] = None
    webFetch: Optional[bool] = None
    codeExecution: Optional[bool] = None
    urlContext: Optional[bool] = None
    verbosity: Optional[str] = None
    reasoningSummary: Optional[str] = None
    functionCallingEnabled: Optional[bool] = None
    agenticLoopEnabled: Optional[bool] = None
    enabledTools: Optional[List[str]] = None
    disabledTools: Optional[List[str]] = None
    minContextLength: Optional[int] = None
    evalBatchSize: Optional[int] = None
    forceImageGeneration: Optional[bool] = None
    responseFormat: Optional[Any] = None
    serviceTier: Optional[str] = None
    textOnly: Optional[bool] = None
    skipConversation: Optional[bool] = None
    autoApprove: Optional[bool] = None
    planFirst: Optional[bool] = None
    maxIterations: Optional[int] = None
    maxSubAgentIterations: Optional[int] = None
    agentContext: Optional[Any] = None
    workspaceRoot: Optional[str] = None
    enableCriticGate: Optional[bool] = None
    criticModel: Optional[str] = None
    reminderModel: Optional[str] = None
    reminderProvider: Optional[str] = None
    parallelToolCalls: Optional[bool] = None
    candidateCount: Optional[int] = None
    branchCount: Optional[int] = None
    responseMimeType: Optional[str] = None
    store: Optional[bool] = None
    mediaResolution: Optional[str] = None
    topLogprobs: Optional[int] = None
    responseLogprobs: Optional[bool] = None
    logprobs: Optional[int] = None

    @validator('provider', 'agentSessionId', 'parentAgentSessionId', 'harness', pre=True)
    def sanitize_strings(cls, v):
        if isinstance(v, str):
            v = v.replace('\x00', '')
            if DISALLOWED_IDENTIFIER_PATTERN.search(v):
                raise ValueError("String contains disallowed characters")
        return v
    
    class Config:
        extra = 'allow'

class GetTextQuerySchema(BaseModel):
    page: int = 1
    limit: int = Field(50, le=500)
    origin: Optional[str] = None
    search: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    from_date: Optional[str] = Field(None, alias="from")
    to_date: Optional[str] = Field(None, alias="to")

class GetConversationsQuerySchema(BaseModel):
    limit: int = Field(50, le=200)
    cursor: Optional[str] = None
    agent: Optional[str] = None
    type: str = "all"
    taskId: Optional[str] = None

class PostConversationMessagesBodySchema(BaseModel):
    messages: List[ChatMessageSchema] = Field(..., min_items=1)
    conversationMeta: Optional[Dict[str, Any]] = None

class PatchConversationBodySchema(BaseModel):
    title: Optional[str] = None
    messages: Optional[List[ChatMessageSchema]] = None
    systemPrompt: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None
