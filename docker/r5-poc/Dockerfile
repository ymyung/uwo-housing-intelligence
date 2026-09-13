FROM eclipse-temurin:21-jdk-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv osmium-tool \
    && rm -rf /var/lib/apt/lists/*

COPY docker/r5-poc/requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY scripts/r5_surface_poc.py /app/r5_surface_poc.py
COPY scripts/r5_surface_acceptance.py /app/r5_surface_acceptance.py
COPY scripts/r5_walking_surface_service.py /app/r5_walking_surface_service.py

ENTRYPOINT ["python3", "/app/r5_surface_poc.py"]
