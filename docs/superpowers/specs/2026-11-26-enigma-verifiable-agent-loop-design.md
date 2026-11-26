# Desain: Enigma — "Verifiable Agent Loop"

**Tanggal:** 2026-11-26
**Status:** Draft untuk review
**Ruang lingkup:** Konsep produk + arsitektur + keputusan runtime (adopsi OpenSandbox).
**Dasar:** Riset landskap (`docs/research/2026-11-26-software-factory-landscape.md`) dan spike OpenSandbox (`docs/research/2026-11-26-opensandbox-spike.md`).

---

## 1. Konteks & Masalah

Enigma saat ini adalah platform yang sangat luas (sandbox orchestrator, Git/PR, SDD, editor/terminal/preview, multi-tenancy). Kelebaran ini membuat fokusnya kabur. Riset landskap menunjukkan industri konvergen ke loop **Plan → Build → Review → Ship**; diferensiasi nyata ada pada tiga hal: *verifikasi perilaku*, *governance*, dan *provider-neutral*.

## 2. Visi & Posisi Produk

**One-liner:**
> Platform tempat AI coding agent bekerja sampai terbukti berfungsi — plan, tulis kode, jalankan, amati, perbaiki — lalu menyerahkan pada manusia untuk review.

**Posisi:** *Verifiable Agent Loop*. Agent punya "mata": ia mengamati perilaku runtime dan merekam bukti, bukan cuma menulis kode. "Berfungsi" = teramati (evidence), bukan klaim. Manusia tetap penentu akhir.

**Aset yang diandalkan** (sudah ada): preview proxy, sandbox runtime (`SandboxRuntime`), pipeline Spec Kit, git worktrees + PR.

## 3. Loop Inti

```
Plan ──► Generate ──► Self-test(Test → Observe → Iterate) ──► Human gate ──► PR ──► Merge
  ▲                                                                                     │
  └────────────────────────── gate "Plan" juga manusia ◄─────────────────────────────────┘
```

1. **Plan** — agent (atau Spec Kit) menghasilkan rencana/spesifikasi yang bisa direview manusia.
2. **Generate** — agent menulis kode di worktree fitur terisolasi.
3. **Self-test** (loop tertutup):
   - **Test** (objektif): jalankan suite/health-check; rekam pass/fail + output.
   - **Observe** (perilaku): buka preview, tangkap snapshot struktural + deskripsi (untuk agent) dan screenshot (untuk manusia).
   - **Iterate**: perbaiki sampai lolos.
4. **Human gate** — manusia menyetujui bukti.
5. **PR → Merge** — diserahkan sebagai PR; manusia memutuskan merge.

### Keputusan yang terkunci

| Aspek | Pilihan |
|---|---|
| Identitas | Arah A: Verifiable Agent Loop |
| Self-test | Gabungan: test otomatis (objektif) + observasi visual (perilaku) |
| Bukti visual | Snapshot terstruktur + deskripsi (untuk agent) + screenshot (untuk manusia) |
| Gate manusia | Hibrida: gate `Plan` + gate `Self-test` + review PR final |
| Eksekusi | Semua lewat `SandboxRuntime`; tidak ada runtime baru |
| Evidence | Model baru: `verification_run` + `verification_evidence` |

## 4. Arsitektur

### 4.1 Gambaran besar

```
┌──────────────────────────── Enigma Backend ────────────────────────────┐
│  Verification Orchestrator (loop) ──► SandboxRuntime (Daytona/OpenSandbox) │
│       │                          │                                     │
│       │  mulai run               ▼                                     │
│       ├──► agent_tools (write/read/run, terkurung)                     │
│       │                                                               │
│       ├──► TEST n: jalankan suite → rekam pass/fail + output           │
│       ├──► OBSERVE: buka preview → snapshot + deskripsi + screenshot   │
│       │            → simpan sebagai verification evidence              │
│       │                                                               │
│       └──► iterate sampai lolos → hand-off ke human gate + PR          │
└────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Komponen

1. **Verification Orchestrator** — mengendalikan loop `test → amati → perbaiki → keputusan`. Memanfaatkan run-registry & guard `satu run aktif per workspace` yang sudah ada.
2. **Evidence Store** — model baru:
   - `verification_run`: konteks iterasi (plan terkait, status, keputusan).
   - `verification_evidence`: item bukti (tipe `test`/`observation`/`screenshot`, hasil, output, snapshot, path, timestamp).
   - Tujuan: ditampilkan ke manusia di UI, dan jadi jejak audit permanen.
3. **Test Runner (adaptor)** — menjalankan suite di sandbox lewat `SandboxRuntime.exec`; normalisasi exit code + output jadi status.
4. **Observation Interface** — jembatan agent ↔ preview:
   - Agent meminta "ambilkan snapshot + screenshot" dari preview proxy (sudah ada).
   - Dikembalikan sebagai snapshot terstruktur (untuk model teks) + screenshot (untuk manusia).
5. **Human Evidence View (frontend)** — UI melihat bukti per iterasi sebelum menyetujui gate. Terintegrasi dengan alur Spec Kit yang ada.

### 4.3 Keputusan runtime: adopsi OpenSandbox

Berdasarkan spike, OpenSandbox diadopsi sebagai **runtime utama** (`runtime="opensandbox"`), menggantikan provider Docker buatan sendiri. Daytona tetap dipertahankan sebagai provider cloud opsional.

- `SandboxRuntime` abstraksi dipertahankan; OpenSandbox menjadi provider baru di `backend/app/services/sandbox/opensandbox.py`.
- Result hazard: `tests/sandbox/test_conformance.py` (tambahkan case `"opensandbox"`) + `tests/sandbox/test_boundary.py`.
- Migrasi bertahap: jangan hapus Daytona/Docker sampai adaptor lolos conformance.
- **Keamanan wajib sebelum produksi:** profile setara `cap_drop=ALL`/`pids_limit`/`no-new-privileges`, set `api_key`, verifikasi image (ada signing gap di upstream), uji PTY khusus.
- **Koneksi ke konsep:** browser/desktop/Playwright OpenSandbox dipakai untuk "mata" agent (observasi visual).

## 5. Alur Data

1. Orchestrator mendapat `plan` (hasil gate Plan).
2. Agent `generate` → tulis file di worktree.
3. Orchestrator panggil **Test Runner** → `SandboxRuntime.exec` → simpan `verification_evidence` (tipe `test`).
4. Jika test lolos, panggil **Observation Interface** → preview proxy → ambil snapshot + screenshot → simpan `verification_evidence` (tipe `observation`/`screenshot`).
5. Orchestrator menilai kriteria selesai → **gate Self-test** (manusia) → **PR**.
6. Semua evidence ditampilkan di **Human Evidence View**.

## 6. Penanganan Error

- `CommandTimeoutError` → tandai evidence `timeout`; orchestrator bisa iterasi atau berhenti.
- `SandboxNotFoundError` / `RuntimeUnavailableError` → dialihkan ke `SandboxRuntimeError` (mengikuti kontrak yang ada); evidence dicatat sebagai `error`.
- Kegagalan preview/observasi → jangan menghentikan run; tandai `observation_failed`, lanjut berdasarkan test.
- Gate manusia batal/menolak → update `verification_run.status = rejected`; orchestrator berhenti.

## 7. Pengujian

- **Conformance:** setiap provider (incl. OpenSandbox) lulus `test_conformance.py` + `test_boundary.py`.
- **Unit:** orchestrator (state machine loop), evidence store, adaptor test runner, adaptor observasi.
- **Integrasi:** alur end-to-end `plan → generate → test → observe → gate → PR` dengan provider fake.
- **Frontend:** Human Evidence View memakai komponen yang sudah ada (vitest/jsdom).

## 8. Lingkup & Urutan Implementasi (usulan)

Ketika rencana implementasi disusun (fase berikutnya), urutan yang disarankan:

1. Adaptor OpenSandbox (`runtime="opensandbox"`) + lulus conformance/boundary.
2. Evidence Store (`verification_run` + `verification_evidence`).
3. Test Runner (adaptor).
4. Observation Interface (agent ↔ preview).
5. Verification Orchestrator (loop).
6. Human Evidence View (frontend).

## 9. Matriks Preservasi Fitur

Komitmen eksplisit: konsep *Verifiable Agent Loop* **tidak memangkas fitur user-facing apa pun**. Bagian ini mencatat apa yang dipertahankan, apa yang berubah, dan apa yang diganti secara implementasi.

### 9.1 Fitur user-facing tetap dipertahankan

| Fitur/Permukaan | Status | Catatan |
|---|---|---|
| Repository (clone/commit/pull/push) | ✅ Dipertahankan | Tak tersentuh konsep ini |
| Worktrees | ✅ Dipertahankan | |
| Pull Requests | ✅ Dipertahankan | |
| Files | ✅ Dipertahankan | |
| Custom Commands | ✅ Dipertahankan | |
| Audit & Notifications | ✅ Dipertahankan | |
| Global LLM settings | ✅ Dipertahankan | |
| Editor (code-server) | ✅ Dipertahankan | Kendaraan, bukan janji produk |
| Terminal | ✅ Dipertahankan | Kendaraan |
| Preview | ✅ Dipertahankan | Menjadi inti "mata" agent |
| Spec Kit pipeline | ✅ Dipertahankan | Menjadi sumber `Plan` & gate |

### 9.2 Perubahan implementasi (bukan kehilangan fitur)

| Area | Perubahan | Risiko/migrasi |
|---|---|---|
| Provider runtime | Provider Docker buatan sendiri (`docker.py`, `docker_fs.py`, `docker_pty.py`, `port_relay.py`, `egress_proxy.py`, dll.) **digantikan** oleh OpenSandbox (`runtime="opensandbox"`) | Self-hosted Docker pindah jalur ke OpenSandbox; wajib lulus `test_conformance.py` + `test_boundary.py` sebelum cutover; uji PTY khusus |
| Daytona | ✅ Dipertahankan sebagai provider cloud opsional | — |

### 9.3 Perubahan posisi (bukan penghapusan)

- Fokus/pesan produk bergeser ke loop agent (plan → generate → self-test → review).
- Editor/terminal/preview tetap tersedia namun tidak dipromosikan sebagai inti produk.

## 10. Yang Secara Sadar TIDAK Masuk Lingkup

- Mengganti pipeline Spec Kit yang ada; Spec Kit tetap jadi sumber `Plan` & gate.
- Membangun runtime/sandbox baru.
- Menjalankan agent dengan akses tak terbatas ke host (tetap terisolasi per sesi).
- Fitur "IDE penuh" — editor/terminal adalah kendaraan, bukan janji produk.
