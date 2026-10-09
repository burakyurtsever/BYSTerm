"""
BYSTerm dil destegi — Qt'den bagimsiz.

Kaynak metinler INGILIZCE yazilir; Turkce karsiliklari TR sozlugundedir.
    from bysterm_i18n import tr
    tr('Connect')                          -> 'Bağlan' (dil tr ise)
    tr('{n} ports found').format(n=3)      -> bicimli metinler .format ile

Dil uygulama acilisinda set_lang() ile secilir (Ayarlar > Language / Dil; yeniden baslatinca gecerli).
"""

LANG = 'en'
LANGS = (('en', 'English'), ('tr', 'Türkçe'))


def set_lang(code):
    global LANG
    LANG = code if code in dict(LANGS) else 'en'


try:
    from bysterm_i18n_data import FULL, FRAGS     # uretilmis sozluk: {kaynak: (en, tr)}
except ImportError:      # pragma: no cover
    FULL, FRAGS = {}, []


def tx(s):
    """Arayuz metnini secili dile cevir. Tam eslesme, yoksa bicimli metin parcalari (f-string)."""
    if not isinstance(s, str) or not s:
        return s
    i = 1 if LANG == 'tr' else 0
    hit = FULL.get(s)
    if hit is not None:
        return hit[i]
    if LANG == 'tr' and s in TR:          # Ingilizce kaynakli eski tr() metinleri
        return TR[s]
    for src, en, trk in FRAGS:            # uzundan kisaya
        if src in s:
            s = s.replace(src, trk if i else en)
    return s


def tx_exact(s):
    """Sadece birebir bilinen metni cevir (kullanici verisi olabilecek yerler icin; parca degistirme yok)."""
    if not isinstance(s, str) or not s:
        return s
    hit = FULL.get(s)
    if hit is not None:
        return hit[1 if LANG == 'tr' else 0]
    if LANG == 'tr' and s in TR:
        return TR[s]
    return s


def tr(s):
    return tx(s)


TR = {
    # --- genel / ana pencere
    'Settings': 'Ayarlar',
    'About': 'Hakkında',
    'Language': 'Dil',
    'Theme': 'Tema',
    'Dark': 'Koyu',
    'Light': 'Açık',
    'Restart BYSTerm to apply the new language.': 'Yeni dil, BYSTerm yeniden başlatılınca geçerli olur.',
    'Restart now?': 'Şimdi yeniden başlatılsın mı?',
    'Show startup animation': 'Açılış animasyonunu göster',
    'Check for updates at startup': 'Açılışta güncellemeleri kontrol et',
    'Check for updates now': 'Güncellemeleri şimdi kontrol et',
    'Ask for admin rights at startup (IP changes, serial port access)':
        'Açılışta yönetici izni iste (IP değiştirme, seri port izni)',
    'Request admin rights now': 'Yönetici iznini şimdi iste',
    'Reset window layout': 'Pencere düzenini sıfırla',
    'Serial · TCP · UDP · Network toolkit': 'Seri · TCP · UDP · Ağ araç kutusu',

    # --- hakkinda
    'Developer': 'Geliştirici',
    'Source code & releases': 'Kaynak kod ve sürümler',
    'Close': 'Kapat',
    ('BYSTerm is a fast test and monitoring tool for embedded and network developers. '
     'It brings serial ports, TCP, UDP, serial traffic monitoring, network settings, ping, '
     'IP scanning and iperf3 speed tests together in one window with side-by-side panes. '
     'It stays responsive at high data rates and runs on Windows, macOS, Linux and NVIDIA Jetson '
     'without any installation.'): (
        'BYSTerm, gömülü sistem ve ağ geliştiricileri için hızlı bir test ve izleme aracıdır. '
        'Seri port, TCP, UDP, seri trafik izleme, ağ ayarları, ping, IP tarama ve iperf3 hız testini '
        'tek pencerede, yan yana bölmelerde toplar. Yüksek veri hızında donmaz; Windows, macOS, Linux '
        've NVIDIA Jetson üzerinde kurulum gerektirmeden çalışır.'),

    # --- guncelleme
    'Update available': 'Güncelleme var',
    'A new version of BYSTerm is available: {new}  (you have {cur})':
        'BYSTerm\'in yeni sürümü çıktı: {new}  (sizdeki {cur})',
    "What's new:": 'Yenilikler:',
    'Update now': 'Şimdi güncelle',
    'Later': 'Sonra',
    'Skip this version': 'Bu sürümü atla',
    'Downloading {name}...': '{name} indiriliyor...',
    'Downloading update': 'Güncelleme indiriliyor',
    'Cancel': 'İptal',
    'Update failed': 'Güncelleme başarısız',
    'The update was installed. BYSTerm will now restart.': 'Güncelleme kuruldu. BYSTerm yeniden başlatılıyor.',
    'You are using the latest version ({cur}).': 'En güncel sürümü kullanıyorsunuz ({cur}).',
    'Could not check for updates: {err}': 'Güncelleme kontrol edilemedi: {err}',
    'No download for this system was found in the release. Opening the release page.':
        'Bu sürümde sisteminize uygun dosya bulunamadı. Sürüm sayfası açılıyor.',
    'BYSTerm is running from source; opening the release page.':
        'BYSTerm kaynak koddan çalışıyor; sürüm sayfası açılıyor.',
    'No write permission for {path}. Download the new version manually from the release page.':
        '{path} için yazma izni yok. Yeni sürümü sürüm sayfasından elle indirin.',
}
