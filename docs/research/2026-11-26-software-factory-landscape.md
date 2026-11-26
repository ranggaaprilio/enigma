# Riset Landskap: AI Software Factory & Agent Orchestration Platform

**Tanggal:** 2026-11-26
**Tujuan:** Menajamkan fokus konsep Enigma — platform yang mengorkestrasi AI coding agent di dalam sandbox cloud, dengan alur *plan → code → self-test (preview) → review human*.

> Catatan metode: Riset ini memprioritaskan sumber primer (dokumen resmi vendor, halaman produk, source repo). Mesin pencari tidak tersedia di sesi ini, sehingga penemuan dilakukan lewat fetch langsung ke sumber primer. Beberapa klaim tentang produk yang tidak sempat dicek langsung ditandai `[belum terverifikasi dari sumber primer]`.

---

## 1. Landskap: Pemain & Penekanan Masing-masing

| Produk | Perusahaan | Fokus utama |
|---|---|---|
| **Devin** | Cognition | "Autonomous software engineer" — bekerja asinkron di cloud, menyelesaikan sampai PR siap merge |
| **Factory (Droid)** | Factory.ai | Menyebut dirinya langsung **"foundation for your software factory"**; fokus enterprise: Control, Governance, Value |
| **OpenHands (Agent Canvas)** | All Hands AI | Self-hosted "developer control center" yang menjalankan banyak agent (Claude Code, Codex, Gemini, Pi, OpenCode) lintas backend lokal/docker/VM/cloud |
| **Claude Code** | Anthropic | Agentic coding tool lintas surface (terminal, IDE, desktop, web); menekankan integrasi & alur review PR |
| **Jules** | Google | Autonomous coding agent yang bekerja asinkron di VM cloud Google |
| **Perwakilan lain** | GitHub Copilot Workspace/Agent, Replit Agent, Cursor, Windsurf, Aider, Sourcegraph Amp | Beragam; umumnya fokus pada XP developer individual |

**Pola dominan:** industri muru menuju *loop yang diprodukkan*: **Plan → Build → Review → Ship**. Perbedaannya bukan pada apakah agent bisa menulis kode (semua bisa), melainkan pada tiga hal: (a) **verifikasi/self-testing** — agent menguji kodenya sendiri di runtime terisolasi, (b) **governance enterprise** — trust, audit, kontrol, dan (c) **model ekonomi** — routing model untuk biaya.

---

## 2. Loop Inti & Human-in-the-Loop

**Devin (Cognition)** menampilkan alur eksplisit 3-tahap:

1. **Understand and plan** — "Map the codebase and systems architecture accurately"; memperbarui diagram/code map seiring perubahan codebase.
2. **Code and test** — iterasi di "Devin Cloud", Desktop, atau CLI.
3. **Review** — "Devin Review finds bugs and vulnerabilities before they reach production"; bisa ditugasi memperbaiki dalam satu klik.

Human-in-the-loop diwujudkan lewat: user men-tag `@Devin` di Slack/Teams, Devin "joins the thread", dan hasil akhir berupa **merged PR** yang melewati review. Pengguna memegang keputusan merge — bukan agent.

**Factory (Droid)** membingkai tiga pilar yang justru sangat relevan dengan dilema fokus Enigma:

- **01 Control** — *Sovereign, Model-Independent Intelligence*: kontrol di mana sistem berjalan, bagaimana ia belajar, dan model apa yang dipakai.
- **02 Governance** — *Trusted, Governed Self-Improvement*: sistem yang terus mengubah sinyal enterprise menjadi kode produksi yang tervalidasi.
- **03 Value** — *Measurable Business Value*: menghubungkan adopsi engineering dengan profitabilitas/risiko.

Kata kuncinya: **"Plan, build, review, and ship software with autonomous agents"** di desktop, terminal, web, dan CLI. Ini mengonfirmasi bahwa *review gate* adalah bagian dari produk, bukan tambahan.

---

## 3. Self-Testing & Verifikasi (Pertanyaan inti kamu)

Ini titik di mana Enigma punya benang merah terkuat dengan industri.

**Devin** membuat klaim yang hampir persis dengan visi kamu:
> "Tests end to end on its own computer — Devin spins up its own Windows, Mac, and Linux virtual machines as needed to build, test in its browser, and record itself clicking through what it built."

Artinya:
- **Agent menguji kodenya di runtime terisolasi** (VM per sesi) — bukan sekadar menulis file.
- Ada elemen **pengamatan visual/browser** — agent merekam dirinya "mengklik" apa yang ia buat (sangat mirip dengan konsep *preview* Enigma tempat agent bisa memeriksa hasilnya).
- Model ini memungkinkan **loop iterasi tertutup**: agent melihat output test, memperbaiki, mengulang.

**OpenHands** menawarkan gradasi isolasi yang berguna sebagai referensi arsitektur:
- **Tanpa sandbox** — agent punya akses penuh ke filesystem (diberi warning keras).
- **Satu sandbox Docker** — volume terpisah, workspace di-mount.
- **Banyak sandbox Docker** — *"Run each new conversation in its own Docker container"* → isolasi per sesi (paralel/concurrent aman).

Ini menegaskan pola industri: **verifikasi memerlukan runtime yang terisolasi dan dapat di-bootstrap**, dan semakin baik agent bisa "melihat" hasilnya, semakin kuat loop-nya.

---

## 4. Sandboxing & Keamanan

- **Devin:** "Every session runs in its own VM". + *Private networking available*. Pengaturan "fine-grained".
- **OpenHands:** Mode Docker memakai container terpisah per percakapan; tidak dijalankan dengan `--privileged`; pengguna diperingatkan keras tentang mode "tanpa sandbox" (full filesystem access).
- **Konsensus industri:** isolasi per-sesi (VM/container) adalah standar de facto; tidak ada yang menjalankan agent dengan akses tak terbatas ke host untuk beban produksi.
- **Egress/network control** menjadi fitur keamanan kelas enterprise.

---

## 5. Governance, Audit & Model Ekonomi

- **Devin (enterprise):** "Inputs and outputs are always your IP", "Zero data retention enforced with model providers", "Comprehensive audit logs", **RBAC / SSO / SCIM / OIDC**.
- **Devin (ekonomi):** model routing — "mixes and matches models" untuk "up to 60% less" biaya sambil tetap mendapat performa frontier.
- **Factory:** "Model Routing" dan "Analytics" adalah fitur produk tersendiri; menghubungkan adopsi dengan *nilai bisnis terukur*.

Pola: **audit log + RBAC + kontrol data + routing model** adalah komponen yang membuat platform ini bisa dijual ke perusahaan, bukan sekadar alat developer.

---

## 6. Celah & Diferensiasi Potensial

Berdasarkan perbandingan di atas, area yang *belum* terdiferensiasi dengan kuat di industri:

1. **Verifikasi "perilaku" vs. "kompilasi".** Banyak yang berhenti di test pass/lint. Sedikit yang benar-benar membuat agent *mengamati perilaku aplikasi di runtime* (browser/preview) lalu memperbaiki. Ini justru kekuatan yang sudah Enigma bangun (preview proxy + sandbox runtime).
2. **Spec-driven / artefak kerja sebagai first-class.** Factory dan Devin menyebut "plan" tapi jarang memprodukkan *spesifikasi berstruktur* sebagai artefak yang bisa direview manusia sebelum implementasi. Pipeline Spec Kit Enigma (constitution → implement dengan gate) adalah diferensiasi nyata.
3. **Provider-neutral runtime.** Mayoritas vendor mengikat ke stack/cloud-nya sendiri. Enigma kini punya `SandboxRuntime` abstrak (Daytona + Docker lokal) — ini posisi yang jarang.

---

## Implikasi untuk Enigma (Bahan Diskusi)

Kalau kita merujuk ke arah industri, fokus konsep Enigma sebaiknya adalah:

- **Jadikan "Plan → Build → Test → Review → Ship" sebagai jalur utama produk**, bukan sekadar kumpulan fitur. Spec Kit pipeline + git worktrees + PR sudah meng-cover `Plan → Review → Ship`; yang perlu ditekankan adalah **self-test loop** (`Test`) — agent yang benar-benar menjalankan & mengamati aplikasinya di preview, lalu iterasi.
- **Posisikan self-testing + preview sebagai diferensiasi inti** (bukan fitur tambahan) — ini yang "Devin" lakukan dengan VM/browser dan sulit ditiru oleh tool yang hanya jalan di terminal.
- **Jual governance** (audit, RBAC, kontrol biaya, model routing) karena itulah yang membuat platform enterprise laku — tapi jangan biarkan ini menenggelamkan inti *software factory*.
- **Hindari menjanjikan "IDE penuh".** Editor/terminal adalah *kendaraan*, bukan janji. Fokus pada *loop agent + verifikasi + manusia di loop*.
- Pertahankan **provider-neutral** (Daytona + Docker) sebagai titik beda arsitektural.

---

## Sumber

- Cognition / Devin — https://devin.ai/ (halaman produk resmi; alur "Understand/plan → Code and test → Review", VM per sesi, model routing, audit/RBAC).
- Factory.ai / Droid — https://factory.com/ (halaman produk resmi; "foundation for your software factory", pilar Control/Governance/Value, Plan/Build/Review/Ship).
- OpenHands / Agent Canvas — https://github.com/OpenHands/OpenHands (source repo resmi; arsitektur multi-backend, mode sandbox Docker per-sesi, warning tanpa-sandbox).
- Anthropic Claude Code — https://code.claude.com/docs/en/overview (dok resmi; surface & integrasi, review PR, cloud sessions).
- Google Jules — https://jules.google/ (halaman produk; autonomous coding agent asinkron).
