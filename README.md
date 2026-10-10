<p align="center"><img src="assets/icon.png" width="128" alt="BYSTerm"></p>

# BYSTerm — Seri / TCP / UDP hızlı test ve izleme aracı

Seri port, TCP, UDP, seri port dinleme, sanal port ve ağ araçları tek uygulamada.
**Windows, macOS, Linux (Ubuntu, NVIDIA Jetson dahil)** üzerinde aynı kodla çalışır.
SSH/telnet yok, gereksiz menü yok: aç, portu seç, bak.

| Sekme | Ne yapar |
|---|---|
| **Seri** | Aktif portları **isim/açıklama/VID:PID** ile listeler (takıp çıkarınca otomatik yenilenir), standart baud listesi (elle de yazılır), 5–8 bit, parity, stop, akış kontrolü, DTR/RTS, BREAK, CTS/DSR/DCD/RI göstergeleri |
| **Seri İzleme** | Sadece portu seç: portu **başka bir uygulama** kullanırken iki yöndeki trafiği gör (sanal port yok) — aşağıya bakın |
| **Sanal Port** | Gerçek port ↔ sanal port köprüsü, birbirine bağlı sanal port çifti, iki portla donanım tap |
| **TCP İstemci** | host:port'a bağlan, gönder/al |
| **TCP Sunucu** | Dinle, bağlı istemcileri listele, **hepsine veya seçilene** gönder, istemciyi at |
| **UDP** | Yerel porttan dinle, hedefe gönder, gelen paketin kaynağını göster, "son gönderene yanıtla", broadcast |
| **Ağ Ayarları** | PC'deki tüm ağ kartlarını listele (Ethernet 1/2/3, Wi-Fi: bağlantı, hız, IP/maske, gateway, DNS, DHCP/statik, MAC). Seçtiğin kartın **IP / maske / gateway / DNS**'ini değiştir veya DHCP'ye al. **Ek IP ekle**: mevcut IP (ve internet) bozulmadan karta cihazın alt ağından ikinci bir IP. **Otomatik IP**: cihazın IP'sini yaz, o ağda boş IP bulunsun. **Profiller**: "Jetson ağı" gibi ayarları kaydet, tek tıkla yükle |
| **Ping** | Sürekli ping, aynı anda birden fazla hedef: kayıp %, son/ort/min/max/jitter, canlı grafik. Cihaz **cevap vermeye başlayınca / kesilince** satır vurgulanır (isteğe bağlı bip) |
| **IP Tarama** | Alt ağı tara: cevap veren cihazların IP, süre, MAC, host adı ve TTL'den tahmini sistemi. Sağ tık → Ping'e ekle / iPerf / TCP hedefi yap |
| **iPerf** | **iperf3 uyumlu** hız testi, istemci ve sunucu: TCP/UDP, ters yön (-R), paralel akış (-P), hız sınırı (-b), kaynak kart seçimi. Cihazdaki gerçek `iperf3 -s` / `iperf3 -c` ile ya da iki BYSTerm arasında çalışır. Canlı Mbit/s grafiği, UDP'de jitter ve kayıp |

Her sekmede: **ASCII / HEX / HEX+ASCII** görünüm, zaman damgası, renkli RX/TX, duraklat,
temizle, ekranı kaydet, **kayıt** (`.log` = zaman damgalı metin, `.bin` = ham RX baytları),
ASCII (`\r \n \t \xHH` kaçışlı) veya HEX gönderme, satır sonu (CR / LF / CR+LF),
gönderim geçmişi (↑/↓), **periyodik tekrar** (ms), dosya gönder. Aynı türden istediğiniz
kadar bölme açabilirsiniz (soldaki listede araç adının yanındaki **+**). Son ayarlar hatırlanır.

**Pencereler:** soldaki araç adına tıklamak seçili bölmenin içeriğini o araca
çevirir; **+** yanına yeni bölme açar. Bölmeleri başlığından tutup sürükleyerek başka bir bölmenin
sağına/soluna/üstüne/altına bırakabilir, aradaki çizgiyle büyütüp küçültebilirsiniz.

**Dil:** Ayarlar → *Language / Dil* → Türkçe veya English. Seçilen dilde menüler, yardım metinleri,
günlük satırları ve tablo başlıkları dahil her şey o dilde görünür (gönderilen/alınan veri asla
değiştirilmez). Koyu / açık tema da Ayarlar menüsündedir.

## İndir ve çalıştır (kurulum yok)

[**Releases**](../../releases/latest) sayfasından sisteminize uygun dosyayı indirin.
Python, pip ya da başka bir kütüphane **gerekmez**; her şey dosyanın içinde.

| Sistem | Dosya | Nasıl açılır |
|---|---|---|
| **Windows** 10 / 11 — kurulum (önerilen) | `BYSTerm-Setup-windows-x64.exe` | Çift tıklayıp kurun. Başlat menüsüne eklenir; Seri İzleme için USBPcap sürücüsünü de isteğe bağlı kurar. İlk açılışta "Windows bilgisayarınızı korudu" çıkarsa *Ek bilgi → Yine de çalıştır* (imzasız). |
| **Windows** 10 / 11 — taşınabilir | `BYSTerm-windows-x64.exe` | Kurulumsuz tek dosya. Çift tıklayın. |
| **macOS** Apple Silicon (M1–M4) | `BYSTerm-macos-arm64.zip` | Zip'i açın, `BYSTerm.app`'i Uygulamalar'a sürükleyin. İlk açılışta *sağ tık → Aç* (imzasız). Olmazsa Terminal'de: `xattr -dr com.apple.quarantine /Applications/BYSTerm.app` |
| **macOS** Intel | `BYSTerm-macos-x64.zip` | Aynı şekilde |
| **Linux PC** (Ubuntu 18.04 → 24.04+, Debian, Mint…) | `BYSTerm-linux-x64.tar.gz` | `tar xzf BYSTerm-linux-x64.tar.gz && cd BYSTerm && ./BYSTerm`. Menüye eklemek için: `./install.sh` |
| **NVIDIA Jetson**: JetPack 4.x / 5.x / 6.x (Nano, TX2, Xavier, Orin), Raspberry Pi OS 64-bit | `BYSTerm-linux-arm64.tar.gz` | Aynı şekilde |

Linux'ta seri porta erişim için kullanıcınızın `dialout` grubunda olması gerekir (bir kez):
`sudo usermod -aG dialout $USER` → oturumu kapatıp açın. `install.sh` bunu kontrol edip hatırlatır.

Linux paketleri bilerek **Ubuntu 18.04 üzerinde** derlenir. Bu sayede eski ve yeni bütün
dağıtımlarda ve JetPack sürümlerinde aynı dosya çalışır. Her sürüm yayınlanmadan önce
otomatik olarak Ubuntu 18.04 / 20.04 / 22.04 / 24.04 üzerinde, Windows'ta ve macOS'ta
açılıp kendi kendini test eder (`--selftest`).

**Çalıştığını doğrulamak için:** `BYSTerm --selftest`. Pencere açılır, kendi içinde
TCP sunucu↔istemci, ağ kartı listeleme, ping ve iperf3 testi yapar ve `SELFTEST OK` yazıp kapanır.

## Yönetici izni (IP değiştirme, seri port izni)

IP değiştirmek her işletim sisteminde yönetici izni ister. BYSTerm bunu **açılışta bir kez** ister,
sonra sormadan çalışır:

* **Windows:** açılışta UAC sorusu → onaylarsanız BYSTerm yönetici olarak yeniden başlar.
* **Linux / Jetson:** açılışta şifre penceresi (pkexec) → küçük bir yardımcı süreç root olarak
  çalışır ve **yalnızca** ağ komutlarını (`nmcli`, `ip`, `dhclient`…) ve seri port iznini çalıştırır.
  Bir seri port "erişim reddedildi" derse izin anında verilir (kullanıcınız `dialout` grubuna da eklenir)
  ve port yeniden açılır.
* **macOS:** açılışta yönetici şifresi, aynı yardımcı.

Reddederseniz uygulama normal çalışır; IP değiştirirken tekrar sorulur. Açılıştaki soruyu
**Ayarlar → Açılışta yönetici izni iste** ile kapatabilirsiniz. Her ağ değişikliğinden önce
çalıştırılacak komutlar size gösterilir ve onay istenir.

> Uyarı: Uzaktan bağlandığınız kartın IP'sini değiştirirseniz bağlantı kopar. Cihaza ulaşmak için
> çoğu zaman **"Ek IP olarak ekle"** daha güvenlidir.

## Yüksek veri hızında donmaz

Terminal uygulamaları yüksek hızda genellikle gelen her parçada ekranı yeniden çizdikleri için
kilitlenir. BYSTerm'da:

* Okuma/yazma ayrı thread'lerde yapılır; arayüz hiçbir zaman port/soket üzerinde beklemez.
* Ekran 40 ms'de bir **toplu** güncellenir ve tik başına bir bayt bütçesi vardır.
  Fazlası ekranda atlanır (`... 12.3 MB ekranda gösterilmedi` uyarısı çıkar), ama
  **sayaçlar ve kayıt dosyası her baytı görür**.
* Ekran 20 000 satırla sınırlıdır (eski satırlar düşer, bellek şişmez).

Ölçüm (tek uygulamada aynı anda TCP sunucu sekmesine sınırsız TCP akışı + seri sekmesine
3 Mbaud pty akışı, 5 sn):

| Görünüm | TCP alınan | Kayıp | Arayüzün en uzun takılması |
|---|---|---|---|
| ASCII | ~650 MB/s | 0 bayt | 22 ms |
| HEX | ~450 MB/s | 0 bayt | 35 ms |
| HEX+ASCII | ~700 MB/s | 0 bayt | 17 ms |

Gerçek bir UART 3 Mbaud'da bile ~0.3 MB/s'dir, yani bu sınırın çok altında kalır.

## Seri İzleme (portu dinlemek)

Sadece **portu seçip** başlatırsın. Portu BAŞKA bir uygulama kullanırken (kendi programın, terminal,
ROS düğümü…) iki yöndeki trafiği olduğu gibi görürsün. O uygulama hiç değişmez, BYSTerm porta
dokunmaz, sanal port yoktur (tamamen pasif). `CIHAZ>` = cihazdan gelen, `UYGUL>` = uygulamanın gönderdiği.

- **Windows:** USB-seri çeviriciler için (FTDI, CP210x, CH340/CH341, PL2303 ve Arduino / STM32 /
  ESP32 / Pico gibi USB CDC cihazlar). Gelen/giden verinin yanında uygulamanın seçtiği
  **baud / format** (ör. `115200 baud 8N1`), **DTR/RTS** değişiklikleri ve hat hataları (çerçeve,
  parite, BREAK) da görünür. Bunun için bir kez ücretsiz **USBPcap** sürücüsü kurulur (kurulum
  programında tek tik ya da Seri İzleme'deki düğme; sonra Windows bir kez yeniden başlatılır).
  USBPcap, Microsoft imzalı bir USB yakalama sürücüsüdür (GPL-2.0, kaynak:
  [github.com/desowin/usbpcap](https://github.com/desowin/usbpcap)). Anakart üzerindeki yerleşik
  COM portları USB olmadığı için onlarda **Sanal Port → Köprü** kullanılır.
- **Linux / Jetson:** Portu hangi uygulama açarsa onun trafiği gösterilir; uygulama portu sonradan
  açsa, kapatıp yeniden açsa ya da başka bir uygulama açsa da dinleme kendiliğinden sürer. Sürücü
  gerekmez; çekirdeğin `ptrace` yetkisi kullanılır, gerekirse yönetici izni istenir
  (`sudo apt install strace`).
- **macOS:** port dinleme yok; **Sanal Port → Köprü** kullanılır.

> Nasıl çalışır (Windows): USBPcap, USB sürücü yığınına takılan bir filtre sürücüsüdür ve USB-seri
> çeviriciye giden/gelen her paketi kopyalar; BYSTerm bunlardan seri veriyi ve port ayarlarını çözer.

## Sanal Port

**Sanal Port** aracındaki "Yöntem" listesinden seçilir:

**1) Köprü: gerçek port ↔ sanal port** — BYSTerm gerçek portu açar ve bir sanal port oluşturur;
uygulamanda gerçek port yerine bu sanal portu açarsın. İki yöndeki trafik iletilir ve görünür.
Linux/macOS'ta sanal port otomatik (`/tmp/ttyV0`); uygulamanın seçtiği baud/format gerçek porta
uygulanır. **Windows'ta** bir sanal port çifti gerekir (**com0com**, aynı ekrandaki düğmeyle kurulur):
uygulamada çiftin bir ucunu, BYSTerm'de diğer ucunu açarsın.

**2) Sanal port çifti** — birbirine bağlı iki sanal port (A ↔ B). Bir uygulamada A'yı, diğerinde
B'yi açarsın; birinin yazdığı diğerine gider. Linux/macOS'ta trafik BYSTerm'de görünür
(`A>` / `B>`), iki tarafın seçtiği baud/format da yazılır. Windows'ta çift com0com ile oluşturulur
("Yeni sanal port çifti oluştur") ve kalıcıdır.

**3) Donanım tap: iki gerçek portu dinle** — iki USB-seri çeviricinin RX uçlarını hattın TX ve RX'ine
bağlarsın; BYSTerm ikisini de sadece dinler (`A>` / `B>`), hatta hiçbir şey göndermez.

## Kaynaktan çalıştırma / derleme (geliştirici için)

```bash
pip install pyserial PyQt5          # veya PySide6
python3 src/bysterm.py
python3 src/test_bysterm_core.py    # çekirdek testleri
python3 src/test_bysterm_usbsniff.py # USB seri izleme (USBPcap) testleri
```

Tek dosya paket: `pip install pyinstaller` → `python3 ci/build.py` → `release/`.
Linux için tüm sistemlerde çalışan paket: `docker run --rm -v "$PWD":/src -w /src ubuntu:18.04 bash ci/build_linux_docker.sh`

**Yeni sürüm yayınlamak:** `src/bysterm.py` içindeki `APP_VERSION`ı artırın, sonra
GitHub → **Actions** → *Build & Release* → **Run workflow** → sürümü yazın (ör. `0.3.1`). (Veya `git tag v0.3.1 && git push origin v0.3.1`.) GitHub Actions bütün platformları derler,
test eder ve Releases'a koyar.

| Dosya | İçerik |
|---|---|
| `src/bysterm.py` | Arayüz (Qt: PySide6 → PyQt5 → PySide2 sırasıyla denenir) |
| `src/bysterm_core.py` | Qt'siz çekirdek: transport'lar, port tarama, biçimleyici, köprü |
| `src/bysterm_usbsniff.py` | Windows canlı seri izleme: USBPcap akışından FTDI/CP210x/CH340/PL2303/CDC seri veri ve ayar çözümü |
| `src/bysterm_net.py` | Qt'siz ağ katmanı: arayüz okuma/yazma, ping, IP tarama, iperf3 protokolü, yönetici yardımcısı |
| `src/bysterm_i18n.py`, `src/bysterm_i18n_data.py` | Dil desteği. Çeviriler `ci/i18n_map.json`'da; düzenledikten sonra `python3 ci/gen_i18n.py` |
| `src/test_bysterm_core.py` | Çekirdek testleri |
| `ci/` | Paketleme (PyInstaller), Linux Docker derlemesi, selftest betikleri |
| `.github/workflows/release.yml` | Otomatik derleme + Release |
