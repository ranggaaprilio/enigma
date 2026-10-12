# Stages:
#   dev     -- what docker-compose builds: the Vite dev server, source bind-mounted.
#   build   -- the locked install + `tsc -b && vite build` into /app/dist.
#   runtime -- the default (last) target: unprivileged nginx serving the static build. Vite
#              fingerprints every asset, so a release is cache-busted by index.html alone
#              (nginx.conf). Point a CDN at this origin, or serve /usr/share/nginx/html from one.

FROM node:22-alpine AS dev

WORKDIR /app

RUN corepack enable

COPY package.json pnpm-workspace.yaml .npmrc ./
RUN pnpm install || (pnpm approve-builds esbuild && pnpm install)

COPY index.html ./
COPY postcss.config.js ./
COPY tailwind.config.ts ./
COPY tsconfig*.json ./
COPY vite.config.ts ./
COPY src ./src

EXPOSE 5173

CMD ["node", "./node_modules/vite/bin/vite.js", "--host", "0.0.0.0"]


FROM node:22-alpine AS build

WORKDIR /app

RUN corepack enable

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc ./
RUN pnpm install --frozen-lockfile

COPY index.html postcss.config.js tailwind.config.ts vite.config.ts tsconfig*.json ./
COPY src ./src

# Public, not secret: Vite inlines it into the bundle. The API origin the browser talks to.
# Required: unset, it inlines "" (not nullish, so api.ts's localhost fallback never applies) and
# every API call would hit nginx and get index.html back.
ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN test -n "$VITE_API_BASE_URL" \
        || { echo "build with --build-arg VITE_API_BASE_URL=https://api.<host>" >&2; exit 1; }
RUN pnpm run build


FROM nginxinc/nginx-unprivileged:1.30-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

# nginx-unprivileged runs as uid 101 and listens on 8080.
USER 101

EXPOSE 8080
