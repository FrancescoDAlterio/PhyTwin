FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Installazione dipendenze di sistema e strumenti di rete
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    iproute2 \
    iptables \
    iputils-ping \
    net-tools \
    iperf3 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app/ /app/

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]