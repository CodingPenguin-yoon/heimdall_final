from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from heimdall.deployments.service import ServiceLogStreamSubscription
from heimdall.runtime.logs import (
    ServiceLogError,
    ServiceLogStreamEnd,
    ServiceLogStreamLine,
)


async def service_log_sse_events(
    subscription: ServiceLogStreamSubscription,
) -> AsyncIterator[str]:
    try:
        yield _sse(
            "ready",
            {
                "deploymentId": str(subscription.ready.deployment_id),
                "services": list(subscription.ready.services),
                "serviceName": subscription.ready.service_name,
                "connectedAt": subscription.ready.connected_at.isoformat().replace("+00:00", "Z"),
            },
        )
        while True:
            try:
                event = await asyncio.to_thread(subscription.receive)
            except ServiceLogError as error:
                yield _sse("stream-error", {"code": error.code})
                break
            if event is None:
                yield ": keepalive\n\n"
                continue
            if isinstance(event, ServiceLogStreamLine):
                yield _sse(
                    "log",
                    {
                        "timestamp": event.line.timestamp,
                        "stream": event.line.stream.value,
                        "message": event.line.message,
                        "truncated": event.truncated,
                    },
                )
                continue
            if isinstance(event, ServiceLogStreamEnd):
                yield _sse("end", {"reason": event.reason})
                break
    finally:
        subscription.close()


def _sse(event: str, data: dict[str, object]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"
