# vphone-legacy

Pengembangan emulator **iPhone6,2 / n53ap / iOS 7.1.2 / 11D257** pada Mac Apple Silicon. Firmware asli sudah diekstrak dan divalidasi, secure monitor asli menjalankan XNU, dan debugger membuktikan callback mount HFS System kembali sukses. **iOS belum mencapai launchd/SpringBoard dan belum usable.**

Manager hanya menampilkan status dan log sebenarnya. SpringBoard/aplikasi tiruan, respons terminal buatan, shim pencetak log, dan timer sukses telah dikeluarkan dari jalur produksi. Jendela QEMU menampilkan framebuffer guest. iOS 8–9 belum didukung.

| Gerbang | Status |
| --- | --- |
| BuildManifest, dekripsi kernel/DeviceTree/RootFS, checksum, sektor HFSX | Lulus untuk IPSW lokal target |
| Monitor EL3, XNU, MMU, timer dan thread kernel | Teramati lewat debugger/trace |
| Firmware ANS asli | Diekstrak dan handoff RAM disiapkan; eksekusi diagnostik berhenti pada MMIO koprosesor yang belum tersedia |
| Serial XNU asli | Output guest teramati; boot userspace belum terbukti |
| Storage dan callback mount HFS System | Teramati lewat model ASP dan debugger; root directory/volume Data masih belum terbukti |
| Process1 dan launchd | Belum lulus |
| Compositor, SpringBoard, touch/Home, keyboard iOS | Belum lulus |
| Tiga cold boot, persistensi Notes, sesi 15 menit | Belum dapat diuji |

Lihat [handover](ANTIGRAVITY_HANDOVER.md), [bukti mount dan wait bootstrap](research/bootstrap-launchd-20261013.md), [audit storage](research/continuation-20261013-storage.md), [catatan bring-up](research/bringup.md), dan [kontrak backend](backend/README.md).

## Build

Python, Xcode/Command Line Tools, GLib, Pixman, OpenSSL, dan `qemu-img` dibutuhkan. Dependency Python berada di `.venv`; dependency build QEMU berada di `backend/.venv`.

```sh
make setup
.venv/bin/python scripts/qemu/build_backend.py --fetch --install-build-deps
make test
```

QEMU dipin ke v11.1.2, commit `4fc49f46dc95d4a27de2509e7fceb2931e91faeb`. Builder menghasilkan `backend/bin/qemu-system-aarch64`; launcher memverifikasi binary, sumber mesin, dan commit pin. `qemu-img` global hanya dipakai untuk konversi image. `make backend` memakai checkout/dependency yang sudah tersedia.

Builder menerima `--sdk /path/to/MacOSX.sdk` bila linker tidak cocok dengan SDK default. Pada host ini build berhasil dengan SDK macOS 15.5 dalam Xcode 2.app; lihat backend README.

## Firmware dan VM

IPSW lokal wajib diberikan. Pipeline memilih identitas erase tepat dari BuildManifest, menyimpan komponen asli, dan gagal bila identitas, dekripsi, struktur atau checksum salah. Patch kernel berbasis tebakan string ditolak.

```sh
# Default: ~/.vphone-legacy/VMs. Opsional penyimpanan workspace:
export VPHONE_LEGACY_VMS_DIR="$PWD/build"
./vphone-legacy vm create ios712 --ios 7.1.2 \
  --ipsw ./iPhone6,2_7.1.2_11D257_Restore.ipsw
./vphone-legacy vm doctor ios712
./vphone-legacy vm launch ios712 --headless --probe-seconds 0.05
./vphone-legacy vm status ios712
```

Bundle tervalidasi yang tersedia pada workspace ini bernama `ios712-validated`. `vm doctor` memeriksa artefak/backend; exit code nol dari doctor atau probe bukan sertifikasi boot iOS. Bundle lama ditolak dan tidak ditimpa; pilih nama baru untuk membangun ulang.

Loader memuat Mach-O/BSS kernel dan monitor asli yang mengikuti stream complzss. CPU mulai di entry monitor EL3; monitor menyerahkan eksekusi kepada XNU. Ini tidak mengeksekusi iBoot. Partisi SEP 6 MiB dan monitor 2 MiB mengikuti kontrak iBoot yang diperiksa; kernel memakai physical base `0x800800000`, virtual base `0xffffff8000200000`. PRAM/STRAM dan region ANS mengikuti kontrak firmware; framebuffer diagnostik dialokasikan terpisah. Clock/fuse dan NVRAM awal memakai profil emulator yang dicatat. CPU tetap prototipe Cortex-A57 dengan sebagian register Apple, bukan Cyclone lengkap. Mesin memakai TCG, satu CPU aplikasi dan RAM fisik 1 GiB pada `0x800000000`.

ANS ARM32 asli diekstrak dari iBoot dan dimuat ke region 8 MiB setelah fixup yang diaudit. Eksekusi ANS tersedia sebagai probe riset opt-in dengan CPU Cortex-A7 sementara. Storage produksi memakai model software ASP terpisah; eksekusi diagnostik koprosesor belum melewati seluruh MMIO yang dibutuhkan. Lihat [kontrak ANS](research/native-ans-firmware.md).

RootFS UDIF sudah dikonversi menjadi sektor disk dan partisi HFSX asli. Model ASP eksperimental memakai path image eksplisit dan memeriksa error I/O/DMA. `system.raw` tetap utuh; `system-runtime.raw` adalah salinan persisten untuk journal/tulisan guest, dan `data.raw` tetap persisten. Namespace utility logis disimpan dalam `utility.raw` dengan header, region dan provenance tervalidasi; ini bukan metadata NAND fisik asli. Tulisan journal System dan NVRAM guest benar-benar bertahan pada file backing. Callback mount HFS kembali dengan kode nol di guest, tetapi root directory, pemetaan Data ke `/private/var`, seed data dan first boot belum terbukti. Setiap sesi menyimpan salinan ketiga disk sebelum boot beserta checksum.

## Display dan lifecycle

```sh
./vphone-legacy vm launch ios712-validated       # QEMU Cocoa
./vphone-legacy vm launch ios712-validated --paused
./vphone-legacy vm stop ios712-validated --force
./vphone-legacy gui
```

Scanout RAM guest memakai 640×1136 BGRX32, stride 2560. Smoke test membuktikan pixel yang ditulis guest muncul dalam capture QMP. Ini belum membuktikan AppleH6PDisplayPipe/AGX atau SpringBoard. Touch, Home/Power dan keyboard belum tersedia.

Launcher memiliki satu proses QEMU dan satu koneksi QMP; stop diarahkan ke launcher pemilik. QMP terpisah dari serial. Exit code/crash diteruskan dan proses direap. Shutdown guest/flush filesystem belum tersedia; stop normal ditolak dan `--force` mencatat shutdown tidak bersih. `make clean` mempertahankan bundle/data VM.

`vm_manifest.json` menyimpan identitas/checksum; `runtime.json` terpisah dengan tahap process/kernel/userspace/display/agent. Kata Darwin atau SpringBoard dalam log tidak menjadi bukti boot. Probe menyimpan command, image boot turunan, hash sumber/binary, register awal/akhir, serial, trace MMIO/exception, status QMP, framebuffer dan status akhir dalam `logs/SESSION/`. Panic XNU membuat launcher keluar gagal meskipun QEMU ditutup dengan exit code nol.

## Verifikasi

```sh
make test
.venv/bin/python backend/smoke_test.py
VPHONE_TEST_BUNDLE="$PWD/build/ios712-validated" \
  .venv/bin/python -m unittest discover -s tests -p test_backend_lifecycle.py -v
```

Smoke test menggunakan fixture program kecil untuk UART/framebuffer. Lifecycle test memakai QEMU sebenarnya untuk kepemilikan tunggal, penolakan launch ganda dan force stop. QMP memerlukan izin local Unix socket. Dekripsi firmware asli bersifat opt-in; lihat `tests/test_firmware_gate.py`.

Kriteria selesai tetap: tiga cold boot mencapai SpringBoard asli, Settings/Calculator/Notes berfungsi melalui input guest, data bertahan setelah shutdown, layar membeku ketika CPU dihentikan, dan sesi 15 menit tanpa crash/restart. Semua kriteria ini masih terbuka.
