FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY k8s_mcp.py .

EXPOSE 8080

CMD ["python", "/app/k8s_mcp.py"]
