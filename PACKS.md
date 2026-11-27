# "Paketleri güncelle" (Claude oturumuna talimat)

Kullanıcı "paketleri güncelle" / "paketi güncelle" dediğinde ya da zamanlanmış görev bunu istediğinde bu dosyayı izle.
**Kullanıcıyla Türkçe konuş.** Bu akış Lenovo Legion'da (paketleri güncelleyen sunucu sistemi) çalışır.
Uygulamayı kullanan Windows bilgisayarın tek işi oynamak: oynarken pakette olmayan satırları havuza, her ses
parçasının ses vektörünü (ses kaydı değil) sunucuya gönderir; Legion ikisini de Cloudflare'den alır.

## Neden

Lenovo 10 dakikada bir havuzdaki yeni satırları Gemma ile çevirip `pool-<oyun>` paketi olarak yayınlıyor (hızlı ama
küçük modelin kalitesinde). Bu akış o satırları ve oyun kayıtlarındaki yeni satırları sahne bağlamıyla doğru çevirip
elle yapılan pakete (`pc/packs/gta6.json`) ekler. Aynı satır iki pakette varsa elle yapılan kazanır, yani herkes bu
çeviriyi görür.

## Adımlar

1. **Güncel ol:** iki depoda da `git pull`: üst klasör (kod) ve `pc\packs` (elle yapılan paketlerin özel deposu,
   `UmutYuksel/Dilmac-packs`; yoksa oraya klonla). İki makine aynı anda düzenlerse çakışır: `pc\packs`'ta
   commit'lenmemiş değişiklik varsa dur ve kullanıcıya sor.
2. **Bekleyenler:** `py pc\pack_tool.py pending --pool` → `pc\lines\pending.json` (konsolda sahne sahne). Havuz
   satırlarında Gemma'nın taslağı `[Gemma: ...]` olarak görünür; doğruysa kullan, değilse düzelt.
3. **Çevir:** dosyayı scratchpad'e yaz, biçim:
   `{"lines": {EN: TR}, "speakers": {EN: kim}, "drop": [EN, ...]}`
   - Doğal sokak Türkçesi; argoyu yumuşatma, karakterin tonunu koru. Önceki/sonraki satırlara bak.
   - Konuşanı yalnız eminsen etiketle; 3 sözcükten kısa ve genel satırlara etiket koyma. Bir kez görünen kişi
     `pedestrian`, polis `police`. Etiketlerken `py pc\voice_tool.py list GÜN` (perde, en yakın profil) güvenilir
     kaynak.
   - Bozuk OCR okuması (ör. "Jell me", "Nozno!"): düzgün hali zaten listede ya da pakette varsa `drop`'a koy; yoksa
     düzeltilmiş İngilizceyle anahtarla. İki repliğin birleştiği okuma, şarkı sözü, deneme satırı: `drop`.
     `drop` satırları paketin `skipped` listesine girer ve bir daha önerilmez.
   - `add` var olan çeviriyi ezer: pakette zaten olan anahtarları dosyadan çıkar. Bulanık eşleşme kısa bir satırı
     yanlış satıra bağlayabilir; öyle bir satırı ayrı anahtar olarak etiketsiz ekle.
   - Kontrol: dosyadaki satırlarla `pending.json`'daki her satır ya bulunuyor ya da `drop`'ta olmalı.
4. **Ekle:** `py pc\pack_tool.py add DOSYA`, sonra yeniden `pending --pool` → 0 bekleyen olmalı.
5. **Yayınla:** `py pc\pack_tool.py publish gta6` ve `py pc\pack_tool.py prune-pool DOSYA` (havuz paketinden elle
   pakete giren ve `drop` edilen satırları çıkarır).
6. **Ses profilleri (Legion'da):** `py pc\voice_tool.py profile`, `validate`, sonra `py pc\voice_tool.py publish`
   (profilleri sunucuya koyar). Legion'da ses modeli olmadığı için `voice_tool` parçaları sunucudaki ses
   vektörlerinden okur (`pc\voices\clip-vectors.jsonl`'a yalnız yenileri iner); sherpa-onnx yüklenmez.
   `validate`'in doğruluğu ve kapsaması bir önceki çalıştırmadan belirgin düştüyse yayınlamadan önce kullanıcıya
   sor.
7. **Git:** `pc\packs` deposunda `gta6.json`'ı commit'le (`Pack: N new lines (date)`) ve push et. Paketler ana
   (herkese açık) depoya asla girmez; uygulamalar sunucudan yalnız karmalı kopyayı alır. Windows bilgisayarda exe'yi
   yeniden derlemek gerekmez; paketler sunucudan iner.
8. **Özet:** kaç satır eklendi, kaçı atıldı, paket sürümü.

## Zamanlanmış görev (Lenovo)

Claude masaüstü uygulamasının zamanlanmış görevleriyle her akşam çalıştırılabilir (uygulama o saatte açık olmalı;
Claude aboneliğinin kullanım sınırı geçerli). Görev metni:
`C:\Dilmac klasöründe PACKS.md'yi oku ve "Paketleri güncelle" akışını uygula. Bekleyen satır yoksa hiçbir şey
değiştirme. Bitince kısa özet yaz.`
Görev, sırları ekrana yazmaz; `pack_tool` anahtarları config'ten şifreli okur.
