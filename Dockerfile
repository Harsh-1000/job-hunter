# ---- base: runtime dependencies --------------------------------------------------------------
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=180 \
    TZ=Asia/Kolkata \
    HOME=/tmp
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- test: the image only builds if every test passes ---------------------------------------
FROM base AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY app ./app
COPY tests ./tests
COPY ai/server.py ./ai/server.py
COPY main.py ./
RUN python -m pytest -q -p no:cacheprovider && touch /tmp/tests-passed

# ---- runtime ---------------------------------------------------------------------------------
FROM base AS runtime
# depending on the test stage forces it to run (BuildKit skips stages nothing uses)
COPY --from=test /tmp/tests-passed /tmp/tests-passed
COPY app ./app
COPY main.py ./
# Healthy while the bot keeps touching data/heartbeat (every 30 s).
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(time.time() - os.path.getmtime('data/heartbeat') > 150)"
CMD ["python", "main.py"]
