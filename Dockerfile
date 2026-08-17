# Reproduction image, not a distribution channel: rebuilds the exact checkout
# environment (pinned node, pinned promptfoo, uv-locked python deps) so a run
# can be reproduced anywhere. Not published to a registry on purpose — an
# image nobody rebuilds is a supply chain with extra steps (HANDBOOK §18).
#
#   docker build -t prompt-eval .
#   docker run --rm prompt-eval                          # doctor (default)
#   docker run --rm -e ANTHROPIC_API_KEY prompt-eval round
#
# Expected on a keyless run: doctor prints the ANTHROPIC_API_KEY FAILED line
# and everything else green — that IS the correct result.
FROM node:24-bookworm-slim

# uv installs into /usr/local/bin (standalone binary, no system python dance).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependency layers first, so source edits don't re-download the world.
COPY package.json package-lock.json ./
RUN npm ci

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# The engine + project layer. .dockerignore keeps .env (the live key) and
# per-run outputs out of the build context — a credential must never bake
# into a layer.
COPY . .
RUN uv sync --frozen

ENTRYPOINT ["uv", "run", "prompt-eval"]
CMD ["doctor"]
