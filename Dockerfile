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

# 將內建故事情節 backup 到模板目錄（volume mount 會蓋過 results/checkpoints）
RUN if [ -d /app/generative_agents/results/checkpoints ]; then \
      mkdir -p /app/story_templates && \
      cp -r /app/generative_agents/results/checkpoints/* /app/story_templates/ 2>/dev/null || true; \
    fi

# 啟動 script：copy 內建故事去 volume（如果唔存在），然後 start server
RUN echo '#!/bin/bash\n\
DEST="/app/generative_agents/results/checkpoints"\n\
SRC="/app/story_templates"\n\
mkdir -p "$DEST"\n\
mkdir -p /app/generative_agents/results/.hf_cache\n\
if [ -d "$SRC" ]; then\n\
  for d in "$SRC"/*/; do\n\
    name=$(basename "$d")\n\
    if [ ! -d "$DEST/$name" ]; then\n\
      echo "Copying built-in story: $name"\n\
      cp -r "$d" "$DEST/"\n\
    fi\n\
  done\n\
fi\n\
cd /app/generative_agents && exec gunicorn story_weaver.gameui.game_server:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 300\n\
' > /app/start.sh && chmod +x /app/start.sh

EXPOSE 5001
CMD ["/app/start.sh"]
