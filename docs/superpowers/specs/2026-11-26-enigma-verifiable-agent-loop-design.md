# Desain: Enigma — "Verifiable Agent Loop"

**Tanggal:** 2026-11-26
**Status:** Draft untuk review (direvisi 2026-11-28: arah *lean* dan peran agent, lihat §9 dan §11–§14)
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
Prepare ──► Plan ──► Generate ──► Self-test(Test → Observe → Iterate) ──► Human gate ──► PR ──► Merge
 (sekali      ▲                                                                              │
  per env)    └────────────────────── gate "Plan" juga manusia ◄──────────────────────────────┘
```

0. **Prepare** (revisi 2026-11-28, §11) — agent menyiapkan environment sandbox sendiri (runtime, dependensi, database, command) dan membuktikannya jalan. Disetujui manusia sekali per environment.
1. **Plan** — agent (atau Spec Kit) menghasilkan rencana/spesifikasi yang bisa direview manusia, **termasuk apa yang akan dibuktikan** (test command dan, untuk backend, daftar probe API — §12).
2. **Generate** — agent menulis kode di worktree fitur terisolasi.
3. **Self-test** (loop tertutup):
   - **Test** (objektif): jalankan suite/health-check; rekam pass/fail + output.
   - **Observe** (perilaku): Enigma menyalakan service `dev` sendiri, lalu mengamati. Dua bentuk (§12): **halaman** (snapshot struktural + deskripsi untuk agent, screenshot untuk manusia) dan **probe API** (request/response nyata) untuk backend tanpa UI.
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

## 9. Matriks Fitur: Pertahankan / Pangkas / Ubah / Tambah

> **Revisi 2026-11-28.** Versi awal bagian ini berkomitmen "tidak memangkas fitur user-facing apa pun". Komitmen itu **dicabut**: target sekarang adalah Enigma yang *lean*, di mana setiap fitur harus dipakai oleh loop inti (§3) atau oleh governance-nya. Ukurannya satu: *apakah loop memakai fitur ini?*
>
> Pangkas dan tambah dieksekusi **terpisah**:
> - **Track A — Pangkas & fokus ulang:** `docs/superpowers/plans/2026-11-28-enigma-lean-cut.md` (Plan 10)
> - **Track B — Tambah:** `docs/superpowers/plans/2026-11-28-enigma-loop-additions.md` (Plan 11)
> - **Track C — Peran agent:** `docs/superpowers/plans/2026-11-28-enigma-agent-roles.md` (Plan 12; mulai setelah B5 dan B6)
> - **Track D — Satu tim, satu mode eksekusi:** `docs/superpowers/plans/2026-11-28-enigma-single-team.md` (Plan 13; dijalankan paling awal)
>
> Peta ketergantungan antar-track dan pemilik tiap bagian yang dipakai bersama ada di §14.

### 9.1 Dipertahankan (inti loop + governance)

| Fitur/Permukaan | Peran di loop |
|---|---|
| Agent runs + sessions | Generate; fix di percakapan dan branch yang sama (Plan 9) |
| Spec Kit pipeline | Sumber `Plan` dan gate |
| Verification (runs, evidence, screenshot) | Self-test dan gate bukti |
| Worktrees, Repository, git providers, Pull Requests | Isolasi kerja dan serah-terima |
| Custom Commands (role `install`/`migrate`/`seed`/`dev`/`test`) | Kontrak environment: diisi agent di tahap Prepare, dipakai Test dan Observe |
| Workspace services (inti: start/stop/ensure-running) | Menyalakan app dan layanan pendukung (mis. Postgres) untuk Observe |
| Preview proxy + capability host | "Mata" agent; URL preview yang aman |
| Files, Terminal | Kendaraan manusia untuk turun tangan |
| Sandbox runtime (OpenSandbox, Daytona) | Semua eksekusi |
| Auth, Global LLM settings, Budgets, Audit, Notifications | Governance; notifikasi memberi tahu gate yang menunggu |

### 9.2 Dipangkas (Track A)

| Fitur | Alasan | Langkah |
|---|---|---|
| Presence | Lapisan sosial; tidak ada test; loop tidak memakainya | A1 |
| Comments | Gate sudah punya approve / *Request changes* | A2 |
| Panel Usage di navigasi workspace dan dashboard utama | Penegakan budget tetap; grafiknya cukup satu tempat | A3 |
| Editor (code-server) + editor alias | "Kendaraan, bukan janji produk"; Files + Terminal cukup; menghapus kebutuhan code-server di image | A4 |
| Facade `/dev/*` lama (`dev_server`) | Duplikat workspace services | A5 |
| Lapisan penasihat preview: proxy detection di composition, CORS diagnostics, Wiring Advisor | Alat debugging dev-server untuk manusia; Observe tidak memakainya | A6 |
| Preview alias (hostname stabil opt-in) | Postur keamanan lebih lemah, tidak dipakai loop | A7 |
| `run_detection`, `run_presets`, `proxy_detection`, tab Run Configuration | Deteksi statis dari manifest digantikan agent yang benar-benar mencoba (Prepare) | A8 (setelah B3–B5 dan B7) |
| Tabel/kolom yatim dari langkah di atas | Migrasi `# contract:` satu rilis kemudian | A10 |

### 9.3 Diubah (perilaku dan UI/UX)

| Area | Sekarang | Menjadi | Langkah |
|---|---|---|---|
| Observe | Berasumsi sudah ada yang listen di `observe_port` | Orchestrator menyalakan service `dev` dulu | B1 |
| Auto-verify | Opt-in, default mati (Plan 9) | Default nyala untuk workspace baru bila kriteria ada | B2 |
| Sidebar workspace | 12 item datar | Grup **Loop** (Overview · Spec · Agent · Verify · Repository/PR) di atas, **Tools** (Files · Terminal · Preview · Commands) di bawah, Audit terpisah | A9 |
| Landing page | "Control room" yang bisa dilihat | Posisi *Verifiable Agent Loop*: bukti, bukan klaim | A9 |
| Dashboard utama | Kesehatan infra + usage | Antrian "menunggu review Anda" lintas workspace; health pindah ke Settings | B8 |
| Panel Verify | Form manual (command, port, cwd) sebagai pintu utama | Bukti per run sebagai pintu utama; form jadi lanjutan | B7 |

### 9.4 Ditambah (Track B dan Track C)

| Tambahan | Ringkas | Bagian | Langkah |
|---|---|---|---|
| Tahap Prepare (mode setup) | Agent menyiapkan environment sendiri | §11 | B3 |
| Tool `register_command` | Agent mendaftarkan command lewat service, muncul di UI | §11 | B4 |
| Bukti dan gate environment | Setup dibuktikan, disetujui sekali | §11 | B5 |
| Probe API | Observe untuk backend tanpa UI | §12 | B6, B7 |
| Antrian review | Halaman depan = gate yang menunggu | §9.3 | B8 |
| Peran agent: minta → rutekan → jawab | Developer meminta, orchestrator merutekan, devops/QA/architect menjawab; reviewer sebelum gate | §13 | C1–C3, C4a, C4b, C5, C7, C8 |
| Tampilan lantai kerja 2D | Bot terlihat bekerja | §13 | C6 |

### 9.5 Target penggunaan: self-hosted, satu tim (diputuskan 2026-11-28)

Enigma ditujukan untuk **satu tim yang menjalankannya sendiri**, bukan platform multi-tenant. Keputusan ini mengubah status dua hal yang sebelumnya dibekukan atau ditunda:

| Area | Status sebelumnya | Status sekarang |
|---|---|---|
| Mode queue: worker arq, jobs, leases, reaper, event bus Redis; worker dan KEDA di Kubernetes | Dibekukan | **Dipangkas** — D1, D2, D3 |
| API di Kubernetes | 2–10 replika dengan autoscaler | **Satu replika**, tanpa autoscaler (D3): API kini menjalankan run, dan dua replika akan saling mematikan run |
| Deploy saat ada run aktif | Worker menunggu sampai ±2 jam | API menunggu 5 menit, lalu run berakhir `interrupted_by_restart`; deploy dilakukan saat sepi |
| Batas waktu run (`agent_run_timeout_seconds`) | Hanya berlaku di worker | Dihapus; run inline memang tidak pernah punya batas waktu |
| Hierarki Project → Team → Workspace | Keputusan tertunda | **Tetap di model data, disembunyikan untuk satu tim** — D4 |
| Redis | — | **Tetap**, hanya untuk rate limit |
| Manifest Kubernetes dasar, tracing, retensi, cap run, opsi PgBouncer | Dibekukan | Tetap; tidak membebani langkah lain |

**Kenapa hierarki tidak diratakan.** Akses selain pemilik ke sebuah workspace *adalah* keanggotaan team (`services/access.py`), dan budget ber-scope team. Meratakannya berarti migrasi data dan model akses baru. Project dan team default yang dibuat otomatis, ditambah UI yang menyembunyikan levelnya selama hanya ada satu, memberi hampir semua manfaatnya tanpa mengubah skema.

**Komunikasi antar bot tidak memakai Redis.** Permintaan dan jawaban (§13) adalah baris di Postgres, dan peran berjalan bergantian dalam satu proses.

Sampai D1 dan D2 mendarat, langkah di Track A, B dan C harus benar di kedua mode eksekusi, karena test yang ada menjaganya. Karena itu Track D dijalankan **paling awal**: setelahnya, langkah B dan C cukup benar di satu mode.

### 9.6 Keputusan tertunda

- **Templates / blank workspace.** Kecil; dibiarkan sampai ada alasan memangkas.
- **Manifest Kubernetes dasar** dan **Redis untuk rate limit**: dibiarkan; pangkas hanya bila terbukti membebani.
- **Meratakan hierarki di database**: hanya bila D4 ternyata tidak cukup.

## 10. Yang Secara Sadar TIDAK Masuk Lingkup

- Mengganti pipeline Spec Kit yang ada; Spec Kit tetap jadi sumber `Plan` & gate.
- Membangun runtime/sandbox baru.
- Menjalankan agent dengan akses tak terbatas ke host (tetap terisolasi per sesi).
- Fitur "IDE penuh" — editor/terminal adalah kendaraan, bukan janji produk.
- Root di dalam sandbox, Docker-in-sandbox, atau melonggarkan lantai keamanan OpenSandbox demi instalasi (§11.2).
- Percakapan bebas antar agent; orchestrator berbasis LLM (§13).

## 11. Tahap Prepare: Agent Menyiapkan Environment Sendiri

### 11.1 Tujuan

Sebelum kode bisa dibuktikan jalan, environment-nya harus terbukti jalan. Enigma diberi keleluasaan mengatur resource di dalam sandbox-nya: memasang runtime dan dependensi, menyalakan layanan pendukung (mis. Postgres untuk live test), menulis `mise.toml`, dan mendaftarkan command yang perlu dijalankan agar muncul di UI.

### 11.2 Batas yang tidak berubah

- **Tanpa root.** Lantai keamanan OpenSandbox (non-root, `CapEff 0`, `NoNewPrivs 1`) tetap. Akibatnya `apt install` tidak tersedia; semua instalasi di userspace (mise, package manager bahasa, binary siap pakai).
- **Tanpa Docker di dalam sandbox.** Project yang bergantung pada `docker compose`/Testcontainers harus diterjemahkan agent menjadi proses biasa.
- **Egress** tetap mengikuti `SANDBOX_NETWORK_*`; bila allow-list dipakai, host unduhan harus masuk daftar.

**Bukti kelayakan (2026-11-28).** Di image `enigma-sandbox:latest`, dijalankan lewat podman dengan `--cap-drop=ALL --security-opt no-new-privileges` (uid 1001, `CapEff 0`, `NoNewPrivs 1`): PostgreSQL 16.16 terpasang lewat micromamba (conda-forge), `initdb`, start di `127.0.0.1:5432`, lalu `CREATE TABLE`/`INSERT`/`SELECT` berhasil, total **10 detik**. Karena itu Postgres **tidak** di-bake ke image. Belum teruji: lewat server OpenSandbox dengan kebijakan egress-nya, layanan selain Postgres, dan persistensi data saat sandbox di-pause/resume.

### 11.3 Komponen

1. **Mode setup untuk agent run.** Kolom `agent_runs.role` (`developer` | `devops`); *setup run* = run ber-`role="devops"`. Kolomnya sengaja dinamai `role` sejak awal karena Track C menambah `qa` dan `reviewer` ke kolom yang sama. Mode setup menjalankan command apa adanya di shell sandbox (package manager, `curl`, pipeline). Konsekuensinya harus dinyatakan terus terang: **di mode setup, lantai keamanan sandbox dan kebijakan egress adalah satu-satunya batas**; penyaring command tidak bisa menahan pipeline, jadi penolakan `sudo`/`docker` di tingkat Enigma hanya pesan galat cepat, bukan pengaman. Bila operator mengaktifkan allowlist command, mode setup ditolak. Run `developer` (semua run yang ada hari ini) tidak berubah.
2. **Tool `register_command`** (hanya mode setup). Agent tidak menyentuh database; tool memanggil `CommandService.create_command`, sehingga tervalidasi dan ter-audit. Baris yang dibuat ditandai asal-usulnya (`proposed_by_agent_run_id`) dan **nonaktif** sampai environment disetujui.
3. **`mise.toml`** ditulis agent sebagai file biasa di repo dan ikut ter-commit, supaya environment bisa direproduksi.
4. **Bukti setup.** Setup selesai bukan saat agent berkata selesai, melainkan saat Enigma sendiri mengeksekusi kontraknya: command `install`/`migrate`/`seed` exit 0, service `dev` (dan layanan pendukung) listen di port-nya, test command bisa dieksekusi. Hasilnya disimpan sebagai evidence pada verification run ber-`kind="setup"`.
5. **Gate environment.** Manusia menyetujui sekali; persetujuan mengaktifkan command yang diusulkan. Penolakan membiarkannya nonaktif.

### 11.4 Alur

`POST` setup run → agent memeriksa repo → memasang yang perlu → menulis `mise.toml` → `register_command` untuk tiap langkah → Enigma menjalankan bukti setup → gate environment → command aktif → loop §3 memakainya.

## 12. Observe untuk Backend: Probe API

Untuk project tanpa UI, yang diamati bukan tampilan melainkan perilaku service yang benar-benar menyala dengan database sungguhan.

- **Probe** = satu request HTTP dengan harapannya: `method`, `path`, `body` opsional, `expect_status`, `expect_json_contains` opsional. Daftar probe adalah bagian dari kriteria run (`PlanCriteria.probes`).
- **Enigma yang menjalankan probe, bukan agent**, melalui jalur yang sama dengan Observe halaman (link preview sandbox), sehingga hasilnya bukti, bukan klaim.
- **Keamanan:** di OpenSandbox, URL preview berbagi origin dengan API execd dan request membawa token execd. Karena itu path probe ditempelkan di belakang path preview (bukan di-join), divalidasi terhadap dot-segment, dan redirect tidak pernah diikuti.
- **Evidence** tetap `kind="observation"`; `detail.probes` berisi request, status, potongan response (dibatasi), dan lolos/gagal per probe. Tidak ada kind baru.
- **Kegagalan probe** diperlakukan seperti test gagal: masuk loop fix selama iterasi tersisa. Kegagalan *menjangkau* service tetap `observation_failed` dan tidak menghentikan run (§6).
- **Gate Plan** ikut meninjau daftar probe sebelum kode ditulis, karena agent yang menulis ujiannya sendiri cenderung menulis ujian yang mudah (lihat peran QA, §13).
- Isi response probe **tidak pernah** masuk ke body PR, sama seperti output test.

Project fullstack memakai dua bentuk Observe sekaligus.

## 13. Peran Agent: Minta → Rutekan → Jawab (Track C)

Tiap bot punya satu tugas yang jelas. "Bot" = satu agent run dengan tiga hal yang berbeda per peran: instruksi, izin tool, dan artefak yang wajib dihasilkan. Bot yang butuh sesuatu di luar tugasnya **meminta**; orchestrator **merutekan** ke peran pemiliknya; **jawaban** kembali ke percakapan si peminta.

**Contoh acuan.** Developer bot butuh Redis. Ia tidak memasangnya (tidak punya izin), melainkan memanggil `request_help(role="devops", need="Redis di 6379 untuk cache session")` dan gilirannya berakhir. Orchestrator memulai devops bot. Devops bot memasang Redis dan mendaftarkan command-nya; Enigma membuktikan Redis listen. Orchestrator melanjutkan percakapan developer: "Redis jalan di 127.0.0.1:6379".

### 13.1 Peran

| Peran | Tugas | Boleh | Tidak boleh | Dimulai oleh | Langkah |
|---|---|---|---|---|---|
| Orchestrator | Merutekan permintaan, menjaga urutan dan gate | — (kode, bukan LLM) | Melewati gate | Sistem | C3 |
| Architect | Rencana/spesifikasi; menjawab konsultasi | Menulis artefak spec (fase `plan`); saat dikonsultasi: baca file, `record_decision` | Menulis kode; mengubah plan yang sudah disetujui | Spec Kit (fase `plan`) atau permintaan | sudah ada; C7 |
| Developer | Menulis kode, memperbaiki | Baca/tulis file, command terbatas, `request_help` | Memasang software | Manusia, loop fix | sudah ada; C2 |
| DevOps | Menyiapkan environment | Shell penuh di sandbox, `register_command` | Commit git | Manusia (B3) atau permintaan (C3) | B3–B5 |
| QA | Menulis probe/skenario uji | Baca file, command terbatas, `propose_probes` | Menulis kode | Permintaan (C4a) atau gate Spec Kit (C4b) | C4a, C4b |
| Reviewer | Membaca diff sebelum gate | Baca file, git diff, `report_finding` | Menulis apa pun, menjalankan command | Loop verifikasi | C5 |

### 13.2 Keputusan (disetujui 2026-11-28)

- **Orchestrator adalah kode.** Permintaan sudah menyebut peran tujuannya, jadi perutean pasti, murah, dan tidak bisa melewati gate. Di tampilan ia muncul sebagai orchestrator bot.
- **Bergantian, bukan paralel.** Satu run aktif per workspace tetap; serah-terima tidak butuh paralel. Karena run tidak punya status "menunggu", developer yang meminta *mengakhiri* gilirannya dan dilanjutkan di session yang sama saat jawaban tiba.
- **Komunikasi = permintaan dan jawaban yang tersimpan** (`agent_requests`), bukan obrolan bebas. Semua terlihat manusia di gate.
- **Perubahan environment di tengah tugas:** yang bersifat **penambahan** aktif otomatis setelah terbukti, dan tampil sebagai bukti di gate akhir. "Penambahan" berarti: role `install`, `migrate`, `seed`, atau layanan pendukung (`custom` + service), dan belum ada command aktif dengan role dan nama yang sama. Command `dev` (aplikasinya) dan `test` (suite-nya) tidak pernah dianggap penambahan dan menunggu manusia. Gate environment pertama (§11) tetap disetujui manusia: selama gate itu masih menunggu, permintaan ikut menunggu, dan setup hasil permintaan tidak pernah menggeser setup yang dimulai manusia.
- **Hanya run developer yang diverifikasi otomatis.** Run QA, reviewer, architect, dan devops tidak memicu verifikasi perubahan.
- **Jawaban sampai tepat sekali.** Jika run jawaban tidak bisa dimulai (mis. budget habis), jawabannya dikirim ke manusia sebagai notifikasi.
- **Pengaman:** maksimal 5 permintaan per session; satu permintaan terbuka per workspace; tidak ada rantai (peran penjawab tidak bisa meminta lagi); permintaan yang gagal atau ditolak tetap dijawab ke developer.
- **Konteks architect = artefak tersimpan** (constitution, spec, plan, tasks, catatan keputusan, kode), bukan ingatan percakapan: run fase Spec Kit tidak punya session untuk dilanjutkan, dan jawaban yang hanya hidup di percakapan tidak bisa diperiksa manusia. Setiap jawaban konsultasi ditulis ke `decisions.md` di folder spec, sehingga peran lain membaca keputusan yang sama.
- **Klarifikasi vs perubahan plan.** Hal yang tidak diatur plan boleh diputuskan architect dan tampil sebagai bukti. Hal yang bertentangan dengan plan yang sudah disetujui **tidak pernah** diputuskan antar bot: permintaan ditolak, manusia diberi tahu, dan perubahan lewat *Request changes* Spec Kit yang sudah ada.
- **Permintaan dari dalam Spec Kit.** Run `implement` bisa meminta bantuan (C8); fase tidak dianggap selesai dan verifikasi tidak dimulai selama run hanya sedang menunggu jawaban.
- **QA dan Reviewer opt-in per workspace**, karena menambah satu run per perubahan. Saat mati, loop persis seperti sebelumnya.

### 13.3 Tampilan lantai kerja (2D)

Satu stasiun per peran, statusnya (diam / bekerja / menunggu), apa yang sedang dikerjakan, garis permintaan dari peminta ke orchestrator ke penjawab, dan artefak yang bergerak sepanjang loop. Digambar dari data yang sudah mengalir (run, event, permintaan, bukti) dengan SVG biasa; tidak menambah state di backend dan tetap bermakna tanpa animasi.

### 13.4 Sengaja di luar Track C

Run paralel per worktree (butuh service, port, dan database test per bot); obrolan bebas antar bot; orchestrator LLM; rantai permintaan; tampilan 3D (data sama dengan 2D, bisa menggantikan gambarnya nanti); peran Refactor dan Design.

## 14. Peta Eksekusi: Ketergantungan dan Kepemilikan

Tiga track, masing-masing satu file plan, bisa dieksekusi terpisah. Bagian ini adalah satu-satunya tempat ketergantungan antar-track dan kepemilikan dicatat; plan merujuk ke sini.

### 14.1 Urutan

```
Uji kelayakan: agent menyiapkan environment di 3 repo nyata (tanpa menulis fitur)
Plan 9 (PR #37)
   │
   ├─ Track D:  D0 ─► D3 ─► D1 ─► D2      D4 (kapan saja)
   │
   ├─ Track A:  A1 A2 A3 A5        A4 ─► A6 ─► A7      A9 (setelah A3, A4)
   │                                                        A8 ◄── B3–B5, B7      A10 (satu rilis kemudian)
   │
   ├─ Track B:  B1 ─► B6 ─┐
   │            B2         ├─► B7
   │            B3 ─► B4 ─► B5 ─┘
   │            B8
   │
   └─ Track C:  (setelah B5, B6)   C1 ─► C2 ─► C3 ─► C4a ─► C4b
                                                  C3 ─► C6, C7, C8
                                    C1 ─► C5  (juga setelah B7)
```

0. **Uji kelayakan** sebelum B3–B5 dibangun: jalankan agent dengan kebijakan command dilonggarkan (`AGENT_COMMAND_DENY_DANGEROUS=false`, setting yang sudah ada) di tiga repo nyata yang butuh database, minta ia menyiapkan environment, dan catat berapa kali berhasil. Hasilnya menentukan apakah Prepare dibangun seperti dirancang di §11.
1. Plan 9.
2. **Track D** D0 → D3 → D1 → D2 (D4 kapan saja). Urutannya dipaksa kode: manifest Kubernetes saat ini meminta mode queue, jadi Kubernetes diubah dulu (D3) sebelum mode itu ditolak (D1).
3. Track A (A1–A7, A9) dan Track B (B1–B8) berjalan tanpa saling menunggu.
4. A8 setelah B3–B5 dan B7; A10 satu rilis setelah langkah pangkas terakhir.
5. Track C setelah B5 dan B6.

### 14.2 Ketergantungan lintas track (hanya ini)

| Langkah | Menunggu | Alasan |
|---|---|---|
| A8 | B3, B4, B5, B7 | Deteksi command statis baru boleh dihapus setelah agent bisa menyiapkan environment dan UI-nya ada |
| A10 | D2 (lunak) | Migrasi kontrak yang sama membuang kolom queue bila D2 sudah satu rilis |
| Semua langkah B dan C | D1, D2 (disarankan, bukan wajib) | Setelah mode queue hilang, bagian queue di tiap langkah tidak berlaku lagi |
| C1 | B3, B4 | Memakai kolom `role`, modul profil peran, dan daftar tool per run |
| C3 | B4, B5 | Memakai usulan command, bukti setup, dan dispatch finaliser |
| C4a | B6 | Memakai skema dan eksekutor probe |
| C5 | B5, B6, B7 | Melebarkan kind evidence setelah B5, memakai prompt fix B6, menambah kartu di panel B7 |
| C6 | B3 | Membaca `role` dari respons agent run |
| C6 | A9 (lunak) | Menaruh entri di grup **Loop** sidebar; bila A9 belum ada, entri ditambah ke daftar datar |

### 14.3 Kepemilikan bagian yang dipakai bersama

Setiap baris punya **satu** pemilik yang membuatnya; langkah lain hanya menambah, tidak mendefinisikan ulang.

| Bagian | Dibuat oleh | Ditambah oleh |
|---|---|---|
| `agent_runs.role` | B3 (`developer`, `devops`) | C1 (`qa`, `reviewer`, `architect` — satu kali pelebaran untuk seluruh track) |
| `services/agent_roles.py` (profil peran) | B3 | C1, C2, C4a, C5, C7 (tool per peran) |
| `agent_requests`, tool `request_help`, daftar tujuan yang diizinkan | C2 (`devops`) | C4a (`qa`), C7 (`architect`), C8 (dibuka untuk run `implement`) |
| Tabel perutean di `services/agent_orchestration.py` | C3 | C4a, C7 |
| `decisions.md` di folder spec | C7 | — (peran lain hanya membaca) |
| Finaliser fase Spec Kit (`_finalize_phase_inner`) dan session untuk `implement` | C8 | — (aturan gate di gotcha 28 tidak berubah) |
| Daftar tool per run (`openhands_provider.py`) | B4 | C1 |
| Dispatch finaliser `_after_completed` (urutan: peran → permintaan tertunda → auto-verify) | B5 | C3 |
| Siapa yang diverifikasi otomatis (`agent_link.verifiable_agent_run`) | Plan 9 | C1 (hanya profil `auto_verify`) |
| `verification_runs.kind`, `setup_agent_run_id` | B5 | C3 (hanya membaca) |
| Aturan ganti/geser setup (per role **dan** nama; geser hanya antar setup yang dimulai manusia) | B5 | C3 (menambah auto-setuju, tidak mengubah aturan B5) |
| `verification_runs.auto_approved_reason` | C3 | — |
| `verification_runs.reviewer_run_id` | C5 | — |
| `services/verification/orchestrator.py` (langkah `advance`, `end`) | sudah ada | B1, B5, B6, C5 — berurutan, selalu rebase |
| Prompt fix di `services/verification/fixer.py` | sudah ada | B6 (probe gagal), C5 (temuan review) |
| `services/openhands_tools.py` (definisi tool) | sudah ada | B4, C2, C4a, C5, C7 |
| `services/reconciliation.py` (sapuan saat startup) | sudah ada | D2 (hapus varian per-worker), B5 (run setup), C3 (permintaan), C5 (`reviewing`) — satu fungsi per pass |
| `services/reaper.py`, `services/leases.py`, `services/jobs.py`, `app/worker.py`, `RedisEventBus` | sudah ada | **D1, D2 menghapusnya**; bila sudah hilang, B5/C3/C5 tidak menambah apa pun ke reaper |
| Empat titik mulai run (`agents.py`, `commands.py`, `speckit.py`, `verification/execution.py`) | sudah ada | D1 (hapus cabang queue); B5, C3, C8 menambah di titik yang sama — rebase |
| `services/capacity.py` | sudah ada | D2 (hapus kedalaman antrean; cap run tetap) |
| `docker-compose.yml`, `docker-compose.opensandbox.yml`, `deploy/k8s/`, `tests/test_deployment.py` | sudah ada | D3 (Kubernetes: worker, KEDA, satu replika API), D1 (worker di compose); A4 (sebutan code-server) |
| Fixture test bersama (`tests/run_fixtures.py`) | D0 | Semua track memakainya |
| Migrasi kontrak (satu-satunya) | A10 | D2 menitipkan kolom queue |
| Project/team default, pendaftar berikutnya masuk sebagai `viewer`, UI yang menyembunyikan hierarki | D4 | A9 dan D4 sama-sama mengubah `AppSidebar.tsx` — rebase |
| Port untuk Observe/probe pada kriteria turunan (`spec_link`, `agent_link`) | B6 | C4a, C4b (menggabungkan probe QA) |
| `agent_sessions.proposed_criteria` | C4a | — |
| `spec_workflows.qa_*`, `workspaces.qa_before_implement`, cek di `_assert_prerequisites` | C4b | — |
| `agent_runs.allow_requests` | C2 | C8 (run `implement` mengisinya) |
| Kind evidence | B5 (`setup`) | C5 (`review`) |
| Status verification run | — (tidak berubah di Track B) | C5 (`reviewing`) |
| `PlanCriteria.probes` + eksekutor | B6 | C4a, C4b (hanya memakai) |
| `check_preview_path` dipindah ke modul bersama | B6 | A7 memakai lokasi baru bila mendarat belakangan |
| Inti primary service di `WorkspaceServiceManager` | sudah ada | A5 **tidak boleh menghapusnya**; B1 dan B5 memakainya |
| `app/preview_proxy.py` | — | A4 → A6 → A7 berurutan (file yang sama) |
| `components/AppSidebar.tsx` | A9 (grup) | A3, A4 (hapus entri), C6 (entri Floor) |
| `src/App.tsx` (route) | — | A3, A4 (hapus route), C6 (route Floor) |
| `components/SpecPanel/` | sudah ada | A2 (hapus komentar), C4b (kartu QA) |
| `routes/DashboardOverviewRoute.tsx` | B8 (antrian review) | A3 (hapus Usage); yang mendarat belakangan me-rebase |
| `components/VerificationPanel.tsx` | B7 | C4a (label probe dari QA), C5 (kartu temuan review, status `reviewing`) |
| `CustomCommandsPanel.tsx` | B7 (lencana usulan agent) | A8 (hapus tab Run Configuration) |
| `api/router.py`, `lib/api.ts` | — | Hampir semua langkah; selalu rebase |
| Nomor migrasi | Diberi saat merge, berurutan dari head saat itu | Nomor di plan (`0046`…) adalah perkiraan; bila track lain mendarat lebih dulu, nomori ulang |
| `AGENTS.md` | — | Setiap langkah, untuk bagiannya sendiri |

### 14.4 Yang tidak boleh tumpang tindih

- **Izin memasang software** hanya milik peran devops (B3). Track C tidak memberi izin itu ke peran lain.
- **Gate environment** didefinisikan di B5; C3 hanya menambah aturan auto-setuju untuk perubahan yang bersifat penambahan dan dimulai oleh permintaan. Setup hasil permintaan tidak menggeser setup yang dimulai manusia, dan tidak menonaktifkan command dengan nama lain.
- **Probe** didefinisikan di B6; C4a dan C4b hanya menentukan *siapa* yang menulisnya.
- **Plan yang sudah disetujui** hanya berubah lewat gate Spec Kit yang ada; C7 tidak menambah jalur lain.
- **Track A tidak menambah kemampuan; Track B dan C tidak menghapus fitur.** Bila sebuah langkah butuh keduanya, itu dua PR di dua track.
