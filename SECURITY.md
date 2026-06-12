# Sicherheit

## Grundsatz

Dieses Repository enthält keine produktiven Secrets, IP-Adressen oder Hostnamen.
Alle sensitiven Werte werden ausschließlich über Umgebungsvariablen oder
lokale Dateien übergeben, die nicht committed werden.

## Secrets-Handling

| Datei im Repo       | Zweck                              | Enthält echte Werte? |
|---------------------|------------------------------------|----------------------|
| `*.env.example`     | Vorlage mit Platzhaltern           | Nein                 |
| `*.tfvars.example`  | Vorlage mit Platzhaltern           | Nein                 |
| `*.env`             | Lokale Laufzeitkonfiguration       | Ja – nie committen   |
| `*.tfvars`          | Lokale Tofu-Variablen              | Ja – nie committen   |

## LocalStack-Credentials

LocalStack benötigt formal AWS-Credentials, akzeptiert aber beliebige Dummy-Werte.
In allen Beispiel-Dateien stehen die offiziellen LocalStack-Dummies:

```
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
```

Diese Werte funktionieren ausschließlich gegen LocalStack.
Sie sind absichtlich als Dummy-Credentials gekennzeichnet und haben keinen Zugang
zu echten AWS-Ressourcen.

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

## Meldung von Sicherheitsproblemen

Falls du im Repository einen echten Secret-Wert oder eine Sicherheitslücke findest,
melde dies bitte direkt an den Repository-Eigentümer (nicht als öffentliches Issue).
