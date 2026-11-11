# branch-sites

Eén container die elke branch van een Git-repository als statische website publiceert. Bedoeld voor een demo waarin veel deelnemers, elk op een eigen branch, HTML, CSS en afbeeldingen pushen.

```
https://<host>/                 indexpagina met alle websites en een zoekveld
https://<host>/_uitleg/         hoe je de repository ophaalt, een branch maakt en pusht
https://<host>/<branch>/        de website van die branch
```

## Hoe het werkt

De container haalt de wijzigingen zelf op, er hoeft niets naar binnen te pushen. Python is PID 1: het start nginx en pollt in een lus.

1. Elke `POLL_INTERVAL_MINUTES` doet de syncer een `git fetch --depth=1 --prune` van alle branches naar een bare repository in `/tmp/work`.
2. Een branch met een nieuwe commit wordt met `git archive` uitgepakt naar `/tmp/sites/.data/<slug>-<sha>`. Daarna wordt de symlink `/tmp/sites/<slug>` in één rename omgezet, dus nginx serveert nooit een half uitgepakte site.
3. Een verwijderde branch verdwijnt van de pagina.
4. `/tmp/sites/_index.json` beschrijft alle sites (naam, titel uit `<title>`, auteur, datum, commit message). De indexpagina leest die en filtert in de browser.

Bij het starten begint de syncer met een lege `/tmp/sites` en pakt hij alles opnieuw uit. Er is dus geen persistente opslag nodig: een `emptyDir` volstaat.

### Branchnaam naar pad

De branchnaam wordt een slug: kleine letters, alles behalve `a-z` en `0-9` wordt `-`, maximaal 63 tekens. `Team/Zon` wordt dus `/team-zon/`. Leveren twee branches dezelfde slug op, dan wint de alfabetisch eerste en komt de andere in de log. Een slug bevat nooit `.` of `_`. Die zijn voor het platform zelf (`_index.json`, `_uitleg/`, `.data`), dus een branch kan daar niet mee botsen.

### Veiligheid

- Wat deelnemers committen wordt alleen uitgepakt en geserveerd, nooit uitgevoerd. Er is geen build.
- Het uitpakken gebruikt het `data`-filter van `tarfile`. Een symlink in een branch die buiten de site wijst (bijvoorbeeld naar `/etc/passwd`) wordt overgeslagen.
- Op de indexpagina komen namen, titels en commit messages alleen in attributen en `textContent`, nooit als HTML.
- Alles krijgt `Cache-Control: no-store`, zonder ETag. Sites veranderen tijdens de demo voortdurend.
- De repository-URL komt zonder credentials in `_index.json`.

## Configuratie

| Env var | Verplicht | Standaard | Betekenis |
|---|---|---|---|
| `REPO_URL` | ja | | De repository, bijvoorbeeld `https://github.com/org/demo-sites.git` |
| `POLL_INTERVAL_MINUTES` | nee | `5` | Hoe vaak de syncer GitHub bevraagt. Een breuk mag, bijvoorbeeld `0.5` |

De container luistert op poort 8080. `/healthz` geeft 503 tot de eerste sync klaar is en daarna 200, zodat een pod die net gestart of gewekt is pas verkeer krijgt als de sites er staan.

De image draait als willekeurige non-root UID (OpenShift). Alles wat schrijft staat in `/tmp`.

## Op ZAD

Eén component met `publish-on-web`, `authorization-wall` en `temp-storage` op `/tmp`, met een `sizeLimit` zodat één grote commit de node niet volschrijft. Voor wie de demo niet de hele dag laat draaien: ZAD's `sleep-mode` met `wake-mode: auto` werkt hier ook. De wekker toont de laadpagina tot `/healthz` groen is.

Nog na te gaan voor de uitrol:

- Komt de pod met de standaard netpol bij `github.com` (op ODCN via de egress-proxy)?
- Gebruikt de readinessprobe van het component `/healthz`?

## Ontwikkelen

```bash
task test                                                   # ruff en pytest
REPO_URL=https://github.com/octocat/Spoon-Knife.git task run  # lokaal op http://localhost:8080
```

De tests draaien tegen een lokale repository als vervanger van GitHub. Ze dekken toevoegen, bijwerken, verwijderen, botsende slugs en een symlink naar buiten.
