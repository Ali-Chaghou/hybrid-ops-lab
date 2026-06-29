# Sicherheit

[Übersicht](README.md) · [Dokumentation](docs/README.md) · [Status & Roadmap](docs/roadmap.md) · [Nachweise](docs/evidence-index.md) · [Security](SECURITY.md)

## Grundsatz

Dieses Repository enthält keine produktiven Zugangsdaten und keine realen privaten
IP-Adressen oder Hostnamen aus der betriebenen Umgebung.
Alle sensitiven Werte werden ausschließlich über Umgebungsvariablen oder
lokale Dateien übergeben, die nicht committed werden.

## Secrets-Handling

| Datei im Repo       | Zweck                              | Enthält echte Werte? |
|---------------------|------------------------------------|----------------------|
| `*.env.example`     | Vorlage mit Platzhaltern           | Nein                 |
| `*.tfvars.example`  | Vorlage mit Platzhaltern           | Nein                 |
| `*.env`             | Lokale Laufzeitkonfiguration       | Ja – nie committen   |
| `*.tfvars`          | Lokale Tofu-Variablen              | Ja – nie committen   |

## Lokaler SQS-Endpoint (ElasticMQ)

Das Lab nutzt **ElasticMQ** als lokalen, SQS-kompatiblen Endpoint (siehe
[ADR-005](docs/decisions/005-elasticmq-statt-localstack.md)), nicht LocalStack. Daraus
folgt für Credentials:

- Dummy-Werte gegen einen lokalen Emulator sind **keine** echten Cloud-Credentials und
  geben keinen Zugang zu echten AWS-Ressourcen.
- Es dürfen **keine** echten AWS-Zugangsdaten in `*.env.example`, Terraform-Beispiele,
  Logs oder Screenshots gelangen.
- Leere oder lokale Beispielwerte dürfen **niemals** für echte AWS-Endpunkte verwendet
  werden.
- `.env`, `make.env`, `*.tfvars`, State-Dateien und private Schlüssel bleiben gitignored
  und werden nie committet.

Pflicht-Passwörter in `*.env.example` sind leer: vor der Nutzung lokal starke
Zufallswerte setzen. Leere oder abgelehnte Platzhalter führen zum kontrollierten
Abbruch von Bootstrap und Deploy.

## Schutzschichten gegen versehentliche Secret-Commits

Das Repository setzt drei Schichten ein:

1. **`.gitignore`** – schließt `*.env`, `*.tfvars` und Kubeconfigs vom Tracking aus.
2. **Pre-Commit-Hooks** – [gitleaks](https://github.com/gitleaks/gitleaks) und
   `detect-private-key` prüfen jeden Commit lokal vor dem Push.
3. **CI** – gitleaks läuft zusätzlich in GitHub Actions und GitLab CI bei jedem
   Push und Pull Request.

```bash
pre-commit install          # einmalig nach dem Klonen
pre-commit run --all-files  # manueller Durchlauf
```

Nach `pre-commit install` laufen die lokalen Hooks bei Commits. Die CI führt zusätzliche
Validierungs- und Secret-Checks aus. Eine einmalige Abschlussprüfung der
gesamten Git-Historie ist ein eigener Schritt vor einem Release-Tag und in der
[Roadmap](docs/roadmap.md) aufgeführt.

## Prüfungen für das öffentliche Repository

Dieses Repository ist öffentlich. Vor einem Release-/Tag-Kandidaten gilt folgende
Checkliste:

- **Secret-Scan** der getrackten Dateien (z. B. `gitleaks`, falls verfügbar).
- **Git-Historie** auf alte Secrets prüfen (Historien-Scan getrennt durchführen).
- **Screenshots visuell prüfen** (`docs/img/*`) auf sichtbare Hosts, IPs, Tokens.
- **EXIF-/Metadaten** der Bilder prüfen.
- **Keine internen Hosts, IP-Adressen oder Benutzernamen** in Nachweisen/Handoffs.
- **Keine vollständigen Secrets in Logs**; Funde nur redigiert (Datei, Zeile, Typ,
  redigierter Fingerprint) melden.

## Meldung von Sicherheitsproblemen

Falls du im Repository einen echten Secret-Wert oder eine Sicherheitslücke findest,
melde dies bitte direkt an den Repository-Eigentümer (nicht als öffentliches Issue).
