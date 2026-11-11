# Real Google Chrome on a virtual screen. Google publishes amd64 packages only,
# so build with --platform linux/amd64 even on an Apple Silicon Mac. That is the
# same architecture as a typical mini-PC.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-liberation \
        openbox \
        python3 \
        xvfb \
    && curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/chrome.deb \
    && useradd --create-home --uid 1000 --shell /bin/bash probe

WORKDIR /opt/slot-machine
COPY src/signin /opt/slot-machine/src/signin
# slot_machine.sitewatch: the site-down logic the Pi uses too (the package is stdlib-only)
COPY src/slot_machine /opt/slot-machine/src/slot_machine
COPY src/signin/docker/entrypoint.sh /opt/slot-machine/entrypoint.sh
RUN mkdir -p /opt/slot-machine/config \
    && chmod 755 /opt/slot-machine/entrypoint.sh

ENTRYPOINT ["/opt/slot-machine/entrypoint.sh"]
CMD ["--check"]
