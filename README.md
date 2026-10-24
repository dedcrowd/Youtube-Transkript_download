Too Many Request sorununu çözmek için 429 u yani Cookie-editör ile youtube cookielerini netscape ile export et ve böyle ver
‘’’
python3 yt_transcripts.py -t nahamsec —cookies ~/cookies.txt —sleep-min 8 —sleep-max 15

‘’’
Kurulum
```
pip install -U yt-dlp
pip install faster-whisper   # opsiyonel, kalite için önerilir
```
Kullanım
```
python yt_transcripts.py -t nahamsec
```
```
python yt_transcripts.py -t criticalthinkingpodcast
```
```
python yt_transcripts.py -s https://youtube.com/watch?v=XXXXXXXXXXX
```
```
python yt_transcripts.py -t nahamsec —limit 3
```
```
python yt_transcripts.py -t nahamsec —whisper always
```
Dizin yapısı
Desktop/transcripts/<Kanal>/<Video Başlığı [id]>/ altında şunlar oluşuyor:

• transcript.txt: temiz düz metin
• transcript_timestamped.txt: zaman damgalı metin
• transcript.srt: altyazı dosyası
• chunks.md: 10 dakikalık bloklar (Claude Code’a vermek için ideal)
• metadata.json: başlık, tarih, süre, bölümler, transkript kaynağı
• description.txt: video açıklaması (araç ve link listeleri burada çıkıyor)

Kanal klasöründe ayrıca _index.json, _failed.txt ve run.log tutuluyor.

Önlemler
• Devam etme: İnternet kopsa veya Ctrl+C yapsan bile tekrar çalıştırınca bitenleri atlayıp kaldığı yerden devam ediyor.
• Eksiksiz liste: Kanalın hem Videos hem Streams sekmesini tarıyor. NahamSec’te uzun canlı yayın kayıtları var, bunlar da dahil.
• Altyazı kalitesi: Önce insan yazımı altyazıyı arıyor, yoksa otomatik olana düşüyor. Otomatik altyazı şüpheli derecede kısaysa veya hiç altyazı yoksa Whisper large-v3’e geçiyor. Whisper’a bug bounty terimlerini (SSRF, ffuf, nuclei vb.) ipucu olarak veriyor.
• Engel koruması: Rastgele bekleme (3-8 sn), üstel geri çekilme ve 429 / “bot değilsin” hatasında uzun bekleme var. Üst üste 3 engel gelirse güvenli şekilde duruyor.
• Atlanan videolar: Özel, üyelere özel, henüz bitmemiş canlı yayın gibi videoları atlayıp nedenini _failed.txt’ye yazıyor.
• Dosya adları: Windows’ta sorun çıkaracak karakterler ve uzun adlar temizleniyor, aynı başlıklar çakışmasın diye video ID’si ekleniyor.

İpuçları
• YouTube sık değiştiği için pip install -U yt-dlp komutunu düzenli çalıştır.
• Engel yersen —cookies-from-browser chrome ekle.
• Kanal adıyla bulamazsa tam URL ver: -t https://www.youtube.com/@nahamsec.
Sonraki adımda chunks.md dosyalarını işleyip zafiyet sınıfına göre bilgi tabanı çıkaracak Claude Code promptunu yazdirabilirsin yapay zekaya ve islemleri daha iyi analiz ettirebilirsin..
