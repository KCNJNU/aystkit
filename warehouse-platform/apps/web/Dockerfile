FROM node:24-bookworm-slim AS build
WORKDIR /workspace
COPY package.json package-lock.json ./
COPY apps/api/package.json apps/api/package.json
COPY apps/web/package.json apps/web/package.json
RUN npm ci
COPY apps/web apps/web
ARG APP_VERSION=0.0.0-dev
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ENV VITE_API_BASE_URL="" VITE_HEALTH_API_BASE_URL="" VITE_APP_VERSION=$APP_VERSION
RUN npm run build --workspace=@cleaning-robot/web

FROM nginxinc/nginx-unprivileged:1.29-alpine AS runtime
ARG APP_VERSION=0.0.0-dev
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
LABEL org.opencontainers.image.title="Warehouse Web" \
      org.opencontainers.image.version="$APP_VERSION" \
      org.opencontainers.image.revision="$GIT_COMMIT" \
      org.opencontainers.image.created="$BUILD_TIME"
ENV NEST_UPSTREAM=backend:3000 DOLLAR=$ \
    APP_VERSION=$APP_VERSION GIT_COMMIT=$GIT_COMMIT BUILD_TIME=$BUILD_TIME
COPY --from=build /workspace/apps/web/dist /usr/share/nginx/html
COPY apps/web/nginx/default.conf.template /etc/nginx/templates/default.conf.template
USER 101
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"]
