FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Installazione dei pacchetti di rete e Python
RUN apt-get update && apt-get install -y \
    iproute2 \
    net-tools \
    iputils-ping \
    iperf3 \
    tcpdump \
    python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia dello script server_agent.py
COPY app/ /app/

CMD ["python3", "-u", "/app/server_agent.py"]