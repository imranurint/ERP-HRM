"""Actor context — who is acting, and what the audit log needs to know.

Threaded through every application service so they can authorise and audit
without touching HTTP. A batch import or a scheduled job builds one of these
directly; it is not an HTTP concept.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(slots=True)
class ActorContext:
    user_id: UUID | None = None
    #: The acting user's own employee record, when they have one. Self-service
    #: scoping and the self-approval guard both need it.
    employee_id: UUID | None = None
    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
