# iOS 7.1.2 App Registration & Sideloading Mechanism (Phase 0 Spike Report)

Dokumen ini mendokumentasikan hasil penelitian baca-saja (Phase 0) mengenai mekanisme pendaftaran aplikasi pada guest emulator iPhone 5s (iPhone6,2, iOS 7.1.2 build 11D257) dan evaluasi jalur injeksi offline untuk sideloading aplikasi hello-world / pihak ketiga.

---

## 1. `installd` dan `MobileInstallation.framework`

### 1.1 Lokasi File & Arsitektur
- **Daemon binary**: `/usr/libexec/installd` (Mach-O 64-bit executable `arm64`, ukuran 601.488 byte).
- **Framework**: `/System/Library/PrivateFrameworks/MobileInstallation.framework`
  - Di filesystem hanya terdapat `Info.plist` dan `_CodeSignature`.
  - Implementasi biner berada di dyld shared cache: `/System/Library/Caches/com.apple.dyld/dyld_shared_cache_arm64`.
- **LaunchDaemon plist**: `/System/Library/LaunchDaemons/com.apple.mobile.installd.plist`
  - `ProgramArguments`: `["/usr/libexec/installd", "--idleExit"]`
  - `RunAtLoad`: `true` (dijalankan langsung saat boot oleh `launchd`).
  - `UserName` / `GroupName`: `mobile` / `mobile`.
  - `MachServices`: `com.apple.mobile.installd`.
  - `LaunchEvents`: `com.apple.notifyd.matching` -> `com.apple.mobile.developer_image_mounted`.

### 1.2 Keberadaan `uicache`
- Binary `uicache` atau `lsregister` **TIDAK ADA** pada iOS 7.1.2 retail (`find /Volumes/... -name "*uicache*"` tidak menghasilkan biner apapun).
- Pada ekosistem iOS, `uicache` adalah utilitas jailbreak (UIKitTools) pihak ketiga buatan Saurik yang memanggil private API LaunchServices (`[LSApplicationWorkspace registerApplicationDictionary:]`).
- Pada iOS retail, daemon pendaftaran sistem ditangani secara kooperatif oleh `/usr/libexec/installd` dan `/usr/libexec/lsd` (LaunchServices Daemon).

### 1.3 String & Simbol Penting di `installd` dan `lsd`
Dari disassembly dan ekstraksi string pada biner guest:
- **`installd` string / logging signatures**:
  - `0x1000614f8`: `/private/var/mobile`
  - `0x10006150c`: `/Applications`
  - `0x10006151a`: `Applications`
  - `0x100061527`: `/AppleInternal/Applications`
  - `0x10006154e`: `Library/Caches`
  - `0x100061592`: `%s/com.apple.mobile.installation.plist`
  - `0x10006192a`: `preflight_install_map`
  - `0x100061be5`: `check_stale_maps`
  - `0x100061bf6`: `Install cache not found; it will be generated.`
  - `0x100061c25`: `%s appears stale`
  - `0x100061cd0`: `check_freshness` (`sb.st_mtime = %ld reference_mtime = %ld`)
  - `0x100061d42`: `wait_for_ls_registration`
  - `0x100061dc3`: `com.apple.LaunchServices.applicationRegistered`
- **`lsd` disasembly (`0x100003b60` - `0x100003c94`)**:
  - `lsd` memanggil `[LSApplicationWorkspace defaultWorkspace]`
  - Menyiapkan array `ReturnAttributes`: `Path`, `ApplicationType`, `SignerIdentity`, `EnvironmentVariables`, `ProfileValidated`, `Entitlements`.
  - Memanggil `MobileInstallationLookup(options)` ke `installd`.
  - Mengiterasi seluruh `allValues` dan memanggil `-[LSApplicationWorkspace registerApplicationDictionary:]`.

---

## 2. Format & Lokasi Cache

### 2.1 Lokasi File Cache
1. **Installation Map Cache**:
   - Path absolut di guest: `/private/var/mobile/Library/Caches/com.apple.mobile.installation.plist` (symlink dari `/var/mobile/Library/Caches/...`).
   - Format: Apple Binary Property List (`bplist00`).
2. **LaunchServices Database**:
   - Path absolut di guest: `/private/var/db/lsd/com.apple.LaunchServices-*.csstore`
   - Format: LaunchServices CSStore (database biner relasi UTI, bundle, handler).
3. **SpringBoard Icon State**:
   - Path absolut di guest: `/private/var/mobile/Library/SpringBoard/IconState.plist` (dan `DesiredIconState.plist`).
   - Format: Property List berisi `iconLists` (array grid halaman) dan `buttonBar` (dock).

### 2.2 Status File di `data.raw` Saat Ini
- Pada `build/ios712-validated/data.raw`:
  - Volume HFS+ kosong / unseeded (hanya direktori `.` dan `..`).
- Pada `build/virtual-activated-profile-v5/data.raw`:
  - Direktori dasar `/mobile/Library/Caches/`, `/mobile/Applications/`, dan `/db/lsd/` sudah ada (dibuat saat seeding template).
  - Namun file `com.apple.mobile.installation.plist`, `csstore`, dan `IconState.plist` **BELUM ADA** (ukuran 0 file / direktori kosong).
- **Alasan**: Kedua disk image adalah image disk statis sebelum/awal aktivasi. iOS secara dinamis menghasilkan file-file cache tersebut ke data volume saat proses boot pertama kali ketika daemon `installd`, `lsd`, dan `SpringBoard` mulai aktif.

### 2.3 Skema & Contoh Entri Cache `com.apple.mobile.installation.plist`
Struktur top-level dictionary terdiri dari 4 kunci utama:
- `System`: Dictionary berisi seluruh system apps (dari `/Applications`), keyed by `CFBundleIdentifier`.
- `User`: Dictionary berisi third-party / user apps (dari `/var/mobile/Applications`), keyed by `CFBundleIdentifier`.
- `Internal`: Dictionary aplikasi internal (bila `/AppleInternal/Applications` ada).
- `Metadata`: Metadata cache (`CacheGUID` UUID string, `SequenceNumber`, `OSVersion`).

#### Contoh Entri `com.apple.calculator` (System App):
```plist
<key>com.apple.calculator</key>
<dict>
    <key>ApplicationType</key>
    <string>System</string>
    <key>CFBundleExecutable</key>
    <string>Calculator</string>
    <key>CFBundleIdentifier</key>
    <string>com.apple.calculator</string>
    <key>CFBundleDisplayName</key>
    <string>Calculator</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>Path</key>
    <string>/Applications/Calculator.app</string>
    <key>MinimumOSVersion</key>
    <string>7.1</string>
    <key>UIDeviceFamily</key>
    <array>
        <integer>1</integer>
        <integer>2</integer>
    </array>
    <key>Entitlements</key>
    <dict>
        <key>com.apple.private.security.container-required</key>
        <true/>
        <key>platform-application</key>
        <true/>
    </dict>
    <key>ProfileValidated</key>
    <false/>
    <key>SignerIdentity</key>
    <string>Apple iPhone OS Application Signing</string>
</dict>
```

#### Contoh Entri `com.apple.mobilesafari` (System App):
```plist
<key>com.apple.mobilesafari</key>
<dict>
    <key>ApplicationType</key>
    <string>System</string>
    <key>CFBundleExecutable</key>
    <string>MobileSafari</string>
    <key>CFBundleIdentifier</key>
    <string>com.apple.mobilesafari</string>
    <key>CFBundleDisplayName</key>
    <string>Safari</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>7.0</string>
    <key>CFBundleVersion</key>
    <string>7.0</string>
    <key>Path</key>
    <string>/Applications/MobileSafari.app</string>
    <key>UIDeviceFamily</key>
    <array>
        <integer>1</integer>
        <integer>2</integer>
    </array>
    <key>Entitlements</key>
    <dict>
        <key>com.apple.private.security.container-required</key>
        <true/>
        <key>dynamic-codesigning</key>
        <true/>
        <key>platform-application</key>
        <true/>
    </dict>
    <key>ProfileValidated</key>
    <false/>
    <key>SignerIdentity</key>
    <string>Apple iPhone OS Application Signing</string>
</dict>
```

---

## 3. Pemicu Rebuild Cache Saat Boot

### 3.1 Analisis `check_stale_maps` di `installd` (`0x10000a67c` - `0x10000aaf0`)
1. Saat boot, launchd menjalankan `/usr/libexec/installd --idleExit` karena `RunAtLoad = true`.
2. `installd` memanggil fungsi `check_stale_maps` yang melakukan `stat()` pada `/private/var/mobile/Library/Caches/com.apple.mobile.installation.plist`.
3. **Bila cache TIDAK ADA (`errno == ENOENT`)**:
   - `installd` mencetak log: `"Install cache not found; it will be generated."`
   - Flag `system_stale`, `internal_stale`, dan `user_stale` langsung diset ke `1`.
   - `installd` memindai direktori `/Applications` dan `/var/mobile/Applications`, membaca seluruh `Info.plist`, membangun dictionary cache baru, dan menyimpannya ke disk menggunakan `[NSPropertyListSerialization dataWithPropertyList:format:options:error:]` (format bplist `0xc8`).
4. **Bila cache ADA**:
   - Fungsi `check_freshness` membandingkan `st_mtime` dari direktori `/Applications` dan `/var/mobile/Applications` terhadap `reference_mtime` file cache.
   - Jika `st_mtime > reference_mtime`, `installd` mencatat log `"%s appears stale"` dan memperbarui entri yang berubah.
   - *Catatan praktis*: Menghapus file cache secara eksplisit saat injeksi offline jauh lebih aman dan deterministik daripada mengandalkan resolusi timestamp mtime HFS+.

### 3.2 Siklus Pendaftaran ke SpringBoard
1. `lsd` membaca entri aplikasi dari `installd` melalui `MobileInstallationLookup`.
2. `lsd` meregistrasikan dictionary setiap aplikasi ke LaunchServices database (`csstore`).
3. SpringBoard memonitor perubahan LaunchServices atau membaca daftar aplikasi saat bootstrap icon model (`SBIconModel`).
4. **Perilaku SpringBoard terhadap IconState**:
   - SpringBoard memiliki method bawaan `addNewIconsToDesignatedLocations:saveIconState:`.
   - Jika aplikasi yang valid terdaftar di LaunchServices tetapi belum ada di `IconState.plist`, SpringBoard **secara otomatis menempatkan ikon aplikasi tersebut ke slot kosong pertama di SpringBoard**, kemudian memperbarui `IconState.plist`.
   - **Kesimpulan**: SpringBoard **TIDAK memerlukan IconState yang dipaksa**; injeksi offline tidak perlu menulis atau memodifikasi `IconState.plist`.

---

## 4. Persyaratan Bundle Aplikasi

### 4.1 `Info.plist` Minimal
`installd` memvalidasi kunci-kunci berikut pada `Info.plist`:
- `CFBundleExecutable` (string): Nama file binary (misal `Hello`). Harus cocok persis dengan file binary di dalam bundle.
- `CFBundleIdentifier` (string): Bundle ID unik (misal `com.vphone.hello`).
- `CFBundlePackageType` (string): `"APPL"`.
- `CFBundleVersion` (string): **Wajib ada sebagai string**. `installd` memiliki assertion:
  `"Apps must have a CFBundleVersion declared as a string in their Info.plist, but %@ does not. Failing installation."`
- `CFBundleShortVersionString` (string): Versi tampilan (misal `"1.0"`).
- `UIDeviceFamily` (array of integers): `[1]` untuk iPhone/iPod touch, `[1, 2]` untuk Universal.
- `MinimumOSVersion` (string): `"7.0"` (atau `"7.1"`).
- `UIRequiredDeviceCapabilities` (array of strings): `["arm64"]`.

### 4.2 Mach-O Binary & Architecture
- Arsitektur: Thin `arm64` (Cyclone, ARMv8-A).
- Load Command Versi OS:
  - Harus memuat `LC_VERSION_MIN_IPHONEOS` dengan versi 7.0 atau 7.1.
  - Mach-O modern yang menggunakan `LC_BUILD_VERSION` (iOS 12+) tidak didukung oleh kernel dan dyld iOS 7.1.2.
- Framework linking: Hanya boleh menautkan framework yang ada di iOS 7.1.2 shared cache (`UIKit`, `Foundation`, `CoreGraphics`, `QuartzCore`). Tidak boleh ada dependensi ke dylib eksternal yang tidak tersedia di rootfs.

### 4.3 Izin & Kepemilikan Katalog HFS+ (UID/GID)
- **Untuk System App (`/Applications/<App>.app`)**:
  - UID: `0` (`root`), GID: `80` (`admin`) atau `0` (`wheel`).
  - Mode: `0755` (`rwxr-xr-x`) untuk folder dan biner executable; `0644` (`rw-r--r--`) untuk `Info.plist` dan resource.
  - Kepemilikan diatur pada katalog raw HFS+ via `scripts/qemu/hfs_ownership.py` (`set_catalog_owner`).
- **Untuk User App (`/var/mobile/Applications/<UUID>`)**:
  - UID: `501` (`mobile`), GID: `20` (`staff`/`mobile`).
  - Mode: `0755` untuk direktori container dan biner executable; `0644` untuk file data.
  - Membutuhkan struktur container:
    - `<UUID>/<App>.app`
    - `<UUID>/Documents/`
    - `<UUID>/Library/` (dan subdirektori `Preferences`, `Caches`)
    - `<UUID>/tmp/`

### 4.4 Sandbox & Entitlements
- User apps di `/var/mobile/Applications` secara ketat dibatasi oleh sandbox seatbelt profile `container`.
- System apps di `/Applications` tidak di-jail ke dalam direktori container acak UUID dan memiliki toleransi sandbox lebih tinggi.
- Entitlements minimal untuk hello-world di `/Applications`:
  ```xml
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0">
  <dict>
      <key>platform-application</key>
      <true/>
  </dict>
  </plist>
  ```

---

## 5. Code Signing & Kebijakan AMFI

### 5.1 Status pada `build/ios712-validated`
- Pada bundle tervalidasi `build/ios712-validated`:
  - `vm_manifest.json` baris 10: `"boot_args": "-v serial=3"`.
  - `patch_report.json`: `"patches": []`, kernel retail utuh.
  - **Dampak**: AMFI berjalan dengan kebijakan produksi retail penuh. Setiap biner yang bertanda tangan ad-hoc (`CS_ADHOC`), tanda tangan tidak sah, atau tanpa tanda tangan **AKAN DIBUNUH** oleh kernel (`SIGKILL`, code signing crash `CS_KILL`) saat eksekusi.
  - Biner pihak ketiga tidak dapat berjalan pada profil ini tanpa sertifikat valid Apple atau pelonggaran AMFI.

### 5.2 Status pada Profil Diagnostik / `virtual-activated-profile-v5`
- Pada profil diagnostik (`scripts/prepare_virtual_activation.py:27` dan `virtual-activated-profile-v5/profile.json:15`):
  - `boot_args`: `-v serial=3 debug=0x14e cpus=2 csi_disable_watchdog=1 amfi_allow_any_signature=1 amfi_get_out_of_my_way=1 cs_enforcement_disable=1`.
  - Tiga argumen AMFI ini:
    1. `amfi_allow_any_signature=1`: Mengizinkan biner dengan tanda tangan ad-hoc (misal dihasilkan `ldid -S` atau `sign_diagnostic_probe.py`).
    2. `amfi_get_out_of_my_way=1`: Mengabaikan kegagalan validasi trust chain AMFI.
    3. `cs_enforcement_disable=1`: Mematikan enforcement signature check di level kernel XNU.
- **Kesimpulan**: Untuk menjalankan aplikasi sideload di guest tanpa memodifikasi biner kernel, VM instance yang bersangkutan **harus menyertakan ketiga boot-args AMFI tersebut** pada manifest VM-nya.

---

## 6. Rekomendasi Jalur Injeksi (Trade-Off & Action Plan)

### Evaluasi Tiga Opsi:

| Aspek | Jalur A: `/Applications/` (System Volume) + Hapus Cache | Jalur B: `/var/mobile/Applications/` (Data Volume) + Tulis Cache | Jalur C: Helper Native Sekali-Jalan (LaunchDaemon) |
|---|---|---|---|
| **Lokasi Injeksi** | `system.raw` (atau `system-runtime.raw`) | `data.raw` | `system.raw` (LaunchDaemon + Job Cache) |
| **Kompleksitas Host** | **Rendah**: Salin `.app`, ubah CNID owner root:admin, hapus cache plist. | **Tinggi**: Generate UUID, buat 5 folder container, set ratusan CNID 501:20, parse & reconstruct binary plist cache. | **Sangat Tinggi**: Patching job cache xpcd, compile biner guest, interaksi Mach IPC/XPC. |
| **Ketergantungan Container** | Tidak butuh container UUID data volume. | Wajib container UUID + xattrs / metadata breadcrumb. | Bergantung pada API yang dipanggil. |
| **Resiko Kegagalan** | Rendah: Mekanisme resmi pendaftaran system app iOS 7. | Tinggi: Jika cache atau EA container corrupt, `installd` me-wipe data atau menolak container. | Tinggi: Crash pada boot bila shared cache offset / payload Mach-O tidak cocok. |
| **Kesesuaian Target Hello-World** | **Sangat Sesuai** (pola jailbreak Cydia/MobileTerminal). | Terlalu dini untuk Fase 1. | Overkill untuk Fase 1. |

### Rekomendasi: JALUR A
Jalur yang direkomendasikan untuk Fase 1 & 2 adalah **Jalur A**:

#### Langkah-Langkah Persis:
1. **Host Extraction & Validation**:
   - Host mengekstrak payload `.app` dari IPA atau folder build.
   - Memeriksa Mach-O slice (`arm64`, min OS 7.0) dan memvalidasi `Info.plist`.
   - Menandatangani ad-hoc dengan `ldid -S` (atau `sign_diagnostic_probe.py`).
2. **Mount System Disk (Salinan Runtime / Klon Ephemeral)**:
   - Ambil runtime lock (`runtime.lock`).
   - Mount disk system secara read-write (`hdiutil attach ...`).
   - Salin bundle aplikasi ke `/Applications/<AppName>.app` (menggunakan `ditto --noextattr --norsrc` untuk mencegah polusi atribut host macOS).
   - Pastikan izin direktori dan file `0755` / `0644`.
   - Detach disk system.
3. **Perbaiki Ownership HFS+**:
   - Jalankan `set_catalog_owner` pada file/direktori `.app` baru di disk image yang sudah di-unmount agar pemiliknya menjadi `0:80` (`root:admin`).
4. **Hapus Cache Instalasi**:
   - Mount disk data read-write.
   - Hapus file `/mobile/Library/Caches/com.apple.mobile.installation.plist` (bila ada).
   - Hapus file di `/db/lsd/*.csstore` (bila ada).
   - Detach disk data.
5. **Boot Konfigurasi**:
   - Pastikan boot-args menyertakan `amfi_allow_any_signature=1 amfi_get_out_of_my_way=1 cs_enforcement_disable=1`.
6. **Eksekusi Boot Guest**:
   - Saat boot, `installd` mendeteksi cache hilang -> scan `/Applications/<AppName>.app` -> generate cache baru.
   - `lsd` meregistrasi ke LaunchServices.
   - `SpringBoard` otomatis mendeteksi app baru dan memunculkan ikonnya di halaman Home screen.

#### Yang Harus Diverifikasi pada Boot Nyata (Fase Selanjutnya):
- Log `installd` di guest (`/var/mobile/Library/Logs/MobileInstallation/mobile_installation.log`): verifikasi pesan `"Install cache not found; it will be generated"` dan pencatatan bundle ID.
- File cache `/var/mobile/Library/Caches/com.apple.mobile.installation.plist`: verifikasi keberadaan entri aplikasi baru di bawah kunci `System`.
- SpringBoard UI: verifikasi kemunculan ikon dan respons tap peluncuran aplikasi.
- Runtime log output: verifikasi penulisan log pembuktian oleh aplikasi hello-world saat launch.
