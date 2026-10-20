from __future__ import annotations

import os
from dataclasses import dataclass, field

from .llm import BaseLLM
from .mcp_client import MCPGateway


@dataclass
class Settings:
    data_mode: str = "replay"
    max_rewrites: int = 1

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = env if env is not None else os.environ
        return cls(data_mode=env.get("DATA_MODE", "replay"), max_rewrites=int(env.get("MAX_REWRITES", "1")))


@dataclass
class Deps:
    mcp: MCPGateway
    llm: BaseLLM
    settings: Settings = field(default_factory=Settings)
