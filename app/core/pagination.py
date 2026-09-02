"""Pagination primitives.

Two strategies, both supported everywhere collections are returned:

* offset pagination (`page`, `page_size`) — convenient for UIs that show
  page numbers;
* keyset/cursor pagination (`cursor`, `page_size`) — stable under concurrent
  inserts and the only safe option for large scans such as a full year of
  attendance records.

Because identifiers are UUIDv7 (time ordered), the cursor is simply the last
row's id: `WHERE id > :cursor ORDER BY id` is both correct and index-friendly.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, Field

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


class PageParams(BaseModel):
    """Offset/keyset pagination inputs, supplied as query parameters."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    cursor: str | None = None

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size

    def cursor_uuid(self) -> UUID | None:
        return decode_cursor(self.cursor)


def page_params(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> PageParams:
    """FastAPI dependency producing `PageParams`."""
    return PageParams(page=page, page_size=page_size, cursor=cursor)


def encode_cursor(value: UUID) -> str:
    return base64.urlsafe_b64encode(value.bytes).decode("ascii").rstrip("=")


def decode_cursor(value: str | None) -> UUID | None:
    if not value:
        return None
    padded = value + "=" * (-len(value) % 4)
    try:
        return UUID(bytes=base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error) as exc:
        from app.core.exceptions import ValidationError

        raise ValidationError("Malformed pagination cursor.", details={"cursor": value}) from exc


class Page[T](BaseModel):
    """A page of results plus everything a client needs to fetch the next one."""

    items: list[T]
    total: int | None = Field(
        default=None,
        description="Total matching rows. Omitted for cursor pagination, where "
        "counting the full set would defeat the purpose.",
    )
    page: int
    page_size: int
    next_cursor: str | None = None
    has_more: bool = False

    @classmethod
    def build(
        cls,
        items: Sequence[T],
        params: PageParams,
        *,
        total: int | None = None,
        next_cursor: str | None = None,
        has_more: bool = False,
    ) -> Page[T]:
        return cls(
            items=list(items),
            total=total,
            page=params.page,
            page_size=params.page_size,
            next_cursor=next_cursor,
            has_more=has_more,
        )
