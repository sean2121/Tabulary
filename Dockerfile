# tabulary application image: Python app + Ontop CLI (Java) + psql client.
FROM python:3.12-slim

ARG ONTOP_VERSION=5.5.0
ARG PGJDBC_VERSION=42.7.4

RUN apt-get update \
 && apt-get install -y --no-install-recommends openjdk-21-jre-headless postgresql-client curl unzip \
 && rm -rf /var/lib/apt/lists/*

# Ontop CLI and the PostgreSQL JDBC driver it needs
RUN curl -fsSL -o /tmp/ontop.zip https://github.com/ontop/ontop/releases/download/ontop-${ONTOP_VERSION}/ontop-cli-${ONTOP_VERSION}.zip \
 && mkdir -p /opt/ontop && unzip -q /tmp/ontop.zip -d /opt/ontop && rm /tmp/ontop.zip \
 && curl -fsSL -o /opt/ontop/jdbc/postgresql-${PGJDBC_VERSION}.jar https://repo1.maven.org/maven2/org/postgresql/postgresql/${PGJDBC_VERSION}/postgresql-${PGJDBC_VERSION}.jar

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY el_proto ./el_proto
COPY dm_proto ./dm_proto
COPY scan ./scan
COPY server ./server
COPY serve ./serve
COPY docker ./docker

ENV ONTOP_HOME=/opt/ontop \
    TABULARY_DATA_DIR=/app/data \
    TABULARY_CACHE_DIR=/app/data/cache \
    PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "./docker/load_demo.sh && exec uvicorn server.app:app --host 0.0.0.0 --port 8000"]
