# CROUS Caen watcher

Checks trouverunlogement.lescrous.fr every ~5 minutes (GitHub Actions) for new rooms in Caen + Hérouville-Saint-Clair and sends a phone notification via ntfy with a direct link.

- Phases are detected automatically from the public `/api/fr/tools` endpoint.
- The ntfy topic is stored as the repository secret `NTFY_TOPIC` (never in the code).
- `crous_caen_state.json` is committed by the workflow so it remembers which rooms were already reported.

Optional repository variables: `RESIDENCE_FILTER` (e.g. `Lebisey,Satie,Cote de Nacre`), `MAX_RENT_EUR`.
