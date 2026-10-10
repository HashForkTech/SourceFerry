ARG CRAWL4AI_IMAGE=unclecode/crawl4ai:0.9.4@sha256:9021b3cb5c6f12570bbcd5395638495e0a06969b3148e377b953d174af2ebc9b
FROM ${CRAWL4AI_IMAGE} AS tools
USER root
COPY os-packages.txt /tmp/sourceferry-os-packages.txt
RUN apt-get update \
    && xargs apt-get install -y --no-install-recommends < /tmp/sourceferry-os-packages.txt \
    && rm -rf /var/lib/apt/lists/* \
    && rm /tmp/sourceferry-os-packages.txt
COPY requirements.txt /tmp/sourceferry-requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/sourceferry-requirements.txt \
    && python -m pip check \
    && rm /tmp/sourceferry-requirements.txt

FROM tools AS runtime
RUN python -m pip uninstall --yes pip
USER appuser
