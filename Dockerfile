FROM python:3.11-slim

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY execution/ ./execution/
COPY layout_preview.html ./
COPY ad_templates/ ./ad_templates/
COPY .env.example ./

# Persistent data dir (mount a volume here in production)
RUN mkdir -p .tmp

# Server reads PORT from env; Railway/Render/Fly inject this
ENV PORT=8766
EXPOSE 8766

CMD ["python3", "-u", "execution/brand_scraper_server.py"]
