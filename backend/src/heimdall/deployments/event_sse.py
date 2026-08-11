from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from heimdall.deployments.event_stream import (
    DeploymentEventStreamEnd,
    DeploymentEventStreamError,
)
from heimdall.deployments.models import DeploymentEvent
from heimdall.deployments.schemas import DeploymentEventRead
from heimdall.deployments.service import DeploymentEventStreamSubscription


async def deployment_event_sse_events(
    subscription: DeploymentEventStreamSubscription,
) -> AsyncIterator[str]:
    try:
        ready = subscription.ready
        yield _sse(
            "ready",
            {"deploymentId": str(ready.deployment_id), "after": ready.after_id},
        )
        while True:
            try:
                event = await asyncio.to_thread(subscription.receive)
            except DeploymentEventStreamError as error:
                yield _sse("stream-error", {"code": error.code})
                break
            if event is None:
                yield ": keepalive\n\n"
                continue
            if isinstance(event, DeploymentEvent):
                yield _sse(
                    "deployment-event",
                    DeploymentEventRead.from_event(event).model_dump(mode="json", by_alias=True),
                    event_id=event.id,
                )
                continue
            if isinstance(event, DeploymentEventStreamEnd):
                yield _sse("end", {"reason": event.reason})
                break
    finally:
        await asyncio.to_thread(subscription.close)


def _sse(event: str, data: dict[str, object], event_id: int | None = None) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {payload}\n\n"
