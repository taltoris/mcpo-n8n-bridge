FROM python:3.11-slim

WORKDIR /app
RUN pip install fastapi uvicorn aiohttp pydantic

COPY bridge.py /app/bridge.py
CMD ["python", "/app/bridge.py"]
