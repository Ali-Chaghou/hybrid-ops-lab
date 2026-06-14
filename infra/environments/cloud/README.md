# environments/cloud

Validiert das Modul `event-queue` als AWS-portables IaC (`tofu validate` / `tofu plan`).

Lokal wird **kein** `tofu apply` gegen ElasticMQ ausgeführt: Der AWS-Provider
pollt nach dem Anlegen `GetQueueAttributes` und vergleicht den vollständigen
Attributsatz, den ein Emulator nicht deckungsgleich liefert (Timeout 'notequal').
Die lokale Queue wird stattdessen deklarativ in `sites/cloud/elasticmq.conf`
bereitgestellt. `apply` zielt auf echtes AWS.
