# Spike: OpenSandbox vs. Kontrak `SandboxRuntime` Enigma

**Tanggal:** 2026-11-26
**Konteks:** Menilai kelayakan opsi 2 — mengadopsi `opensandbox-group/OpenSandbox` sebagai runtime utama (menggantikan provider Docker buatan sendiri), dengan Daytona sebagai provider cloud opsional.
**Metode:** Setup OpenSandbox server 1.2.1 lokal (Docker runtime, SQLite store) di `/tmp/opencode/opensandbox-spike`; uji langsung dengan Python SDK terhadap kontrak `backend/app/services/sandbox/base.py` + `tests/sandbox/test_conformance.py`.

---

## Ringkasan eksekutif

**OpenSandbox siap dipakai sebagai dasar runtime Enigma.** Semua operasi inti kontrak `SandboxRuntime` terbukti bekerja di live spike: provision, exec, streaming command, file round-trip, status, endpoint/preview, destroy. Fitur-fitur yang selama ini Enigma bangun manual (preview proxy, egress, isolasi, browser/desktop) tersedia native di OpenSandbox.

Terdapat **ketidakcocokan semantik kecil** pada `find_files` (content grep vs. glob) dan model sesi/PTY yang perlu diadaptasi. Tidak ada blokir keras.

## Hasil uji (live)

| Kontrak Enigma | Status | Catatan |
|---|---|---|
| `provision` → id | ✅ | `Sandbox.create()` menghasilkan id sandbox |
| `exec(command)` → exit_code/stdout/stderr | ✅ | `commands.run()` mengambil exit code + logs |
| `stream_command` (output real-time) | ✅ | `commands.run(handlers=ExecutionHandlers(on_stdout=…))`; stdout/init/complete mengalir live |
| `fs.upload_file/read/download` | ✅ | `write_files()` / `read_file()` / `read_bytes()` |
| `fs.list_files` | ✅ | `list_directory()` (perlu adaptor, EntryInfo) |
| `fs.get_file_info` | ✅ | `get_file_info([path])` → `EntryInfo` |
| `fs.find_files` (content grep) | ⚠️ | `search()` memakai **glob** (`*.txt`), bukan content grep. Perlu fallback `exec("grep -r …")` |
| `fs.move/delete/create_folder/set_perms` | ✅ | `move_files`, `delete_files`/`delete_directories`, `create_directories`, `set_permissions` |
| `status` | ✅ | `get_info()` → `SandboxStatus(state='Running')` |
| terminal / sesi | ⚠️ | `create_session`/`run_in_session` ada (bash session, streaming SSE). **Belum diverifikasi** PTY interaktif + resize + reconnect (kontrak `create_or_connect_pty`/`kill_pty`) |
| `preview_link(port)` | ✅ | `get_endpoint(port)` → `127.0.0.1:<hostport>/proxy/<port>`; `get_signed_endpoint(port, expires)` tersedia |
| `expose_port` | ✅ | endpoint dapat diminta per port; idempotent |
| `delete`/cleanup | ✅ | `destroy()` → server log `action=kill` + `remove` |
| error mapping | ⚠️ | `SandboxException` family ada; perlu di-map ke subtipe `SandboxRuntimeError` Enigma |

## Peta adaptor yang dibutuhkan

Untuk `runtime="opensandbox"` di `SandboxRuntime`, dibutuhkan adaptor (`backend/app/services/sandbox/opensandbox.py`) yang memetakan:

- `exec` → `commands.run()`; pastikan `timeout` → `opts.timeout`.
- `stream_command` → `commands.run(handlers=…)` dengan `on_stdout`/`on_stderr` → callback Enigma.
- `create_or_connect_pty` → kemungkinan `create_session` + `run_in_session`; **perlu uji terpisah** untuk perilaku PTY (resize, disconnect/reconnect) karena kontrak Enigma menuntut terminal yang bertahan hidup melewati reload halaman.
- `fs` → bungkus `FilesystemAdapter` (names beda: `list_directory`, `search`, `get_file_info([...])`), dan tambahkan `find_files` berbasis `exec("grep -r")` agar tetap content-search.
- `preview_link` → `get_endpoint(port)` (sudah mengembalikan URL host-mapped proxy); `expose_port` → panggil `get_endpoint(port)`.
- Perhatikan `get_file_info` mengembalikan dict ber-keys path (bukan satu objek); sesuaikan di adaptor.

## Keamanan & keselarasan dengan pola Enigma

- **`SandboxRuntime` abstraksi dipertahankan.** OpenSandbox menjadi satu provider baru; Daytona tetap ada. Pola boundary (`tests/sandbox/test_boundary.py`) dan conformance (`test_conformance.py`) tetap berlaku — tambahkan case `"opensandbox"`.
- **Isolasi:** OpenSandbox menawarkan gVisor / Kata / Firecracker; namun config Docker default di spike tidak membatasi `privileged`/caps secara eksplisit. Perlu **`assert_safe_container_spec` / profile keamanan setara** (cap_drop=ALL, pids_limit, no-new-privileges, network mode) sebelum produksi — ini mengikuti gotcha Docker Enigma yang sudah ada.
- **Egress/network:** egress policy tersedia (`get_egress_policy`, `patch_egress_rules`, `delete_egress_rules`, `network_mode=bridge`); perlu diselaraskan dengan jaringan `enigma-sbx-*`/egress proxy saat ini.
- **Kredensial:** `credential_vault` tersedia (suntik token tanpa membeberkan); guna mendukung pola token git/LLM Enigma.

## Risiko & catatan

- **Konfigurasi default:** `api_key` kosong (insecure mode dilewati via `OPENSANDBOX_INSECURE_SERVER=YES`). Produksi wajib set `api_key`.
- **Pengiriman image:** ada jejak *signing/provenance gap* yang tercatat di upstream (issue #2463) untuk image umbrella; pin by digest + verifikasi sebelum produksi.
- **Kematangan:** roadmap masih berisi volume/volume-binding, Go SDK, secure container — beberapa tersedia via proposal.
- **`search` ≠ content grep:** perlu fallback grep di adaptor (lihat tabel).
- **PTY:** belum diverifikasi penuh — ini satu-satunya area yang memerlukan uji tambahan sebelum menjadikan OpenSandbox runtime terminal.

## Keputusan yang direkomendasikan

1. **Lanjut ke desain** dengan OpenSandbox sebagai runtime utama (`runtime="opensandbox"`) — bukti spike mendukung.
2. **Adopsi bertahap:** pertahankan Daytona dan Docker provider selama transisi; jangan hapus hingga adaptor OpenSandbox lulus `test_conformance.py` + `test_boundary.py`.
3. **Tambahan wajib sebelum produksi:** profile keamanan setara, api_key, verifikasi image, dan uji PTY khusus.
4. **Fitur penguat konsep:** gunakan browser/desktop/Playwright OpenSandbox untuk "mata" agent (observasi visual) pada loop Verifiable Agent.

## Lingkungan spike

- OpenSandbox server `1.2.1` (`opensandbox-server`, `opensandbox` SDK, `opensandbox-cli`), Python 3.12.14 (uv), Docker 29.9.2.
- Config: `sandbox.toml` (docker runtime, sqlite store, `network_mode=bridge`, port 8080).
- Verifikasi manual: log server menampilkan create / kill / remove sandbox; HTTP 204 pada DELETE.
