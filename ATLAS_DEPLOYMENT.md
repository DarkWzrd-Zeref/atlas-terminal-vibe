# Atlas Terminal deployment

The upstream Vibe application and bundled frontend are built from this fork.
No model provider or exchange account is preconfigured. Research generation
requires a provider configured by the owner through the secured native settings.

Railway: mount one volume at `/data`, set `RAILWAY_RUN_UID=0`, `PORT=8899`.
The entry point connects the upstream runs, sessions, uploads, swarm runs,
user memory, skills and settings file to this volume, then drops privileges to
the upstream `vibe` account. It refuses to boot without its volume.

The API requires a random persistent key, optionally supplied through the
`ATLAS_VIBE_API_KEY` secret shared with the Terminal gateway. No public domain.
Liveness alone is not acceptance: research, saved artifacts, restart recovery
and the upstream generated-code sandbox must be exercised before user readiness.
