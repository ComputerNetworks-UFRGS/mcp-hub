FROM python:3.11-slim

WORKDIR /app

# Install dependencies from both projects
COPY backend/requirements.txt     ./requirements_backend.txt
COPY magentic_ui/requirements.txt ./requirements_ui.txt
RUN pip install --no-cache-dir \
    -r requirements_backend.txt \
    -r requirements_ui.txt

# Copy source code
COPY backend/    ./backend/
COPY magentic_ui/ ./magentic_ui/

WORKDIR /app/magentic_ui

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
