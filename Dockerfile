FROM python:3.12-slim

# pyodbc needs unixodbc; lp/lpstat come with cups-client for the CUPS transport
RUN apt-get update && apt-get install -y --no-install-recommends \
        unixodbc cups-client fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/platen
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
