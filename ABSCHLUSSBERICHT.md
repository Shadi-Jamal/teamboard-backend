# TeamBoard – Abschlussbericht

Stand: 14.11.2026 · Branch `main` · Arbeitsverzeichnis sauber (bis auf die bewusst
nicht eingecheckte `PROMPT.md`)

## 1. Ergebnis in Kürze

Das Backend ist vollständig umgesetzt und lokal verifiziert:

- **529 Tests, 0 fehlgeschlagen, 98,28 % Abdeckung** (Zeilen und Zweige), gegen
  echtes PostgreSQL 16 und Redis 7.
- **ruff und mypy (strict) ohne Befund.**
- **`docker compose up` startet alles** aus leerem Zustand; die OpenAPI-Doku ist
  erreichbar; ein End-to-End-Smoke-Test (19 Prüfungen, inklusive Worker und
  Mandantentrennung) läuft durch.

**Eine Anforderung der Definition of Done ist nicht nachgewiesen: „CI passes“.**
Das Repository hat kein GitHub-Remote, der Workflow ist also nie auf GitHub
gelaufen. Jeder Schritt des Workflows wurde lokal mit denselben Befehlen
ausgeführt und ist grün (Details in Abschnitt 4).

## 2. Was gebaut wurde

**Mandantenfähigkeit.** Gemeinsame Datenbank, gemeinsames Schema, `tenant_id` auf
jeder mandanteneigenen Tabelle. Die Trennung ist dreifach abgesichert:

1. *Anwendung:* Mandanteneigene Tabellen sind nur über `TenantScopedRepository`
   erreichbar. Unterklassen können keine Abfrage ohne Mandantenfilter bauen; ein
   Architekturtest prüft das am Quelltext.
2. *Datenbank:* Row-Level Security ist auf allen 11 Tabellen aktiviert und
   erzwungen. Ohne Kontext sieht die Anwendungsrolle in keiner Tabelle eine Zeile.
3. *Rechte und Constraints:* Die Anwendung verbindet sich als eingeschränkte Rolle
   ohne `BYPASSRLS`, mit `UPDATE`-Rechten nur auf einzelnen Spalten; Fremdschlüssel
   zwischen Mandantentabellen enthalten `tenant_id`.

Der Mandantenkontext wird mit `set_config(..., is_local => true)` (entspricht
`SET LOCAL`) pro Transaktion gesetzt und endet mit ihr. Dass er nicht über den
Connection-Pool in die nächste Anfrage gelangt, ist ausdrücklich getestet: Pool
mit genau einer Verbindung, gleiche Backend-PID, nach Commit, nach Rollback und
nach abgebrochener Transaktion.

**Authentifizierung und Rechte.** Benutzer in mehreren Mandanten über
Mitgliedschaften; JWT-Access-Token (15 Minuten); rotierende Refresh-Token mit
Reuse-Erkennung (die ganze Token-Familie wird widerrufen); Argon2id; Einladungen;
vier Rollen (owner, admin, member, viewer) mit Rangregeln; API-Keys pro Mandant –
nur als Hash gespeichert, mit Scopes, widerrufbar, einmalig angezeigt.

**Fachlogik.** Projekte und Aufgaben mit Filtern, Cursor-Pagination, Soft Delete
(mit Wiederherstellung) und Audit-Log, das in derselben Transaktion wie die
Änderung geschrieben wird.

**Betrieb.** Rate Limiting pro Mandant (Token Bucket in Redis) und Plan-Quotas;
Idempotency-Keys für die erzeugenden POST-Endpunkte; ein Hintergrundjob
(asynchroner Datenexport) mit Wiederholungen und exponentiellem Backoff; JSON-Logs
mit Request- und Mandanten-ID; Fehler nach RFC 7807; Health- und
Readiness-Endpunkte; vollständige OpenAPI-Dokumentation (39 Operationen).

**Auslieferung.** Mehrstufiges Dockerfile (Nutzer 10001, kein Root, schreibgeschütztes
Dateisystem), `docker-compose.yml`, GitHub-Actions-Workflow, `.env.example`,
README mit Architektur- und ER-Diagramm, `ASSUMPTIONS.md` (49 dokumentierte
Entscheidungen), AWS-Leitfaden.

## 3. Verifikation – echte Ausgaben

Letzter vollständiger Lauf auf dem eingecheckten Endstand:

```text
=== ruff format ===   112 files already formatted
=== ruff check ===    All checks passed!
=== mypy ===          Success: no issues found in 106 source files
=== pytest ===
TOTAL                                    2640     25    328     26    98%
64 files skipped due to complete coverage.
Required test coverage of 90.0% reached. Total coverage: 98.28%
529 passed in 66.04s (0:01:06)
```

Die 25 nicht abgedeckten Zeilen sind defensive Zweige (zum Beispiel das
Weiterreichen unerwarteter Datenbankfehler); sie stehen in der Coverage-Ausgabe.

Verteilung der Tests:

| Bereich | Tests |
|---|--:|
| Mandantentrennung (`test_rls`, `test_cross_tenant`, `test_repository_scoping`, `test_defense_in_depth`, `test_architecture`) | 245 |
| Authentifizierung, API-Keys, Einladungen | 72 |
| Mandanten, Mitglieder, Rollenmatrix | 34 |
| Projekte, Aufgaben, Pagination, Audit | 65 |
| Idempotenz, Rate Limits, Export-Worker | 46 |
| Fehlerformat, Logging, OpenAPI, Migrationen, Health, Units | 67 |

Zu den nicht verhandelbaren Anforderungen:

- **Cross-Tenant-Tests für jeden Endpunkt.** Alle 29 mandantenbezogenen Routen
  werden von Mandant A gegen Mandant B angegriffen: fremde Objekt-IDs (als owner,
  admin, member, viewer und API-Key), fremder `X-Tenant-ID`-Header, umgelenkter
  API-Key, Listen und Filter. Die Antwort muss identisch zu der für ein nicht
  existierendes Objekt sein (Status, Header, Body), und die Daten von B werden vor
  und nach jedem Angriff per Fingerabdruck verglichen. Ein Wächtertest schlägt
  fehl, sobald eine Route ohne Matrixeintrag hinzukommt. Die übrigen 10 Routen
  (Health, Auth, eigene Mandantenliste, Einladung annehmen) sind einzeln begründet.
- **RLS blockiert die vergessene Filterbedingung.** `SELECT … FROM <tabelle>` ohne
  `WHERE` liefert als Anwendungsrolle nur Zeilen des gesetzten Mandanten – für jede
  der 8 mandanteneigenen Tabellen, zusätzlich über das ORM.
- **Gegenprobe.** Mit entferntem RLS hält die Anwendungsschicht, mit entferntem
  Repository-Filter hält RLS, und mit beidem entfernt liest Mandant A tatsächlich
  Daten von B. Das belegt, dass die Tests ein Leck erkennen würden.
- **Nebenläufigkeit.** Refresh-Rotation (10 gleichzeitige Anfragen, genau eine
  gewinnt), Idempotency-Key (10 gleichzeitige, genau eine Ressource),
  Projekt-Quota (12 gegen ein Limit von 5), Einladungen, letzter Owner, Token
  Bucket, zwei Mandanten verschränkt auf einem Pool mit zwei Verbindungen.

Docker Compose aus leerem Zustand (Volumes gelöscht, Image neu gebaut):

```text
compose up --wait exit code: 0
api       running   Up 6 seconds (healthy)
db        running   Up 9 seconds (healthy)
migrate   exited    Exited (0) 6 seconds ago
redis     running   Up 9 seconds (healthy)
worker    running   Up 6 seconds (healthy)
api uid=10001, read-only rootfs: true
/docs: 200   /redoc: 200   /openapi.json: 200   /healthz: 200   /readyz: 200
Smoke test: All smoke checks passed.   (19 von 19)
```

Die curl-Befehle aus dem README-Schnellstart wurden wörtlich gegen diesen Stack
ausgeführt und liefern das dokumentierte Verhalten.

## 4. Was nicht ausgeführt werden konnte

- **GitHub Actions.** Kein Remote, daher kein Lauf auf GitHub. Lokal ausgeführt:
  alle vier Jobs mit ihren Befehlen (Lint, Typprüfung, Tests, Compose-Smoke-Test).
  Die Workflow-Datei wurde mit PyYAML geparst und strukturell geprüft. Eine Prüfung
  mit `actionlint` wurde von der Berechtigungsrichtlinie der Sitzung abgelehnt
  (Ausführen eines fremden Container-Images) und deshalb nicht durchgeführt.
- **Linux x86-64.** Tests liefen auf macOS (arm64), das Image wurde für
  linux/arm64 gebaut und gestartet. Die GitHub-Runner sind x86-64; dort ist nichts
  gelaufen.
- **AWS.** Der Leitfaden wurde ohne AWS-Konto geschrieben und nicht ausgeführt; das
  steht auch im Dokument. Geprüft sind nur die anwendungsseitigen Anforderungen
  (Rollen, Umgebungsvariablen, Health Checks, Reihenfolge der Migration).

## 5. Im Self-Review gefunden und behoben

| Befund | Behebung |
|---|---|
| Wiederherstellen eines Projekts mit inzwischen vergebenem Namen ergab 500 | Antwortet jetzt 409 |
| Die Anwendungsrolle durfte jede Spalte ändern, auch `tenants.plan`, `users.is_active`, `tenant_id` und die Scopes eines API-Keys | `UPDATE` nur noch auf einzelnen Spalten, `DELETE` nur auf Mitgliedschaften, neue Mandanten nur im Free-Plan |
| Job-Payloads in Redis waren mit pickle serialisiert (Schreibzugriff auf Redis hätte Codeausführung im Worker bedeutet) | JSON |
| Redis-Aufrufe ohne Zeitlimit: ein hängendes Redis hätte Anfragen blockiert | Zeitlimit, danach „fail open“ wie bei einem Ausfall |
| Keine Größenbegrenzung für Request-Bodies | 413 oberhalb von 1 MiB, auch bei Chunked-Uploads |
| Unbekannte Felder im Request wurden stillschweigend ignoriert | Werden abgelehnt (422) |
| Verbindungs-URLs mit Passwort waren über `repr(settings)` sichtbar | Maskiert wie das JWT-Secret |
| Ein fertiger Export konnte durch eine doppelte Zustellung überschrieben werden | Abgesichert |
| Der Wächtertest für die Vollständigkeit der Cross-Tenant-Matrix zählte auf dieser FastAPI-Version null Routen und wäre leer durchgelaufen | Auf Router-Registry und OpenAPI umgebaut; prüft jetzt 39 Routen |

Der letzte Punkt ist der wichtigste: Ein Test, der nichts prüft, sieht genauso
grün aus wie einer, der alles prüft. Deshalb enthalten die Isolationstests jetzt
feste Erwartungswerte (39 Routen, 29 in der Matrix) und die Gegenprobe aus
Abschnitt 3.

## 6. Bekannte Einschränkungen

- Access-Token lassen sich vor Ablauf (15 Minuten) nicht einzeln widerrufen.
  Kontosperre, Entfernen einer Mitgliedschaft und Widerruf der Refresh-Token wirken
  dagegen sofort.
- Die Kontosperre nach 10 Fehlversuchen lässt sich missbrauchen, um eine bekannte
  Adresse 15 Minuten auszusperren. Die Registrierung verrät, ob eine E-Mail
  vergeben ist.
- Kein E-Mail-Versand: Einladungstoken gehen an den Einladenden; es gibt weder
  E-Mail-Verifikation noch Passwort-Reset.
- Exportergebnisse liegen als JSONB in PostgreSQL – für sehr große Mandanten
  ungeeignet.
- Keine Aufräumjobs für abgelaufene Idempotency-Keys, verbrauchte Refresh-Token und
  soft-gelöschte Zeilen.
- Pläne werden direkt in der Datenbank geändert (kein Billing); Mandanten lassen
  sich nicht per API löschen.
- API-Keys gehören dem Mandanten: Verlässt ihr Ersteller den Mandanten, bleiben sie
  gültig.
- Ein einziges Signatur-Secret; ein Wechsel macht alle Access-Token ungültig.
- Nicht authentifizierter Verkehr außerhalb der Auth-Endpunkte wird von der
  Anwendung nicht gedrosselt (Aufgabe von Load Balancer oder WAF).
- RLS schützt vor Programmierfehlern, nicht vor beliebigem SQL: Die Anwendungsrolle
  darf `app.tenant_id` selbst setzen. Das ist im README als Bedrohungsmodell
  beschrieben.
- Wird ein Worker beim letzten Versuch hart beendet, kann ein Job in `running`
  hängen bleiben.

## 7. Was ich als Nächstes verbessern würde

1. **Auf GitHub pushen und die CI zum ersten Mal laufen lassen** – das schließt die
   einzige offene Lücke der Definition of Done.
2. **Aufräumjob** mit eigener, dafür berechtigter Datenbankrolle.
3. **Exporte nach S3** streamen und per vorsignierter URL ausliefern.
4. **E-Mail-Versand** als zweiter Hintergrundjob: Einladungen, Verifikation,
   Passwort-Reset.
5. **Signaturschlüssel mit Key-ID** (rotierbar), mittelfristig asymmetrisch.
6. **Metriken und Tracing** (OpenTelemetry), Alarme auf „refresh token reuse“.
7. **Mandanten-Offboarding:** Export, Löschfristen, endgültiges Löschen.
8. **Infrastructure as Code** für das AWS-Setup, sobald ein Konto zum Validieren
   vorhanden ist.

## 8. Hinweise zur lokalen Umgebung

- Docker Desktop lief zu Beginn nicht und wurde gestartet; es wird für die Tests
  benötigt.
- Git hatte nur eine E-Mail-Adresse, aber keinen Namen konfiguriert. Für dieses
  Repository ist lokal `user.name = "Shadi Jamal"` gesetzt (aus der E-Mail-Adresse
  abgeleitet). Falls das nicht stimmt:
  `git config user.name "…"` und die Commits bei Bedarf umschreiben.
- `PROMPT.md` ist nicht eingecheckt.
- SQLAlchemy ist auf die 2.0-Serie festgelegt (2.0.57), weil der Stack „2.0“
  vorgibt; aktuell wäre 2.1.
- Auf diesem Rechner markiert macOS die `.pth`-Dateien des virtuellen Environments
  wiederholt als versteckt, wodurch Python 3.12 die editierbare Installation
  ignoriert. Tests, mypy und Alembic lesen deshalb direkt aus `src/`. Für eigene
  Skripte: `PYTHONPATH=src` setzen oder `chflags -R nohidden .venv` ausführen.
- Der Ryuk-Container von testcontainers ist in den Tests standardmäßig
  abgeschaltet, weil er unter Docker Desktop sporadisch beim Start scheiterte. Die
  Fixtures räumen ihre Container selbst auf.
