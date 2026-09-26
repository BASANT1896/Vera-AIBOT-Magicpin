FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080

# Single worker process is REQUIRED — see storage.py's module docstring.
# --threads gives real concurrency for the judge's up-to-10-req/s load
# without risking two SQLite connections/two in-memory views diverging.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "35", "-b", "0.0.0.0:8080", "bot:app"]
