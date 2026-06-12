# ADR-002: Zwei VMs statt einer Maschine

**Status:** Accepted
**Datum:** 2026-06-11

## Kontext

Die Demo simuliert zwei Standorte: ein gewachsenes Data-Center-System und
eine Cloud-Plattform. Beides ließe sich auf einer einzigen Maschine in
Containern abbilden. Damit wäre die Standortgrenze aber nur eine
Namenskonvention – es gäbe keine echte Netzwerkstrecke, die ausfallen
oder degradieren kann. In realen Hybrid-Umgebungen ist genau diese
Strecke der kritische Punkt zwischen den Welten.

## Entscheidung

Die beiden Standorte laufen als getrennte VMs mit eigenen OS-Instanzen
und eigenen IP-Adressen auf demselben Proxmox-Host. Die Trennung ist
logisch, nicht physisch: Entscheidend ist die definierte Grenze zwischen
den Systemen, nicht getrennte Hardware.

## Konsequenzen

- Es existiert eine echte Netzwerkstrecke zwischen den Standorten, die
  beobachtet und gestört werden kann.
- Jede Site hat einen eigenen, unabhängigen Lebenszyklus (Reboot,
  Bootstrap, Updates).
- Nachteil: keine physische Redundanz – ein Host-Ausfall trifft beide
  Sites. Für den Zweck der Demo bewusst akzeptiert.
- Nachteil: etwas höherer Ressourcenbedarf als eine Single-Node-Lösung.
