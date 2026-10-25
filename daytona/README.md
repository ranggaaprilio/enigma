# Custom Daytona snapshot (mise pre-installed)

The preview / dev-server feature installs [`mise`](https://mise.jdx.dev) inside the
sandbox at runtime via `curl -fsSL https://mise.run | sh`
(`backend/app/services/dev_server.py`). On the stock `daytonaio/sandbox` image that
download is reset by the sandbox network, so setup fails with:

```
[setup] mise not found on PATH
exit 127
```

This directory builds a snapshot with `mise` already baked in, so `command -v mise`
succeeds and the runtime download is skipped.

## Build & publish

1. Install the Daytona CLI and log in:
   ```bash
   brew install daytonaio/cli/daytona      # or: see https://www.daytona.io/docs
   daytona login
   ```
2. Build the snapshot (needs network to `mise.run` from your machine, build-time only):
   ```bash
   ./daytona/build-snapshot.sh
   ```
3. Point the backend at it — set in `.env`:
   ```
   DAYTONA_DEFAULT_SNAPSHOT=enigma-mise
   ```
4. Restart the backend. **Existing** sandboxes were provisioned from the old image —
   delete/recreate the workspace's sandbox so it picks up the new snapshot.

## If sandbox egress is also blocked

`mise install` (run during preview) still downloads language runtimes from the
internet. If your sandboxes have no outbound access at all, also pre-install the
project's tools at build time — see the commented `COPY` / `RUN mise install` block
at the bottom of `sandbox/Dockerfile`.

## Shared with the local Docker runtime

The Dockerfile lives in `sandbox/` because the local Docker/Podman provider builds the same
image (`./sandbox/build-local-image.sh` → `enigma-sandbox:latest`). Keep one definition so
the two providers run an identical toolchain; `build-snapshot.sh` points at it.
