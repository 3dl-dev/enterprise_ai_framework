FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
# --no-proxy-headers is load-bearing, not tidiness.
#
# The portal decides who you are from headers the oauth2-proxy sidecar sets, and trusts
# them only when the request arrives from 127.0.0.1 — the sidecar shares this pod's
# network namespace, nothing else can reach loopback. uvicorn's proxy-header handling is
# ON by default and trusts X-Forwarded-For FROM 127.0.0.1, which is precisely the
# sidecar. It would therefore rewrite request.client.host to whatever the incoming
# X-Forwarded-For said — turning the one check that makes the identity headers
# trustworthy into a value the caller supplies.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
