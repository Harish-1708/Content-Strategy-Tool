# Agent-Reach on Google Cloud Run: Agent-Reach, its OpenCLI, Node and a browser, and the small HTTP wrapper (server.py).
# Build from the REPOSITORY ROOT:  docker build -f cloudrun/agent-reach/Dockerfile .   (Cloud Build does this through cloudbuild.yaml)
# Nothing secret is in the image: the token and the cookies arrive at run time from Secret Manager.
FROM node:22-bookworm-slim

ARG AGENT_REACH_REF=7a38047acf26a729cbb27851e3cb81639da86b29
ARG PLAYWRIGHT_VERSION=1.49.1

ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PATH=/opt/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-venv python3-pip \
 && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv \
 && pip install "git+https://github.com/Panniantong/Agent-Reach@${AGENT_REACH_REF}" "playwright==${PLAYWRIGHT_VERSION}" \
 && playwright install --with-deps chromium

RUN useradd --create-home --uid 10001 reach \
 && mkdir -p /app /home/reach/.agent-reach \
 && chown -R reach:reach /app /home/reach

COPY cloudrun/agent-reach/server.py cloudrun/agent-reach/cookies.py cloudrun/agent-reach/bootstrap.sh /app/
COPY contentintel/agent_reach_catalog.py contentintel/human.py /app/
RUN chmod 0555 /app/bootstrap.sh

USER reach
ENV HOME=/home/reach PORT=8080 PLAYWRIGHT_BROWSERS_PATH=/home/reach/.cache/ms-playwright
WORKDIR /app

# Agent-Reach prepares OpenCLI, its browser and the extension itself (agent-reach add <channel>); every step's result is kept in /app/bootstrap.json and shown by /healthz, so a step that did not work is seen, not guessed.
RUN /app/bootstrap.sh

EXPOSE 8080
CMD ["python3", "/app/server.py"]
