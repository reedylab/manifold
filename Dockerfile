FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core iputils-ping && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir yt-dlp
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x entrypoint.sh
CMD ["./entrypoint.sh"]
