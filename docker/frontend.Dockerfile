# syntax=docker/dockerfile:1.7
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
# Vite inlines these at build time, so they are build arguments rather than runtime environment:
# a bundle is built for one deployment's error tracker and analytics sink. With none set the
# build ships neither -- the dynamic import of the error SDK is then dead code and is dropped.
ARG VITE_SENTRY_DSN=""
ARG VITE_ANALYTICS_ENDPOINT=""
ARG VITE_RELEASE="dev"
ARG VITE_DEPLOY_ENV="production"
RUN npm run build
# The source maps are built `hidden`: they are kept here for upload to the error tracker at
# deploy time and are deliberately not copied into the image that serves the bundle.


FROM nginxinc/nginx-unprivileged:1.29-alpine AS runtime
# The entrypoint runs envsubst over templates; the filter keeps nginx's own $variables intact.
# LOCAL_RESOLVERS opts into the script that reads the container's nameservers into
# NGINX_LOCAL_RESOLVERS, which the template needs to re-resolve the API upstream.
ENV API_UPSTREAM=api:8000
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1
# Set HSTS_HEADER to "max-age=31536000; includeSubDomains" when the deploy terminates TLS.
ENV HSTS_HEADER=""
ENV NGINX_ENVSUBST_FILTER="API_UPSTREAM|NGINX_LOCAL_RESOLVERS|HSTS_HEADER"
COPY --from=build /app/dist /usr/share/nginx/html
# Maps are not served: a visitor's browser never asks for them, and the application source
# should not be published next to the bundle.
RUN find /usr/share/nginx/html -name '*.map' -delete
COPY docker/nginx.conf.template /etc/nginx/templates/default.conf.template
EXPOSE 8080
