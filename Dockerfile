FROM python:3.12-slim

RUN useradd -u 1000 -m -s /usr/sbin/nologin poller

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=poller:poller --chmod=644 app/ /app/
# The register map ships in the image so the container is runnable without a
# mount for testing. config.yaml is excluded by .dockerignore -- it must be
# mounted, so a missing mount fails loudly instead of running stale settings.
COPY --chown=poller:poller --chmod=644 config/eg4_6000xp_registers.yaml /config/eg4_6000xp_registers.yaml

USER poller
ENV PYTHONUNBUFFERED=1 CONFIG=/config/config.yaml
CMD ["python", "-u", "/app/poller.py"]
