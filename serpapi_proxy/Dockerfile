FROM python:3.11-slim
WORKDIR /app
COPY serpapi_proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY serpapi_proxy/ ./serpapi_proxy/
ENV PORT=8001
CMD ["python", "-m", "serpapi_proxy"]