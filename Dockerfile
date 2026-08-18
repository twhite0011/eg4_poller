FROM python:3.12-slim

RUN useradd -u 1000 -m -s /usr/sbin/nologin poller

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=poller:poller --chmod=644 app/ /app/
# The register map and the device/site config template ship in the image so
# the container is runnable without the ./config bind mount for testing --
# in a real deploy, ./config:/config:ro (see docker-compose.yml) overlays
# the same paths with the tracked copies.
COPY --chown=poller:poller --chmod=644 config/eg4_6000xp_registers.yaml /config/eg4_6000xp_registers.yaml
COPY --chown=poller:poller --chmod=644 config/config.example.yaml /config/config.example.yaml

USER poller
ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "/app/poller.py"]
