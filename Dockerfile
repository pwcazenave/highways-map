FROM docker.io/python:3.13-alpine

# Install uv from the upstream image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/


# Install the application
WORKDIR /app
COPY . /app
# Using --system in containerised environments is fine: https://docs.astral.sh/uv/pip/environments/#using-arbitrary-python-environments
RUN uv pip install --system .

ENV PORT=5000
ENV HOST=0.0.0.0
ENV UV_PYTHON=/usr/local/bin/python3
CMD ["uv", "run", "--", "python3", "/app/src/highwaysmap/main.py"]
