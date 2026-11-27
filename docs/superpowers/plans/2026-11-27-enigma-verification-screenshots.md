# Enigma — Verifiable Agent Loop: Real Screenshots in the Evidence (Plan 6)

> **For agentic workers:** Execute task-by-task with TDD (red → green → refactor). Tick the `- [x]` steps as they land.

**Goal:** Spec §3 asks for a *screenshot for the human* next to the structured snapshot. Today an observation stores only a placeholder reference (`verification/<run>/<port>.png`), and no image is ever captured. This plan captures a real PNG of the running app inside the tenant's own sandbox, stores it in **S3-compatible object storage** (RustFS in dev), and shows it in the Evidence view (the Verify panel and the SpecPanel's implement view).

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md` §3 (Observe), §5 step 4.

## Current state (verified)

- **Observation:** `services/verification/observation.py` `observe()` fetches the preview page over HTTP from the backend (snapshot + description). It records `detail.screenshot` / `evidence.path` as a reference string only.
- **Browser:** the sandbox image (`daytonaio/sandbox:0.8.0`, Debian 13) already ships `/usr/bin/chromium` 147. I verified it in `enigma-sandbox:latest`, running as the non-root `daytona` user with `--cap-drop=ALL --security-opt no-new-privileges`: `chromium --headless=new --no-sandbox --screenshot=… http://127.0.0.1:<port>/` writes a valid PNG. **No image change is needed.**
- **Reading files:** `SandboxRuntime.fs(workspace).download_file(path)` reads bytes on every provider, and absolute paths are kept as-is.
- **No object storage yet:** there is no S3 client dependency and no object store in compose or k8s.
- **Frontend:** `EvidenceCard` (`components/VerificationPanel.tsx`) prints `evidence.path` as text.

## Design

**Where to render:** inside the tenant's sandbox, never in the control plane. Tenant pages are untrusted, and rendering them in the backend or worker would hand a browser exploit the platform's credentials and network. Inside the sandbox, the app is reached on `127.0.0.1:<port>`.

**Capture** (`services/verification/screenshot.py`, `capture_screenshot(runtime, workspace, port) -> bytes`):
1. One `runtime.exec`:
   - `d=$(/usr/bin/mktemp -d) && /usr/bin/timeout 45 /usr/bin/chromium --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --hide-scrollbars --window-size=1280,800 --screenshot="$d/shot.png" http://127.0.0.1:<port>/ >/dev/null 2>&1; echo "$d"`
   - Absolute binary paths only, because `~/.local/bin` leads PATH and belongs to the tenant.
   - It uses a fresh `mktemp -d`. `--no-sandbox` is required because the container already drops every capability.
2. `fs.download_file(f"{d}/shot.png")`, then a best-effort `/bin/rm -rf "$d"`.
3. **Validation:** the bytes must start with the PNG signature and be ≤ `MAX_SCREENSHOT_BYTES` (2 MB). Otherwise `ScreenshotError`.

**Object store** (`services/artifact_store.py`):
- **Interface:** an `ArtifactStore` protocol (`put(key, data, content_type)`, `get(key) -> bytes`, `delete(key)`). `S3ArtifactStore` uses **`aioboto3`**, a proven client that works with AWS S3, R2 and RustFS/MinIO.
- **Settings:** `ARTIFACT_S3_ENDPOINT_URL` (empty = AWS), `ARTIFACT_S3_BUCKET`, `ARTIFACT_S3_REGION`, `ARTIFACT_S3_ACCESS_KEY_ID`, `ARTIFACT_S3_SECRET_ACCESS_KEY`. `get_artifact_store(settings)` returns `None` while no bucket is set.
- **Unconfigured:** screenshots are skipped with `screenshot_error: "artifact storage is not configured"`. Nothing else changes, so existing installs keep working.
- **Keys:** `verification/<workspace>/<run>/<evidence>.png`. The bucket stays private: objects are only ever read back through the authenticated API, and no presigned URLs are handed out.
- **Errors:** an S3 error is surfaced as `ArtifactStoreError`. Secrets never reach logs or responses.

**In `observe()`** (after a successful snapshot, and only when a store is configured):
1. Capture.
2. Insert the evidence.
3. `put` the object.
4. Insert a `verification_artifacts` row (migration `0044`):
   - `id`
   - `evidence_id` (FK `verification_evidence` `ON DELETE CASCADE`, unique)
   - `storage_key`
   - `content_type`
   - `byte_size`
   - `created_at`
5. Set `detail.screenshot_captured = true`.

Any failure leaves the observation **passed** on its snapshot, with `screenshot_captured = false` and `screenshot_error` (spec §6). The placeholder `screenshot` ref and `path` stay for compatibility.

**Serve:** `GET /workspaces/{id}/verification/runs/{run_id}/evidence/{evidence_id}/screenshot`.
- **Auth:** the same workspace access as the run detail. The run and evidence are scoped to the workspace in the URL (404 otherwise). It reads the artifact row, then `store.get(storage_key)`.
- **Response:** `image/png` with these headers:
  - `X-Content-Type-Options: nosniff`
  - `Content-Security-Policy: default-src 'none'`
  - `Cache-Control: private, max-age=300`
  - `Content-Disposition: inline`
- **Errors:** 503 if the store is unconfigured or unreachable.

**Dev and deploy:**
- **Compose:** an `artifact-store` service (`rustfs/rustfs:1.0.7`, because MinIO no longer publishes images: Docker Hub and quay.io deny pulls), a named volume, and a one-shot `artifact-store-init` (`amazon/aws-cli:2.42.3`) that creates the bucket. Both are under `--profile artifacts`. Backend and worker get `ARTIFACT_S3_*`, and `.env.example` documents them. The credentials are dev-only placeholders, not real secrets.
- **k8s:** the settings are documented in `deploy/README.md` (bucket + credentials via the existing Secret pattern, and an egress note for the network policy). The manifests themselves are a follow-up, so `tests/test_deployment.py` stays untouched.

**Frontend:**
- `getVerificationScreenshot(ws, run, evidence) -> Blob` (cookie auth via `fetch`).
- `EvidenceCard` shows the image (an object URL, revoked on unmount) when `detail.screenshot_captured`, with alt text from the description, or `screenshot_error` when not captured.

## Global Constraints

- **Migration:** `0044` is expand-only (a new table).
- **Async-only, no module-level state.** The store client is created per use, or held by the caller.
- **Containment:** no tenant content is rendered or parsed outside the sandbox beyond the PNG signature check.
- **Secrets:** S3 credentials come only from settings and never appear in evidence, responses or logs.

## Review Focus

- **Containment:** the browser runs only in the tenant sandbox, with absolute binary paths and a fresh temp dir.
- **Serving:** the endpoint is workspace-scoped, the bucket is private, there is no presigned or public URL, and only validated PNG is served.
- **Never blocking:** a failed capture or upload never fails the observation or the run. An unconfigured store is a clean skip.

---

### Task 1: Artifact metadata (migration `0044`)

**Files:** `alembic/versions/0044_verification_artifacts.py`, `models/verification.py`, model test.

- [x] Failing test: the artifact row round-trips and cascades with its evidence and run. Implement, `upgrade`/`downgrade`/`upgrade`, migration-safety green.

### Task 2: Object store

**Files:** `pyproject.toml` (`aioboto3`), `core/config.py`, `services/artifact_store.py`, tests (moto-free: a fake S3 via `aiobotocore` stubbing, or an in-memory store for callers).

- [x] Failing tests:
  - unconfigured → `None`
  - `S3ArtifactStore` put/get/delete against a stubbed client (bucket/key/content type passed through)
  - S3 errors → `ArtifactStoreError` without secrets in the message
- [x] Implement, green.

### Task 3: Capture a screenshot in the sandbox

**Files:** `services/verification/screenshot.py`, `tests/test_verification_screenshot.py`.

- [x] Failing tests:
  - absolute paths, `127.0.0.1:<port>`, returns the downloaded PNG, cleans up
  - non-PNG / oversize / exec failure / download error → `ScreenshotError`
- [x] Implement, green.

### Task 4: Observation stores it, never fails on it

**Files:** `services/verification/observation.py`, tests.

- [x] Failing tests:
  - captured + stored → an artifact row and object, `screenshot_captured: true`
  - capture or upload failure → `passed`, `screenshot_error`, no artifact row
  - no store → skip with reason
  - snapshot failure → no capture
- [x] Implement, green.

### Task 5: Serve it

**Files:** `api/verification.py`, tests.

- [x] Failing tests:
  - 200 `image/png`, the security headers, the exact bytes
  - 404 for no artifact, another workspace's run, or evidence of another run
  - 503 when the store is unavailable
- [x] Implement, green.

### Task 6: Show it

**Files:** `lib/api.ts`, `components/VerificationPanel.tsx`, tests.

- [x] Failing tests:
  - a captured observation renders an `<img>` from the blob
  - a failed capture shows the error
  - no fetch when not captured
- [x] Implement, green (`tsc -b`, `eslint`, `vitest`, `vite build`).

### Task 7: Dev stack + docs

- [x] Compose `artifact-store` (RustFS) + `artifact-store-init`, `ARTIFACT_S3_*` for backend/worker, `.env.example`, `deploy/README.md` notes.
- [x] AGENTS.md note 31 and `0044`, and tick this plan.
- [ ] Live check (optional, no LLM cost): a dev server in a sandbox → observation → object in the store → image in the Evidence view. *Partial:* the exact Chromium command was verified in `enigma-sandbox:latest` (non-root, `--cap-drop=ALL`, `no-new-privileges`), and `S3ArtifactStore` plus the compose bucket init were verified against a real RustFS 1.0.7. The end-to-end UI flow has not run yet.

## Review fixes (applied)

- **Never fails the run:** `S3ArtifactStore` wraps *any* client failure (aiohttp disconnects, a bad endpoint, botocore) as `ArtifactStoreError`. Before this, a non-botocore error escaped and turned an optional screenshot into a failed run.
- **Fails fast:** the client uses `AioConfig(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2})`. Without it, a dead store could hold the observing step for minutes.
- **Built once:** the store (and its aioboto3 session, which reads botocore's data files from disk) is cached per configuration. Each operation still opens its own client inside the calling task.
- **Size checked in the sandbox:** the capture command also prints `stat -L -c %s` of the PNG. Anything over 2 MB, or empty, is refused before it is downloaded (a download may be up to 64 MiB). The temp dir is now cleaned on every path once its name is known. The generated command was re-run against `enigma-sandbox:latest` and printed the dir and size, with a valid PNG.
- **Known:**
  - Objects are never deleted. Rows cascade with their run; use a bucket lifecycle rule on `verification/` (`deploy/README.md`).
  - The screenshot GET is not rate-limited, the same as the other read endpoints.

## Live E2E fix (2026-11-27)

The Plans 4–6 live E2E (portofnext) showed an empty hero. Chromium's `--screenshot` captures on `load`, which can fire before React hydrates and framer-motion reveals the content. On some pages `load` is held by third-party assets for 30–40 s, so the result was blank, slow or missing.

**What did not work:** `--virtual-time-budget` (stops animations midway) and `--force-prefers-reduced-motion`.

**The fix:** `cdp_capture.py` drives the same Chromium over `--remote-debugging-pipe` from the image's own Python (`-I -S`, stdlib only, source sent inline):
1. Sets a 1280×800 viewport.
2. Navigates, then waits for `load`, at most 20 s.
3. Sleeps 2 s of real time, then captures.
4. Kills the browser's process group, including on SIGTERM from `timeout`.

Verified live: 3/3 full-hero captures in about 5 s. The full-page option was declined: sections revealed on scroll stay blank without scrolling.

## Out of scope

- k8s manifests for the store (documented, follow-up), retention/lifecycle rules on the bucket, and deleting objects when a run is deleted (rows cascade, and objects are left for a bucket lifecycle rule).
- Full-page or multi-viewport captures, visual diffing, and giving the screenshot to the fix agent.
