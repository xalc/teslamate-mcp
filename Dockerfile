FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml /app/
COPY src /app/src
RUN pip install --no-cache-dir .

USER 1001:1001
EXPOSE 8766 8767
ENTRYPOINT ["teslamate-mcp"]
