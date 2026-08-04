# syntax=docker/dockerfile:1
# (pinned frontend: the WORKDIR-honors-USER behavior below depends on it)
#
# python:3.12 (not 3.14 like the sibling workers): the pinned pandas 2.1.3 /
# numpy 1.26.4 only ship wheels up to cp312. musllinux cp312 wheels exist for
# every pinned dep on x86_64 — build with --platform linux/amd64 (the k3s
# nodes); an arm64 build would compile pandas from source.
FROM python:3.12-alpine
RUN addgroup -g 1000 ejecutor && adduser -G ejecutor -u 1000 -D ejecutor
USER 1000:1000
# WORKDIR honors USER under the dockerfile:1 frontend → /kg is ejecutor-owned,
# so the runtime file caches (cache/{geocode,extraction,link_llm}, created
# lazily) are writable. Caches are ephemeral per pod — accepted for v1 (see
# docs/todos/productionization_streaming_kg.md Phase 4).
WORKDIR /kg
ADD requirements.txt requirements.txt
RUN pip install -r requirements.txt
ADD scripts ./scripts
ADD src ./src
# Fails the build early if /kg isn't ejecutor-writable (WORKDIR ownership
# depends on the BuildKit version) — the pipeline caches are created lazily
# at runtime and must be writable.
RUN mkdir -p cache
ENTRYPOINT ["python", "-u", "src/listener.py"]
