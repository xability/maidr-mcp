FROM python:3.12-slim

# The MCP Registry lists the image under server.json's name only if the image carries it.
LABEL io.modelcontextprotocol.server.name="io.github.xability/maidr-mcp"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    PORT=8000

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home maidr

USER maidr
EXPOSE 8000
# One process: the relay keeps each chart's queue in memory.
CMD ["maidr-mcp", "--host", "0.0.0.0"]
