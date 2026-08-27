import sys
import json
import argparse
import zmq

def main():
    parser = argparse.ArgumentParser(
        description="Subscriber ZeroMQ per la telemetria di rete."
    )
    parser.add_argument(
        "topics",
        nargs="*",
        default=[],
        help="Identificatori dei topic a cui iscriversi (es. metrics.iperf metrics.middlebox.ue). Se vuoto, ascolta tutti."
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Indirizzo IP o hostname del Telemetry Manager (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        default="5555",
        help="Porta ZeroMQ PUB (default: 5555)"
    )

    args = parser.parse_args()

    # Inizializzazione Socket ZeroMQ SUB
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    address = f"tcp://{args.host}:{args.port}"
    socket.connect(address)

    # Configurazione filtri topic
    if not args.topics:
        # Una stringa vuota "" iscrive a qualsiasi messaggio trasmesso
        socket.setsockopt_string(zmq.SUBSCRIBE, "")
        print(f"[*] Connesso a {address}. Ascolto attivo su TUTTI i topic...")
    else:
        for topic in args.topics:
            socket.setsockopt_string(zmq.SUBSCRIBE, topic)
            print(f"[*] Sottoscritto al topic: '{topic}'")
        print(f"[*] Connesso a {address}. In attesa di dati...")

    # Loop di ricezione dei messaggi
    try:
        while True:
            raw_message = socket.recv_string()

            # Separazione del prefisso TOPIC dal payload JSON
            try:
                topic, json_str = raw_message.split(" ", 1)
                data = json.loads(json_str)

                print(f"\n================ [{topic}] ================")
                print(json.dumps(data, indent=2))
            except ValueError:
                # Gestione di fallback in caso di messaggi non conformi al formato "TOPIC JSON"
                print(f"\n[RAW] {raw_message}")

    except KeyboardInterrupt:
        print("\n[*] Arresto manuale intercettato. Chiusura connessione ZeroMQ...")
    finally:
        socket.close()
        context.term()

if __name__ == "__main__":
    main()