from __future__ import annotations

from heimdall.deployments.models import Deployment
from heimdall.runtime.models import RuntimeDeployment


def render_nginx_config(deployment: Deployment, runtime: RuntimeDeployment) -> str:
    generation = deployment.id.hex[:12]
    locations: list[str] = []
    for route in sorted(runtime.routes, key=lambda item: len(item.path), reverse=True):
        service = next(item for item in runtime.services if item.name == route.service)
        alias = f"{service.name}-g-{generation}"
        if route.path == "/":
            locations.append(_location("/", alias, service.internal_port))
        else:
            locations.append(_location(f"= {route.path}", alias, service.internal_port))
            locations.append(_location(f"^~ {route.path}/", alias, service.internal_port))
    return "\n".join(
        [
            f"# deployment: {deployment.id}",
            "server {",
            "    listen 8080;",
            "    server_name _;",
            "    proxy_hide_header X-Heimdall-Deployment-Id;",
            f'    add_header X-Heimdall-Deployment-Id "{deployment.id}" always;',
            *locations,
            "}",
            "",
        ]
    )


def default_nginx_config() -> str:
    return (
        'server { listen 8080; add_header X-Heimdall-Deployment-Id "none" always; '
        "location / { return 503; } }\n"
    )


def _location(location: str, alias: str, port: int) -> str:
    return "\n".join(
        [
            f"    location {location} {{",
            f"        proxy_pass http://{alias}:{port};",
            "        proxy_set_header Host $host;",
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "        proxy_set_header X-Forwarded-Proto $scheme;",
            "    }",
        ]
    )
