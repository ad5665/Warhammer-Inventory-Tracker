FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ARG WH40K_BUILD_VERSION
ENV WH40K_BUILD_VERSION=${WH40K_BUILD_VERSION}
ENV WH40K_AUTH_ENABLED=false
VOLUME ["/app/data"]
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port \"${WH40K_PORT:-${PORT:-8000}}\""]
