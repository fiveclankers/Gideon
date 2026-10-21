"""Shared engine interface for Gideon. Antigravity stays default."""
from __future__ import annotations

from typing import Optional, Protocol


class BaseEngine(Protocol):
    name: str
    is_turn_active: bool
    current_tool: Optional[str]

    async def start(self) -> None: ...
    async def send_prompt(self, text: str) -> None: ...
    async def interrupt(self, hard: bool = False) -> bool: ...
    async def close(self) -> None: ...
