FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN npm i -g @openai/codex \
    && codex --version

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

ENV PORT=8000
ENV CODEX_HOME=/codex-home
RUN mkdir -p /codex-home
EXPOSE 8000

CMD ["python", "app.py"]
