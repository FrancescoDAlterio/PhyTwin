FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    iproute2 \
    iperf3 \
    iperf \
    iputils-ping \
    python3 \
    sqlite3 \
    tini \
    && rm -rf /var/lib/apt/lists/*

COPY app/ue_agent.py /usr/local/bin/ue_agent.py
RUN chmod +x /usr/local/bin/ue_agent.py

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["tail", "-f", "/dev/null"]