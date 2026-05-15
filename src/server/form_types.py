"""Reusable form type aliases for FastAPI form parameters."""

from __future__ import annotations

from typing import Annotated

from fastapi import Form

StrForm = Annotated[str, Form(...)]
IntForm = Annotated[int, Form(...)]
OptStrForm = Annotated[str | None, Form()]
