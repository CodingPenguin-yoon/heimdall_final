FROM docker:29.6.1-cli AS docker-cli

FROM python:3.14.6-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins

WORKDIR /opt/heimdall

COPY pyproject.toml ./
COPY src ./src

RUN python -m pip install .

CMD ["uvicorn", "--factory", "heimdall.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
