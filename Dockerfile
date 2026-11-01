FROM python:3.11
ENV PYTHONUNBUFFERED=1

WORKDIR /srv/root

RUN apt update && apt install --no-install-recommends -y \
    git curl build-essential=12.12 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml poetry.lock ./
RUN pip install -U pip poetry==2.2.1
RUN poetry config virtualenvs.create false
# only the runtime dependencies: the dev group (pre-commit) would downgrade virtualenv,
# which poetry itself uses to build py3rijndael, and the build fails on newer python:3.11 images
RUN poetry install --no-root --only main
# WebSocket support for the website chat (/ws/chat); uvicorn picks it up by itself
RUN pip install --no-cache-dir websockets==12.0

RUN apt update && \
    apt install -y default-mysql-client redis-tools

# NOTE: done last to avoid re-run of previous steps
COPY . .

# Copy logging config if it doesn't exist
RUN if [ ! -f logging.yaml ]; then cp logging.yaml.example logging.yaml; fi

RUN chmod +x scripts/start_server.sh scripts/wait-for-it.sh

CMD [ "/srv/root/scripts/start_server.sh" ]
