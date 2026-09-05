FROM python:3.12-slim

# espeak-ng is the local TTS engine. With it present the container produces real
# speech; without it the pipeline still runs, on a placeholder tone that is
# deliberately not mistakable for a voice.
RUN apt-get update \
 && apt-get install -y --no-install-recommends espeak-ng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
