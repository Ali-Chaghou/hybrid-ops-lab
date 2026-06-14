# Runbook — degradierte Standortverbindung (Strecke)

Beschreibt den simulierten Incident "langsame Verbindung zwischen Consumer
und Queue" und das Vorgehen zur Demonstration und Behebung.

## Aufbau

Der Consumer (k3d, site-cloud) liest die Queue nicht direkt, sondern ueber
Toxiproxy als Strecke (sites/cloud/toxiproxy.json, Listener :9326 ->
ElasticMQ :9324). Toxiproxy erlaubt das gezielte Einspielen von Stoerungen
("Toxics") ueber seine API auf :8474.

## Symptome

- Backlog steigt: consumer_queue_depth_approximate waechst, obwohl der Consumer laeuft.
- Durchsatz faellt: consumer_messages_consumed_total steigt nur langsam.
- Keine Fehler im Consumer-Log, nur erhoehte Latenz pro Receive.

## Stoerung ausloesen (Demo)

```sh
./ops/chaos/degrade-link.sh            # 7000ms Latenz (Default)
./ops/chaos/degrade-link.sh 3000 500   # eigene Werte
```

## Behebung

```sh
./ops/chaos/restore-link.sh
```

Der Backlog faellt anschliessend zuegig auf 0 (der Consumer holt auf).

## Hinweise

- Die Skripte sind idempotent (mehrfaches Aufrufen ist unkritisch).
- Toxiproxy-API-Adresse ueber TOXIPROXY_API ueberschreibbar.
- at-least-once: Bei Stoerung verarbeitete, aber nicht bestaetigte Nachrichten
  werden nach Ablauf des Visibility-Timeouts erneut zugestellt.
