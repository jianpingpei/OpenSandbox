FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      netcat-openbsd \
      iproute2 \
      dnsutils \
      procps \
      socat \
      python3 \
      python3-requests \
      python3-certifi \
      nodejs && \
    rm -rf /var/lib/apt/lists/*

# Upstream envd 0.8.0 (e2b-dev/runtime release 2026.30, commit f32ee8a2a50052f32e3632ceb451111a98dd5104)
# Expected sha256sum: 101e86ad3841eb289c4332c58f8d756b6f3bd0e166a98554cb687b7290bf7347
COPY envd /usr/local/bin/envd
RUN chmod +x /usr/local/bin/envd
