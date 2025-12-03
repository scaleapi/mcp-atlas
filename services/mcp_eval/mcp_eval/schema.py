"""Schema definitions for MCP evaluation."""

from typing import Dict, List, Literal, Optional, Union, Any
from pydantic import BaseModel, Field


class ToolCallSchema(BaseModel):
    """OpenAI Function Calling Schema."""

    type: Literal["function"]
    function: Dict[str, Any]


class ToolCall(BaseModel):
    """Tool call representation."""

    id: str
    type: Literal["function"]
    function: Dict[str, str]


class SystemMessage(BaseModel):
    """System message."""

    role: Literal["system"]
    content: str


class UserMessage(BaseModel):
    """User message."""

    role: Literal["user"]
    content: str


class AssistantMessage(BaseModel):
    """Assistant message."""

    role: Literal["assistant"]
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None


class TextContent(BaseModel):
    """Text content for tool outputs."""

    type: Literal["text"]
    text: str


class ToolCallOutputMessage(BaseModel):
    """Tool call output message."""

    role: Literal["tool"]
    tool_call_id: str
    content: List[TextContent] = Field(default_factory=list)


Message = Union[SystemMessage, UserMessage, AssistantMessage, ToolCallOutputMessage]


class RunAgentAPIRequestBody(BaseModel):
    """Request body for running MCP eval."""

    model: str
    messages: List[Message]
    enabled_tools: List[str] = Field(alias="enabledTools")
    max_turns: int = Field(20, alias="maxTurns")

    class Config:
        populate_by_name = True


class CallToolResponse(BaseModel):
    """Response from calling a tool."""

    content: List[TextContent] = Field(default_factory=list)
    is_error: bool = Field(False, alias="isError")

    class Config:
        populate_by_name = True


class ToolDefinition(BaseModel):
    """MCP tool definition."""

    name: str
    description: Optional[str] = None
    input_schema: Dict[str, Any] = Field(alias="inputSchema")
    server: Optional[str] = None
    disabled: Optional[bool] = None

    class Config:
        populate_by_name = True


class MCPTool(BaseModel):
    """MCP Tool schema."""

    name: str
    description: str
    input_schema: Dict[str, Any] = Field(alias="inputSchema")

    class Config:
        populate_by_name = True
