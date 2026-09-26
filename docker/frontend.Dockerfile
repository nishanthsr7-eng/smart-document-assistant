# syntax=docker/dockerfile:1.7
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build


FROM nginxinc/nginx-unprivileged:1.29-alpine AS runtime
# The entrypoint runs envsubst over templates; the filter keeps nginx's own $variables intact.
# LOCAL_RESOLVERS opts into the script that reads the container's nameservers into
# NGINX_LOCAL_RESOLVERS, which the template needs to re-resolve the API upstream.
ENV API_UPSTREAM=api:8000
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1
ENV NGINX_ENVSUBST_FILTER="API_UPSTREAM|NGINX_LOCAL_RESOLVERS"
COPY --from=build /app/dist /usr/share/nginx/html
COPY docker/nginx.conf.template /etc/nginx/templates/default.conf.template
EXPOSE 8080
