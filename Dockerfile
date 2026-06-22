FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg is required by yt-dlp to merge video+audio streams and extract audio.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# spotdl (Spotify -> YouTube audio) in its own venv so its yt-dlp pin never
# clashes with the bot's. Exposed at /opt/spotdl/bin/spotdl.
RUN python -m venv /opt/spotdl \
    && /opt/spotdl/bin/pip install --no-cache-dir spotdl \
    && chmod -R a+rx /opt/spotdl

COPY bot/ ./bot/
# Optional legacy statistics dump, imported automatically on first start with an
# empty DB. The bracket glob makes the copy a no-op when the file is absent
# (a fresh install has none), so the build never fails on it.
COPY info.tx[t] .

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 30080
CMD ["python", "-m", "bot"]
