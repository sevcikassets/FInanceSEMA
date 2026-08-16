FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Fresh whenever source actually changed (this layer sits after COPY, so
# Docker's cache invalidates it exactly when app/ changes) - lets /version
# answer "is this deploy actually running the code I just pushed" without
# needing git available in the build context (the context is ./api, not the
# repo root, so a real commit SHA isn't reachable here).
RUN date -u +"%Y-%m-%d %H:%M UTC" > BUILD_TIME

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
