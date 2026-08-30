FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
COPY assets ./assets
RUN pip install --no-cache-dir .
USER 65532:65532
CMD ["capacity-api"]
