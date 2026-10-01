FROM denoland/deno:bin-2.5.4 AS deno
FROM python:3.12-slim-bookworm
COPY --from=deno /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/bootstrap \
    && /opt/bootstrap/bin/pip install --no-cache-dir 'yt-dlp[default]'
WORKDIR /app
COPY app.py /app/app.py
COPY web /app/web
EXPOSE 8080
CMD ["python", "-u", "app.py"]
