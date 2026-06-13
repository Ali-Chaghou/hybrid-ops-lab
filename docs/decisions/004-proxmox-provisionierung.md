# ADR-004: Proxmox-Provisionierung der Lab-VMs

**Status:** Accepted
**Datum:** 2026-06-13

## Kontext
Die beiden Lab-Standorte (site-dc, site-cloud) brauchen reproduzierbare,
nachvollziehbar erzeugte VMs. Manuelles Klicken in der Proxmox-Oberfläche
wäre schneller, hinterlässt aber keinen prüfbaren Stand und widerspricht
dem deklarativen Anspruch des restlichen Projekts. Gleichzeitig läuft die
Virtualisierungsumgebung privat mit selbstsigniertem Zertifikat und darf
nicht nach außen exponiert werden.

## Entscheidung
Die VMs werden per OpenTofu mit dem Provider bpg/proxmox provisioniert
(infra/proxmox/), als Full-Clone eines Ubuntu-24.04-Templates. Sizing,
statische IP-Konfiguration und SSH-Keys werden über Cloud-Init im
Tofu-Code gesetzt.

Authentifizierung erfolgt über einen dedizierten API-Token, nicht über
das Administrator-Passwort. Endpoint, Token und alle umgebungsspezifischen
Werte liegen ausschließlich in einer lokalen Variablendatei und sind vom
Repository ausgeschlossen; eine Beispieldatei mit Platzhaltern wird
versioniert.

Das Host-Setup (Container-Runtime, k3d etc.) erfolgt nicht über
Cloud-Init, sondern über separate idempotente Bootstrap-Skripte. Damit
bleibt die Trennung erhalten: Infrastruktur deklarativ via Tofu,
Host-Konfiguration über reproduzierbare Skripte – ohne dass jede
Änderung einen VM-Neubau erzwingt.

`tofu apply` wird bewusst nur aus einer kontrollierten Umgebung mit
legitimem Netzzugang ausgeführt, nicht aus der CI. Die Pipeline
beschränkt sich auf fmt und validate.

## Konsequenzen
- Der VM-Stand ist versioniert, reproduzierbar und jederzeit per destroy
  / apply neu herstellbar; das beweist nebenbei Idempotenz.
- Keine Geheimnisse im Repository: Zugangsdaten und umgebungsspezifische
  Werte leben nur in der ignorierten Variablendatei. Auch der vom Tooling
  erzeugte Status, der Zugangsdaten im Klartext enthalten kann, ist vom
  Repository ausgeschlossen.
- Apply bleibt außerhalb der CI, weil ein Cloud-Runner die
  Management-API der privaten Umgebung erreichen müsste – das hieße, sie
  zu exponieren oder zu tunneln, was der Entscheidung gegen jede
  Exposition widerspricht.
- Im Lab nutzt der Provisionierungs-Token zur Vereinfachung weite Rechte.
  Produktiv gilt least-privilege: eine dedizierte Rolle, begrenzt auf die
  tatsächlich benötigten Operationen, mit Token-Rotation und Ablaufdatum.
- Das Basis-Image stellt keinen Management-Agent bereit; Adressierung und
  Lifecycle sind daher bewusst deterministisch über statische
  Konfiguration gelöst statt über Laufzeit-Discovery.
