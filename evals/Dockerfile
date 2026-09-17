FROM mcr.microsoft.com/powershell:7.5-ubuntu-24.04

USER root

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends ca-certificates git python3 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 20000 evaluator \
    && useradd --uid 20000 --gid 20000 --create-home --shell /bin/bash evaluator \
    && groupadd --gid 20001 mycelium-grader \
    && useradd --uid 20001 --gid 20001 --home-dir /var/lib/mycelium-grader --shell /usr/sbin/nologin mycelium-grader

RUN mkdir -p /logs/verifier /logs/agent /logs/artifacts /app /verifier /tests \
    /opt/mycelium-grader /var/lib/mycelium-grader/tmp

# BenchFlow copies only this Dockerfile into generated task contexts. The
# runner replaces this marker with the byte-exact neutral workflow oracle.
# MYCELIUM_WORKFLOW_ORACLE_PAYLOAD

RUN chown evaluator:evaluator /app && chmod 0755 /app

USER evaluator:evaluator
WORKDIR /app
