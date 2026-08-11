from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from http.client import RemoteDisconnected
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from heimdall.deployments.worker import RuntimeFailure


class RouteProbe(Protocol):
    def probe(
        self,
        url: str,
        *,
        timeout_seconds: float,
        heartbeat: Callable[[], None],
    ) -> None: ...

    def observe(
        self,
        url: str,
        *,
        timeout_seconds: float,
        heartbeat: Callable[[], None],
    ) -> GatewayObservation: ...


@dataclass(frozen=True, slots=True)
class GatewayObservation:
    reachable: bool
    deployment_id: UUID | None


class HttpRouteProbe:
    def probe(
        self,
        url: str,
        *,
        timeout_seconds: float,
        heartbeat: Callable[[], None],
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            heartbeat()
            try:
                request = Request(url, method="GET")
                with urlopen(request, timeout=min(2, timeout_seconds)) as response:
                    if response.status < 500:
                        return
            except HTTPError as error:
                if error.code < 500:
                    return
            except (URLError, TimeoutError, RemoteDisconnected, ConnectionError):
                pass
            time.sleep(0.25)
        raise RuntimeFailure("ACTIVATION", "GATEWAY_ROUTE_PROBE_FAILED")

    def observe(
        self,
        url: str,
        *,
        timeout_seconds: float,
        heartbeat: Callable[[], None],
    ) -> GatewayObservation:
        heartbeat()
        try:
            request = Request(url, method="GET")
            with urlopen(request, timeout=min(2, timeout_seconds)) as response:
                marker = response.headers.get("X-Heimdall-Deployment-Id")
        except HTTPError as error:
            marker = error.headers.get("X-Heimdall-Deployment-Id")
            error.close()
        except (URLError, TimeoutError, RemoteDisconnected, ConnectionError):
            return GatewayObservation(False, None)
        if marker is None or marker == "none":
            return GatewayObservation(True, None)
        try:
            deployment_id = UUID(marker)
        except (ValueError, AttributeError):
            return GatewayObservation(True, None)
        return GatewayObservation(True, deployment_id)
