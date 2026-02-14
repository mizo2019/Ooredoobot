FROM python:3.11-slim

WORKDIR /app

# Install dependencies directly
RUN pip install --no-cache-dir python-telegram-bot requests

# Copy all files
COPY . .

CMD ["python", "ooredoo.py"]
