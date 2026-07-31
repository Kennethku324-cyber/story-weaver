FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV DEEPSEEK_API_KEY=""
ENV DEEPSEEK_BASE_URL="https://api.deepseek.com"
ENV PYTHONPATH=/app/generative_agents
ENV PORT=5001

# 確保 checkpoints / results 目錄存在
RUN mkdir -p /app/generative_agents/results/checkpoints

EXPOSE 5001
CMD cd generative_agents && exec gunicorn story_weaver.gameui.game_server:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 300
