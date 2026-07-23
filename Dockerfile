# syntax=docker/dockerfile:1
# (heredocs + --mount=type=secret need the BuildKit dockerfile frontend)
#
# python:3.12 (not 3.14 like the sibling workers): the pinned pandas 2.1.3 /
# numpy 1.26.4 only ship wheels up to cp312. musllinux cp312 wheels exist for
# every pinned dep on x86_64 — build with --platform linux/amd64 (the k3s
# nodes); an arm64 build would compile pandas from source.
FROM python:3.12-alpine AS downloader
# APIFY_CLIENT_VERSION: pinned commit of deepriver-ai/apify_client — the kg
# geocoder wrapper (src/entities/linking/geocode.py) imports `helpers.geocode`
# from that repo's src/ (path import via APIFY_CLIENT_SRC, not pip).
ARG APIFY_CLIENT_VERSION="e33507def22e4aa7fbb335d0da4bac6a57ced342"
RUN apk add --no-cache openssh-client git
COPY <<EOF /root/.ssh/config
Host github.com-apify_client
        HostName github.com
        User git
        AddKeysToAgent yes
        IdentitiesOnly yes
        IdentityFile /run/secrets/gh-ssh-priv
EOF
# Add github.com public ssh keys as of 2026-06-10T20:18:16+00:00. Update as needed, see https://docs.github.com/en/authentication/troubleshooting-ssh/error-host-key-verification-failed, https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
COPY <<EOF /root/.ssh/known_hosts
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
EOF
WORKDIR /apify_client
RUN --mount=type=secret,required=true,id=gh-ssh-priv \
    git init -q . \
    && git remote add origin git@github.com-apify_client:deepriver-ai/apify_client.git \
    && git fetch -q --depth 1 origin ${APIFY_CLIENT_VERSION} \
    && git checkout -q FETCH_HEAD

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
# Only helpers/ is needed (helpers.geocode: the NLP+geocoder microservice
# client); path-imported, so no pip packaging required.
COPY --from=downloader /apify_client/src/helpers /apify_client/src/helpers
ENV APIFY_CLIENT_SRC=/apify_client/src
ADD scripts ./scripts
ADD src ./src
# Fails the build early if /kg isn't ejecutor-writable (WORKDIR ownership
# depends on the BuildKit version) — the pipeline caches are created lazily
# at runtime and must be writable.
RUN mkdir -p cache
ENTRYPOINT ["python", "-u", "src/listener.py"]
