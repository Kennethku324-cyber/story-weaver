FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV DEEPSEEK_API_KEY=""
ENV DEEPSEEK_BASE_URL="https://api.deepseek.com"
ENV PYTHONPATH=/app/generative_agents

EXPOSE 7860
CMD ["python", "start_hf.py"]
