# Jobrunner in a container.
#
# Not a deployment artifact — CLAUDE.md §1 says this runs on localhost for one
# user, and nothing here pushes to a registry. It exists so CI can prove the
# project installs from nothing on every commit, which is exactly what a
# developer machine with a warm virtualenv cannot tell you.
#
# The native side is the reason this is worth having: WeasyPrint loads Pango
# through cffi at import time and Playwright needs a real browser. Neither is
# something pip resolves, so both have to be installed explicitly and both have
# broken this project before.

FROM python:3.12-slim

# - libpango / libcairo / libgdk-pixbuf: WeasyPrint, loaded via cffi at import
# - libffi: cffi itself
# - build-essential: some wheels still compile on slim
# - postgresql-client: psql, for migrations and debugging against the service
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libffi-dev \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Unbuffered so container logs appear in order; no .pyc to keep the layer thin.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Outside $HOME so the browser survives the switch to the non-root user.
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

# Dependency metadata first: this layer is the expensive one, and it only
# rebuilds when the dependencies actually change rather than on every edit.
COPY pyproject.toml README.md ./
# The editable install needs the packages to exist, so stub them for this layer
# and let the real COPY below replace them.
RUN mkdir -p packages apps \
    && touch packages/__init__.py apps/__init__.py \
    && python -m pip install --upgrade pip \
    && pip install -e ".[dev]"

# --with-deps pulls Chromium's shared libraries. Only chromium: the adapters
# never touch firefox or webkit, and each extra browser is ~300MB.
RUN python -m playwright install --with-deps chromium

# The API has no authentication and can submit real job applications. It
# refuses non-loopback callers itself (apps/api/middleware.py), but running as
# root as well would be gratuitous.
#
# Note what is *not* here: `chown -R` over /opt/playwright. Changing the mode
# of a file rewrites it into a new layer, so recursing over a 1.3GB browser
# added 947MB of duplicates to the image. Chromium only has to be readable and
# executable, which it already is — root can own it.
RUN useradd --create-home --uid 1000 jobrunner

COPY --chown=jobrunner:jobrunner . .

# Reinstall now that the real sources are present, so entry points resolve.
RUN pip install -e ".[dev]"

# Only the directories written to need to belong to the user, and only the
# directories themselves — no -R. /app itself is included so pytest can create
# its cache there.
RUN mkdir -p /app/storage /app/.secrets \
    && chown jobrunner:jobrunner /app /app/storage /app/.secrets \
    && chmod 700 /app/.secrets

USER jobrunner

EXPOSE 8000

# No CMD that starts anything by default. This image is a verification
# artifact and a shell for `make api` / `make worker`; starting a worker
# implicitly would have it reach for a database that may not be there.
CMD ["python", "-c", "import apps.api.main; print('jobrunner image ok')"]
