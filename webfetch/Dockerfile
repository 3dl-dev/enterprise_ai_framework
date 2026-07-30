# Standard library only — no pip install, so this image builds offline and adds no
# third-party licence to the bundle. See app.py for why the service exists at all.
FROM python:3.12-slim

WORKDIR /app
COPY app.py .

EXPOSE 3002
CMD ["python", "-u", "app.py"]
