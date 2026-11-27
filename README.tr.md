# Dilmaç

[English](README.md) · **Türkçe**

*Dilmaç* eski Türkçede "tercüman" demek. PS5'te oynarken İngilizce altyazıları Türkçeye çevirip Philips Google TV'de oyunun üstünde gösterir.

```
PS5 ──HDMI──> TV (sen burada oynarsın)
 │
 └─Remote Play─> PC: pencereyi okur (Windows OCR) → bilgisayarda (OPUS-MT), Ollama ya da Gemini ile çevirir ──Wi-Fi──> TV uygulaması (üst katman)
```

## Kolay yol: Dilmac-Setup.exe
**Dilmac-Setup-<tarih>.exe**'yi çalıştır (yönetici izni istemez). Program `%LOCALAPPDATA%\Programs\Dilmac`'a, ayarları ve
paketleri `%LOCALAPPDATA%\Dilmac`'a kurulur; Başlat menüsüne kısayol konur, güncelleme için yenisini üstüne kurman yeter
(ayarlar kalır). Exe imzalı olmadığı için Windows "Bilgisayarınız korundu" diyebilir: **Ek bilgi → Yine de çalıştır**.
Kurulum dosyası `py pc\build_exe.py --setup` ile üretilir (Inno Setup 6 gerekir).

Geliştirme bilgisayarında ana klasördeki **Dilmac.exe** de doğrudan çalışır; o zaman ayarlarını yanındaki `pc\` klasöründe tutar.
- **Kurulum (ilk açılışta):** adım adım ilerler. Bilgisayar (yazı tanıma paketi, ağ profili, çeviri motoru), PS5 (PSN girişi ve 8 haneli kodla eşleme ya da Remote Play penceresi), TV (ağdaki TV'leri bulur, markasını tanır; Android TV, LG ve Samsung için ne yapılacağını canlı ✓ işaretleriyle gösterir ve uygulamayı kurar), oyun ve altyazı alanı, TV'de deneme yazısı. Sonradan ana ekrandaki "Kurulum" düğmesiyle yeniden açılır.
- **Ana ekran:** "Çeviriyi başlat" düğmesi; PS5, TV, çeviri ve oyun kartları; eksik bir şey varsa "Yapılacaklar" listesi ve düzeltme düğmeleri; sağda canlı altyazı (Türkçesi, İngilizcesi, kaynağı) ve günlük.
- **Ayarlar:** oyun, çeviri motoru, Gemini anahtarı, TV'deki kutu (yazı boyu, opaklık, geçiş, eski satır), görev yazıları, konuşan rengi, sesten tanıma, görüntü kaynağı, altyazı alanı.

Pencere Windows'un Edge WebView2 bileşeniyle çizilir (Windows 11'de ve güncel Windows 10'da hazır gelir). Yoksa eski görünümlü arayüz açılır.

İlk açılış birkaç saniye sürer. Windows Güvenlik Duvarı izin sorarsa **Özel ağlar** için izin ver; PS5'i bulmak için gerekir.

Kodu değiştirdikten sonra exe'yi yeniden üretmek için: `py pc\build_exe.py` (gerekenler: `py -m pip install pyinstaller sv-ttk pywebview`).

## Bir kerelik kurulum (elle)

### 1. PC
1. **İngilizce yazı tanıma paketi** (yönetici PowerShell'de):
   ```powershell
   Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"
   ```
2. **Python paketleri**:
   ```powershell
   py -m pip install -r pc\requirements.txt
   ```
3. **PS Remote Play**: Sony'nin sitesinden Windows sürümünü kur ve PS5'e bağlan. İkinci (ücretsiz) bir PSN hesabı önerilir.
4. **Çeviri motoru**: varsayılan `opus`: OPUS-MT modeli bilgisayarın işlemcisinde çalışır, anahtar ve ekran kartı gerekmez (satır başına ~50-100 ms; ~475 MB'lık model bir kez iner, arayüzde "Modeli indir"). Argoda daha iyi çeviri için `local` (Ollama'daki yapay zekâ modeli, ekran kartıyla ~0,4 sn) ya da `gemini` (https://aistudio.google.com/apikey adresinden ücretsiz anahtar). Paketteki satırlar her zaman önce gelir; motor geç kalırsa Google Çeviri yedek olarak devreye girer.
5. `pc\config.example.json` dosyasını `pc\config.json` olarak kopyala. `tv_ip`, `game` ve gerekiyorsa `gemini_api_key` alanlarını doldur. TV IP'si, TV uygulamasının ekranında yazar.

### 2. TV (Philips 65PUS8808)
1. **Ayarlar → Sistem → Hakkında → Android TV OS derlemesi** satırına 7 kez tıkla. Geliştirici seçenekleri açılır.
2. **Ayarlar → Sistem → Geliştirici seçenekleri → USB hata ayıklama**'yı aç. Menüde **Kablosuz hata ayıklama** varsa onu da aç.
3. PC'de:
   ```powershell
   .\tv\install.ps1 -TvIp <TV_IP>
   ```
   TV ekranında "Hata ayıklamaya izin verilsin mi?" sorusu çıkınca **İzin ver**'i seç ("Her zaman" kutusunu işaretle).

### LG TV (webOS 4 ve üstü, 2018 sonrası; gerçek TV'de henüz denenmedi)
LG, uygulamaların HDMI görüntüsünün üstüne çizmesine izin vermiyor. Bu yüzden LG'de TV uygulaması önde açık kalır, PS5'in HDMI girişini kendi içinde gösterir ve altyazıları onun üstüne çizer. Bağlantı ters yönde kurulur: TV, PC'nin 8788 portuna bağlanır.
1. https://webostv.developer.lge.com adresinde ücretsiz bir LG hesabı aç.
2. TV'de LG Content Store'dan **Developer Mode** uygulamasını kur, bu hesapla giriş yap ve **Dev Mode Status**'u aç. TV yeniden başlar.
3. PC'deki uygulamada Ayarlar'da TV türünü **LG (webOS)** yap ve TV'yi "Ağda bul" ile bul.
4. Developer Mode uygulamasında **Key Server**'ı aç. Soldaki TV satırında **Kur**'a bas ve TV'nin sol altında yazan 6 karakterli Passphrase'i gir.

Sonrasında uygulama çeviri başlayınca kendiliğinden açılır, yeni sürüm çıkınca kendiliğinden güncellenir ve 1000 saatlik geliştirici modu süresi günde bir kez yenilenir. Windows güvenlik duvarı sorarsa izin ver. PS5 TV'yi kendi girişine geçirip uygulamayı arkaya atıyorsa TV ayarlarından SIMPLINK'i (HDMI-CEC) kapat. TV kumandasında ◀ ▶ HDMI girişini değiştirir, OK bağlantı bilgisini gösterir.

### Samsung TV (Tizen 3.0 ve üstü, 2017 sonrası; gerçek TV'de henüz denenmedi)
LG'deki gibi çalışır: uygulama PS5'in HDMI girişini kendi içinde gösterir. Samsung yalnızca o TV için imzalanmış paketi kurar; imzayı bu bilgisayar senin ücretsiz Samsung hesabınla alır.
1. TV'de Uygulamalar ekranını aç, kumandada **1 2 3 4 5**'e bas, **Developer mode**'u aç ve **Host PC IP**'ye bu bilgisayarın adresini yaz (PC'deki Samsung penceresi gösterir). TV'yi tam yeniden başlat: güç tuşunu Samsung logosu çıkana kadar basılı tut.
2. PC'deki uygulamada Ayarlar'da TV türünü **Samsung (Tizen)** yap ve TV'yi "Ağda bul" ile bul.
3. TV satırında **Kur**'a bas. İlk seferde tarayıcıda Samsung giriş sayfası açılır. Giriş yapınca PC, TV'nin kimliğine (DUID) özel sertifikaları alır (`pc\samsung\` klasöründe durur, git'e girmez), paketi imzalar, TV'ye kurar ve açar.

PC'nin adresi pakete yazılır. Adres değişirse TV uygulaması PC'yi ağda arar, bir sonraki çeviri başlangıcında da paket yeni adresle yeniden kurulur (giriş gerekmez). TV uygulamayı bırakıp sürekli PS5'in girişine geçiyorsa Anynet+'yı (HDMI-CEC) ve otomatik kaynak değiştirmeyi kapat.

## Doğrudan PS5 bağlantısı (Remote Play uygulaması olmadan)
Uygulama PS5'e kendisi bağlanabilir. Bu durumda PS Remote Play uygulaması, açık kalması gereken pencere ve "pencere görünür kalsın" derdi ortadan kalkar. Görüntü ekrana çizilmeden doğrudan yazı tanımaya gider. Ses alınmaz, sen zaten TV'den oynuyorsun. PS5 dinlenme modundaysa uyandırılır.

Bir kerelik eşleme: Bağlantılar'da PS5 satırındaki **Eşle** düğmesine bas.
1. **PSN girişi:** **PSN ile giriş yap**'a bas. Sony'nin kendi giriş sayfası küçük bir pencerede açılır (adres çubuğu olmayan Edge penceresi, her seferinde boş bir geçici profille). İkinci PSN hesabınla giriş yap; şifren yalnız Sony'ye gider. Giriş bitince uygulama Sony'nin yönlendirme adresini kendisi yakalar ve pencereyi kapatır. Edge ya da Chrome yoksa tarayıcı açılır ve adres eskisi gibi yapıştırılır.
2. **PS5'in kodu:** PS5'te **Ayarlar → Sistem → Uzaktan Oynatma → Cihazı Bağla**'yı aç ve ekrandaki 8 haneli kodu yazıp **Eşle**'ye bas. Resmi Remote Play uygulaması açıksa önce kapat, çünkü PS5 aynı anda tek bağlantı kabul eder.

Eşleme bitince Ayarlar'daki **Görüntü kaynağı** kendiliğinden "Doğrudan PS5" olur. Eşleme bilgisi `pc\ps5-pairing.json` dosyasında durur. İstediğin zaman "Remote Play penceresi"ne geri dönebilirsin.

**Bağlantı kütüphanesi**
- Bağlantı chiaki-ng'nin kütüphanesiyle (libchiaki) yapılır; CouchSubs da bunu kullanır.
- Uygulama kütüphaneyi `pc\chiaki_bridge` içindeki küçük bir C köprüsüyle (`chiaki_bridge.dll`) çağırır. Bu DLL exe'nin içindedir.
- **Yeniden derlemek gerekirse:** MSYS2 UCRT64 kabuğunda şunu çalıştır:
  ```
  bash pc/chiaki_bridge/build.sh <chiaki-ng kaynağı> <derleme klasörü> pc/chiaki_bridge/bin
  ```
  Gereken paketler `build.sh`'nin başında yazar. Sonra exe'yi `py pc\build_exe.py` ile yeniden üret.
- **Lisans:** chiaki-ng AGPL-3.0 lisanslıdır. Kendi kullanımın için sorun yok; uygulamayı başkasıyla paylaşırsan kaynak kodunu da paylaşman gerekir.
- **IP değişirse:** PS5 eski IP adresinde bulunamazsa uygulama onu ağda arar ve yeni adresine bağlanır.

## Her oynayışta
1. TV'de **Dilmaç** uygulamasını bir kez aç (TV yeniden başlayınca kendisi de açılır), sonra PS5'in HDMI girişine geç.
2. PC'de Remote Play'i aç ve PS5'e bağlan. Pencere **görünür kalsın**: simge durumuna küçültme ve üstünü başka pencereyle kapatma. PC'nin sesini kapatabilirsin.
3. Çevirmeni başlat:
   ```powershell
   py pc\subtitler.py
   ```

## Nasıl çalışır
- **Okuma** (`ocr.py`): altyazı bölgesi önce olduğu gibi okunur. Sonuç temiz İngilizce değilse bir kez de yalnız "beyaz yazı + sağ-altında koyu gölge" pikselleri bırakılarak okunur ve sözlükçe daha düzgün olan seçilir. Parlak zeminde (gökyüzü, beyaz gömlek) işe yarar. PS5'in Türkçe menü ve bildirimleri çevrilmez.
- **Satır ayırma** (`ocr.py`, oyunun sözlüğündeki `screen` bölümü): GTA VI'da konuşma altyazısı her zaman ekranın tam ortasındadır. Ortası ekranın ortasında olmayan yazılar atılır: telefon mesajları, tabelalar, semt adları, düğme ipuçları. En alttaki ortalı satır görev yazısıdır ("Go to the docks"). Konuşmaya karışmaz, ayrı çevrilir ve kendi yerinde ayrı bir kutuda gösterilir.
- **Üst üste binen altyazılar** (`ocr.split_into_blocks`, `subtitler.process_blocks`): GTA VI aynı anda birden çok altyazıyı alt alta dizer. Örneğin araçla giderken bağıran yayalar, polis anonsu ya da hemen ardından gelen replik. Bunları ayırmanın yolu satır aralığıdır:
  - Tek bir uzun altyazının kaymış satırları sık dizilir; üst kenarlar arası görüntünün yaklaşık 0,044'ü.
  - Ayrı altyazılar arasında boşluk daha geniştir, yaklaşık 0,053.
  - Eşik sözlükte `"block_step": 0.049` olarak durur.
  - Ölçüm karede ±0,003 oynar; ayrı iki altyazı bazen eşiğin hemen altında ölçülüp tek altyazı sanılır. Bu durumda parçalardan biri ekranda zaten izlenen bir altyazıysa satırlar oradan ayrılır (`split_by_known`). Böylece aynı replik iki kutuda iki kez çevrilmez.
  - Her altyazı ayrı izlenir, ayrı çevrilir ve TV'de kendi İngilizcesinin üstünde ayrı bir kutuda gösterilir. Oyun yazıları yukarı kaydırınca TV'deki kutular da kayar.
  - Ayarlar'daki "Üst üste altyazı" seçeneğiyle kapatılabilir; o zaman hepsi tek kutuda birleşir.
- **Konuşan rengi:** oyun altyazıda kimin konuştuğunu göstermez. Kayıtlı karelerde ölçüldü: Lucia'nın, Jason'ın, polisin ve yayaların yazısı aynı beyaz. Bu yüzden bilgi paketten gelir.
  - Paketi çevirirken sahneden anlaşılan konuşan, paketteki `"speakers"` alanına yazılır: Lucia, Jason, pedestrian (yaya), police (polis) ya da karakterin adı.
  - TV'de yazı o renkte çıkar. Renkler sözlükte `"colors"`: Lucia pembe, Jason mavi, diğer karakterler sarı, yaya ve polis gri.
  - Pakette olmayan ya da konuşanı belli olmayan satır beyaz kalır. Kısa ve herkesin söyleyebileceği satırlar ("Okay.", "Let's go!") bilerek etiketlenmez.
  - Bu yüzden renk ilk oynayışta, yani pakete henüz girmemiş yeni repliklerde çıkmaz. Paket güncellenince o satırlar etiketlenir; sonraki oynayışta (bölümü tekrar oynayınca, başka kayıtta) renkli görünür.
  - Ayarlar'daki "Konuşan rengi" seçeneğiyle kapatılır. Renk için TV uygulaması 0.5 ya da üstü gerekir; Bağlantılar'da TV satırındaki **Kur** ile kurulur.
- **Sesten tanıma (`audio.py`):** doğrudan PS5 bağlantısında oyunun sesi de alınır (chiaki'nin Opus çözücüsü, 48 kHz stereo).
  - Her yeni replik için, ilk görülmeden 0,5 sn önce başlayıp 2,5 sn sonra biten ses parçası `pc\voices\clips\` altına mono 16 kHz WAV olarak kaydedilir (~96 KB). Liste: `pc\voices\clips.jsonl` (satır, söyleyeni paketten biliniyorsa o da).
  - Tanıma: replik ilk görüldükten 1,6 sn sonra o sesin vektörü (NVIDIA TitaNet-small, sherpa-onnx ile, ~25 ms) karakterlerin ses profilleriyle karşılaştırılır. Yeterince benziyorsa (eşik 0,55 ve ikinci adaydan 0,08 ileride) kutunun rengi o karaktere döner. Yeni replik önce beyaz çıkar, sesi tanınınca renklenir.
  - Profiller (`pc\voices\profiles.json`): söyleyeni belli repliklerin seslerinden çıkar. Oynarken paketten söyleyeni bilinen replikler profile kendiliğinden katılır. Paket güncellenip konuşanlar etiketlendikten sonra `py pc\voice_tool.py profile` bütün kayıtlı parçalardan profilleri baştan çıkarır; `py pc\voice_tool.py validate` doğruluğu ölçer. "pedestrian" ve "police" profile katılmaz (bir sürü farklı ses).
  - Model `pc\voices\model\nemo_en_titanet_small.onnx` (40 MB, k2-fsa/sherpa-onnx GitHub sürümü) exe'nin içinde değil; yoksa tanıma sessizce kapalı kalır.
  - Ayarlar'daki "Sesten tanıma" ile kapatılır. Ses akışı PS5'in TV'deki sesini etkilerse kapat.
- **TV'deki kutu** (`tv/`): Türkçe yazı, İngilizce altyazının okunduğu yerin tam üstüne oturur. Siyah kutu İngilizceyi örtecek kadar geniş ve yüksektir; yazı boyu İngilizce satırın boyundan hesaplanır. Uzun çeviri ekranın %80'ine kadar genişler, böylece daha az satıra sığar. Oyun ard arda iki altyazıyı üst üste gösterdiğinde yalnız yenisi çevrilir. Önceki çeviri birkaç saniye üstte soluk kalır, sonra kalkar.
  - Geçişler (TV uygulaması 0.4 ve üstü): kutu birden çıkıp kaybolmaz, yumuşakça belirip söner. Kutu ekrandayken yazı değişirse kutu yerinde kalır, yalnız yeni yazı belirir. Oyun altyazıyı kaydırınca kutu da kayarak gider. Süre Ayarlar'da "Geçiş (ms)"; varsayılan 180, 0 = eskisi gibi anında.
  - Kutu varsayılan olarak %90 opaktır ("Kutu opaklığı").
  - TV uygulaması güncellenince altyazı servisi kendiliğinden yeniden başlar; uygulamayı TV'de açmak gerekmez.
- **Dengeleyici** (`stabilizer.py`): temiz ve noktalamayla biten satır ilk karede çevrilir. Bozuk okunan ara haller (yazı belirirken ya da solarken) çevrilmez. Aynı satırın farklı okunuşları yeniden çevrilmez. Konuşma uzarsa yalnız yeni cümle çevrilir. Daha doğru bir okuma gelirse çeviri bir kez düzeltilir. Yazı bölgeden 0,8 sn kalkınca silinir; OCR yazıyı kaçırmış ama yazı hâlâ yerindeyse 4 sn'ye kadar beklenir.
- **Çeviri** (`translation.py`): altyazı cümlelere bölünür, her cümle `pc\translation-cache.json` dosyasında ayrı saklanır. Konuşan adı ("JASON:") çevrilmez. Modele oyun adı, son 3 satır ve sözlükten yalnız o satırda geçen adlar ve terimler gider.
- **Oyun sözlüğü** (`pc\glossary\*.json`): `game` alanındaki oyunun sözlüğü kullanılır (şimdilik `gta6.json`). İçindekiler: çevrilmeyecek adlar (`keep`), hitap ve terim karşılıkları (`address_terms`, `terms`) ve modele hiç gitmeyen hazır satırlar (`lines`, ör. WASTED → ÖLDÜN). Elle düzenlenebilir.
- **Oyun paketi** (`pc\packs\*.json`, `pack.py`): oynanmış satırların sahne bağlamıyla yapılmış iyi çevirileri (`gta6.json`) ve elle yazılmış GTA tarzı üslup örnekleri (`gta6-uslup.json`). Canlı çeviriden önce pakete bakılır. OCR'ın bir iki harfini yanlış okuduğu satır da bulunur. Pakette olmayan satır çevrilirken paketten en benzer üç satır modele örnek olarak gider. Böylece küçük yerel model oyunun ağzını öğrenir ("scripts" → "reçete", "trailer" → "karavan").

## Oyun paketi: oynadıkça büyür
1. Oyna. Çevrilen her satır kendiliğinden `pc\lines\<gün>.jsonl` dosyasına yazılır.
2. Claude Code'a "paketi güncelle" de. Claude şunları yapar:
   - `py pc\pack_tool.py pending` ile pakette olmayan satırları sahne sahne listeler. OCR farkıyla okunmuş kopyalar birleşir, çöp okumalar atlanır.
   - Bu satırları sahne bağlamıyla çevirip `py pc\pack_tool.py add CEVIRI.json` ile `pc\packs\gta6.json` dosyasına ekler.
3. Uygulamayı yeniden başlat. Görev tekrarı, yaya lafları ve görev yazıları artık paketten gelir. Yeni satırlar da paketteki örneklerden üslup öğrenir.

Paket dosyası düz JSON'dur (`"English": "Türkçe"`). Beğenmediğin çeviriyi elle düzeltebilirsin. Ayrıntılı akış (havuzdaki satırlar dahil): `PACKS.md`.

İngilizce altyazılar oyunun yayıncısına ait olduğu için İngilizce metinli paket yayınlanmaz. Elle yapılan paketler ayrı, özel bir depoda durur (`pc\packs`'a klonlanır). Sunucu ve exe her uygulamaya yalnız **karmalı** kopyayı verir: Türkçe satırlar İngilizcenin karmasıyla, bir iki harfi yanlış okunan satırlar da harf üçlülerinden çıkan bir parmak iziyle (MinHash) bulunur.

## Ayar ve sorun giderme
- `py pc\subtitler.py --calibrate`: `calibration.png` (kırmızı kutu = okunan bölge), `calibration-region.png` ve `calibration-white.png` (iki hazırlık) dosyalarını kaydeder ve bölgeyi okur. Kutu altyazıyı tam kapsamıyorsa `config.json` içindeki `region` değerlerini değiştir.
- `py pc\subtitler.py --image kare.png`: PS5'ten aldığın bir ekran görüntüsünü okur. Oyun açık olmadan OCR'ı denemek için kullanılır.
- `py pc\subtitler.py --translate "Dude, where's Lucia?"`: tek bir satırı çevirir. Motoru ve sözlüğü denemek için kullanılır.
- `py pc\subtitler.py --tv-test "Merhaba"`: TV'ye deneme yazısı gönderir.
- `"save_frames": true`: çevrilen her satırın bölge görüntüsü `pc\frames\` klasörüne kaydedilir (`readings.jsonl`). Çevirileri ve süreleri de `translations.jsonl`'a yazılır: çeviri kaç ms sürdü, yazı ilk görüldükten kaç ms sonra TV'ye gitti. Bu kayıtlar OCR ve gecikme ayarı için kullanılır.
- Günlük her zaman `pc\log.txt` dosyasına da yazılır (2 MB'ı geçince `log.old.txt` olur).
- Satır ayırma eşikleri: `"center_tol"` (konuşmanın ortası ekranın ortasından en çok ne kadar sapabilir, ör. 0.02) ve `"objective_y"` (bunun altındaki satır görev yazısıdır, ör. 0.925). `null` bırakılırsa oyunun sözlüğündeki değerler kullanılır. `font_size` ve `bottom_margin` yalnız yazının yeri bilinmediğinde (eski TV uygulaması, deneme yazısı) kullanılır.
- Yazı çok mu büyük ya da küçük? Ayarlar'da "Yazı boyutu (punto)" değerini değiştir, yanındaki **TV'de dene** ile TV'de hemen gör (32 = GTA VI altyazısı kadar; görev yazısı bunun %70'i). Boy sabittir, satırdan satıra değişmez. İngilizce kutunun altından fazla seçiliyorsa "Kutu opaklığı"nı yükselt (100 = hiç görünmez). TV uygulaması eskiyse (yazı eski yerinde, yarı saydam çıkıyorsa) Bağlantılar'dan TV satırındaki **Kur** ile güncelle.
- Testler: `py -m unittest discover -s pc\tests`

## Gizlilik
Gemini seçiliyse ya da yedek olarak Google Çeviri devreye girerse yalnızca okunan altyazı metni gönderilir. Bilgisayardaki modelde (OPUS-MT ya da Ollama) metin bilgisayardan çıkmaz.

Paket sunucusuna (Dilmaç'ın Cloudflare sunucusu) iki şey gider; ikisi de ayarlardan kapatılabilir:
- **Pakette olmayan satırlar** (`share_lines`): yalnız İngilizce metin ve oyunun adı. Herkes için paket bu satırlardan büyür.
- **Ses özetleri** (`share_voices`): konuşanı paketten bilinen satırlarda, sesin 192 sayılık özeti. Ses kaydı değildir, geri dinlenemez; karakterlerin ortak ses profilleri bundan çıkar.

Sunucu IP adresi, hesap, ad ya da kurulum kimliği saklamaz (aynı ağdan gelenler yalnızca geri çözülemeyen bir özetle sayılır). Görüntü, ses kaydı, PSN bilgisi veya kullanım istatistiği hiçbir yere gönderilmez. PC ile TV arasındaki trafik yalnızca ev ağında kalır.
