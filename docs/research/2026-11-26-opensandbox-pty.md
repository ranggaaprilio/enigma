# Riset: PTY OpenSandbox — status penangguhan (dibuka kembali)

**Tanggal:** 2026-11-26
**Status:** Menyimpulkan hasil spike PTY langsung terhadap server OpenSandbox (execd) — **penangguhan PTY dicabut, menjadi pekerjaan terdefinisi**.

## Ringkasan

Riset awal (spike Task 1) menangguhkan PTY karena SDK Python OpenSandbox (1.2.1) tidak mengekspos API PTY — hanya bash sessions (`create_session`/`run_in_session`). Riset lanjutan pada kode sumber & dokumentasi upstream (`components/execd/PTY.md`, `pty_ws.go`, `pty_session.go`, `specs/execd-api.yaml`) menemukan bahwa **execd memiliki PTY interaktif asli via WebSocket pada `/pty`** — diklasifikasikan sebagai *reference-implementation extension* yang belum masuk spec publik (sebabnya SDK tidak mengeksposnya).

## Protokol (diverifikasi di live server)

| Endpoint/Frame | Perilaku |
|---|---|
| `POST /pty` `{"cwd": "..."}` | Buat sesi; mengembalikan `session_id` (201). Shell baru mulai di WebSocket pertama |
| `GET /pty/{id}/ws` | Attach WebSocket; default PTY mode (`pty=0` = pipe mode) |
| frame `connected` (JSON) | `mode` (`pty`/`pipe`) dan `role` (`holder`/`viewer`) |
| stdin | binary `0x00` + raw bytes |
| stdout | binary `0x01` + bytes (stderr: `0x02` di pipe mode) |
| resize | JSON text `{"type":"resize","cols":120,"rows":40}` |
| signal/ping | JSON text `{"type":"signal","signal":"SIGINT"}`, `{"type":"ping"}` → `pong` |
| reconnect | `?since=<output_offset>` (dari `GET /pty/{id}`) = replay scrollback; `?takeover=1` evict holder lama |
| viewer | `?mode=viewer` — read-only, replay + live |
| end | frame JSON `exit` (`exit_code`) lalu socket tutup |
| minta | `DELETE /pty/{id}` (idempotent) |

## Hasil spike langsung (11/11 pass, server live)

1. ✅ `POST /pty` → session_id.
2. ✅ holder attach; `connected` role=holder mode=pty.
3. ✅ kirim `echo PTY-PROBE-$((1+1))` → stdout berisi `PTY-PROBE-2` (frame `0x01`).
4. ✅ `resize` → pong.
5. ✅ **disconnect + reconnect** (`since=<output_offset>`) → attach kedua sukses, dan perintah berikutnya (`echo STILL-ALIVE-$((3+4))`) mengembalikan `STILL-ALIVE-7` — **sesi shell bertahan melewati putus koneksi**.
6. ✅ `DELETE /pty/{id}` → 2xx; DELETE kedua → idempotent.

## Relevansi dengan kontrak Enigma

Kontrak `create_or_connect_pty`/`kill_pty`/`PtyHandle` membutuhkan: terminal interaktif asli, resize, input/output, dan **bertahan hidup melewati reload halaman** (reconnect). Execd memenuhi semuanya dengan her `since=<output_offset>` + `takeover`.

## Konsekuensi keputusan

- **Penangguhan PTY dicabut** — bukan lagi "tidak didukung", melainkan "perlu adaptor langsung ke API execd" (SDK belum mengeksposnya).
- **Risiko tersisa:** protokol `/pty` belum masuk spec publik (potensi drift antar-versi); perlu uji integrasi terhadap execd asli sebelum produksi; auth via `X-EXECD-ACCESS-TOKEN` (atau subprotocol untuk browser) perlu dihubungkan ke endpoint resolution.
- Ini adalah prasyarat sebelum OpenSandbox menjadi provider terminal.

## Sumber

- `components/execd/PTY.md` (upstream, raw).
- `components/execd/pkg/web/controller/pty_ws.go` + `pty_ws_test.go` + `pkg/web/router.go` (upstream).
- `specs/execd-api.yaml` (upstream; hanya bash sessions — PTY tidak tercantum => "reference-implementation extension").
- `docs/architecture/data-plane/execd.md` (upstream; "Interactive terminals — a real terminal (PTY) over WebSocket").
- Spike langsung `spike_pty.py` (`/tmp/opencode/opensandbox-spike/`) vs server OpenSandbox 1.2.1.
