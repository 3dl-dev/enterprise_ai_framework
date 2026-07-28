FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "mcp>=1.28,<2"
COPY app.py .

EXPOSE 8080
CMD ["python", "app.py"]
