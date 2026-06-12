# ADR-001: Lokale Demo statt echter AWS-Umgebung

**Status:** Accepted
**Datum:** 2026-06-11

## Kontext

Dieses Projekt ist ein Showcase für Hybrid-Cloud-Betrieb. Mit AWS arbeite
ich beruflich täglich in einer Multi-Account-Umgebung; das muss ein
privates Projekt nicht erneut belegen. Ein Showcase soll stattdessen für
Dritte nachvollziehbar sein: klonbar, ohne Credentials lauffähig und ohne
laufende Kosten. Eine echte AWS-Umgebung würde Zugangsdaten, Abrechnung
und Aufräumarbeit erfordern, ohne dem Kern des Projekts – Betriebsdenken –
etwas hinzuzufügen.

## Entscheidung

Die Demo läuft vollständig lokal auf zwei VMs. AWS-Dienste werden, wo
nötig, durch LocalStack (nur SQS) ersetzt. Der OpenTofu-Code verwendet
einen konfigurierbaren Endpoint, sodass derselbe Code gegen ein echtes
AWS-Konto laufen könnte.

## Konsequenzen

- Jeder kann das Repository klonen und die Demo ohne AWS-Konto starten.
- Keine laufenden Kosten, kein Risiko vergessener Ressourcen.
- Der IaC-Code bleibt AWS-portabel (Endpoint als Variable).
- Nachteil: kein echtes IAM, keine echten Service-Quotas.
- Nachteil: LocalStack verhält sich unter Last nicht wie AWS; die Demo
  zeigt Prinzipien und Vorgehen, nicht Skalierung.
