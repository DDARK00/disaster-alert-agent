from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import Settings  # noqa: F401  (기존 import 경로 유지)

from .llm import BaseLLM
from .mcp_client import MCPGateway


@dataclass
class Deps:
    mcp: MCPGateway
    llm: BaseLLM
    settings: Settings = field(default_factory=Settings)
