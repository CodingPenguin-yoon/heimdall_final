FROM node:24.13.0-alpine AS build

ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH

RUN corepack enable \
    && corepack prepare pnpm@11.13.0 --activate

WORKDIR /opt/heimdall-ui

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

FROM nginx:1.29.8-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /opt/heimdall-ui/dist /usr/share/nginx/html
