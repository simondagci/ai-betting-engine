# CLAUDE.md

Nesine/İddaa bülteninden futbol (ve basketbol) maçlarını çekip SQLite'a kaydeden, istatistikleri derleyip Google Gemini ile tahmin üreten **kişisel kullanım** amaçlı Python uygulaması. Arayüz: terminal menüsü (`app.py`) + Flask dashboard (`web_server.py`).

## Dokümantasyon Haritası (Önce Bunları Oku)

| Dosya | İçerik | Durum |
| :--- | :--- | :--- |
| [Gemini.md](Gemini.md) | İş akışı, iş kuralları, DB şeması, config, Gemini prompt şablonu | **Ana kaynak (source of truth)** |
| [mdFiles/APIs/01_bulten_api.md](mdFiles/APIs/01_bulten_api.md) | Bülten servisi + `sg.EA[]` alan sözlüğü (`C`, `TYPE`, `HN`, `AN`, `MA`/`OCA`…) | Güncel |
| [mdFiles/APIs/02_last_matches_api.md](mdFiles/APIs/02_last_matches_api.md) | Takımların son maçları | Güncel |
| [mdFiles/APIs/03_competition_history_api.md](mdFiles/APIs/03_competition_history_api.md) | H2H geçmişi (`filter=false`) | Güncel |
| [mdFiles/APIs/04_missing_players_api.md](mdFiles/APIs/04_missing_players_api.md) | Sakat/cezalı oyuncular | Güncel |
| [mdFiles/APIs/05_league_table_api.md](mdFiles/APIs/05_league_table_api.md) | Lig puan durumu | Güncel |
| [mdFiles/APIs/06_match_results_api.md](mdFiles/APIs/06_match_results_api.md) | Tarih bazlı maç sonuçları (`BetType` 1/2) | Güncel |
| [IDDAA_API_ENDPOINTS.md](IDDAA_API_ENDPOINTS.md) | 6 API'nin tek dosyada özeti + curl örnekleri | `mdFiles/APIs/` ile mükerrer |
| [SDSDIDDAA_ANALYSIS.md](SDSDIDDAA_ANALYSIS.md) | Eski Salesforce Apex projesinden göç raporu | **Tarihsel** — şema ve 2.5 gol sınırı artık geçerli değil |

API davranışı değişirse ilgili `mdFiles/APIs/0X_*.md` dosyasını güncelle; iş kuralı değişirse `Gemini.md`'yi güncelle.

## Modüller

| Dosya | Sorumluluk |
| :--- | :--- |
| `config.py` | Tüm ayarlar `.env` üzerinden (`os.getenv` + varsayılan). Yeni ayar buraya eklenir, `.env.example` de güncellenir. |
| `rules.py` | **İş kurallarının tek kaynağı:** İY/MS sonuç etiketi, 3 gol Alt/Üst, KG, tahmin eşleştirme |
| `nesine_api.py` | Nesine HTTP çağrıları (`fetch_*`, `fetch_match_stats`), market sınıflandırma (`_classify_market`) + `extract_odds_from_event`, `_clean_nesine_data_json` |
| `database.py` | SQLite şeması (`init_db`), `db_session`, tüm sorgular, saat dilimi dönüşümü, 150 dk kuralı, doğruluk hesabı |
| `gemini_engine.py` | Prompt oluşturma (`build_prompt`), `MatchPrediction` Pydantic şeması, Gemini çağrısı + retry/fallback |
| `app.py` | Terminal menüsü — `MENU_ITEMS` listesi (etiket, fonksiyon) + `main_menu()` |
| `web_server.py` | Flask dashboard + `/api/...` JSON endpoint'leri (bülten senkronizasyonu, Nesine istatistik, skor girişi dahil) |
| `templates/index.html` | Liste görünümü (sıralanabilir maç tablosu, `?view=` ile görünüm seçimi) + Lig Yönetimi (`#admin`) sekmesi |
| `templates/match_detail.html` | `/matches/<id>` maç kayıt sayfası: highlights, Quick Actions, İstatistikler/Oranlar/Detaylar sekmeleri |
| `static/app.css`, `static/common.js` | Ortak açık tema (Lightning esinli) ve JS yardımcıları (`esc`, `api`, `showToast`, `openModal`) |
| `main.py` | Sadece `app.main_menu()` çağırır |
| `test_app.py` | `unittest` testleri |

## Komutlar

```bash
pip install -r requirements.txt
cp .env.example .env          # GEMINI_API_KEY vb. doldur
python3 main.py                # terminal menüsü
python3 -c "import web_server; web_server.start_server()"   # dashboard: http://127.0.0.1:5001
python3 -m unittest test_app -v
```

## Kritik İş Kuralları (Kodda Zorunlu)

- **Alt/Üst sınırı 3 goldür, 2.5 değil:** toplam gol `> 3` (≥4) → ÜST, `≤ 3` → ALT. Bkz. `rules.OVER_UNDER_GOAL_LIMIT`, `MatchPrediction.predicted_over_under_3`. Sonuç/gol hesabını başka yerde yeniden yazma, `rules.py`'yi kullan.
- **KG VAR:** her iki takım da ≥1 gol.
- **150 dakika kuralı:** başlama saatinden `MATCH_COMPLETION_DELAY_MINUTES` (150) dk sonra `match_completed = 1`, `status = 'COMPLETED'`.
- **Gemini izinleri iki katmanlı:** genel anahtar **ve** `REQUIRE_LEAGUE_APPROVAL` açıksa yalnızca `leagues.gemini_analysis = 1` olan ligler. Yeni ligler varsayılan kapalı (`0`).
  - Genel anahtarın etkin değeri `database.is_gemini_enabled(conn)`: Web › Lig Yönetimi › Ayarlar'dan kaydedilen `settings` tablosu değeri, yoksa `.env` `ENABLE_GEMINI_ANALYSIS`. Kodda `config.ENABLE_GEMINI_ANALYSIS`'i doğrudan okuma.
  - Gemini'ye istek atan her web endpoint'i `_gemini_disabled_response` ile kontrol eder (403 `GEMINI_DISABLED`). Tek maç analizi lig izni kapalıysa 409 `LEAGUE_DISABLED` döner; `{"enable_league": true}` ile izin açılıp analiz yapılır.
  - Maç detay sayfası (`/details`) Gemini'yi **asla** çağırmaz; Gemini canlı istatistik sadece "İnternetten Tara" (`refresh_stats`) ile çekilir.
- **Spor ayrımı:** bülten `TYPE "1"` = futbol, `"2"` = basketbol, `"-1"` = uzun vadeli (atlanır). Skor tipi futbolda `FT`, basketbolda `Current`; ilk yarı her ikisinde `HT`.
- **Saat dilimi:** bülten saatleri `Europe/Istanbul`, gösterim `Europe/Berlin` (`convert_tr_to_local_time`).

## Kod Kuralları

- **Döngüde SQL yok:** bülten senkronizasyonu (`sync_bulletin_matches_and_odds`) önce mevcut kayıtları hafızaya alır, sonra `executemany` ile toplu yazar. Yeni toplu işlemlerde aynı deseni kullan.
- **Mükerrer kayıt yok:** zaten var olan maç kodları atlanır (`get_existing_match_codes`).
- Nesine istatistik servisleri `Data` alanında **string içinde kaçışlı JSON** döner ve hata durumunda HTML dönebilir — her zaman `_clean_nesine_data_json` ile parse et, boş `{}` durumunu ele al.
- DB fonksiyonları `conn`'u parametre alır; `get_connection` `sqlite3.Row` döner. Çağıran tarafta bağlantıyı `with database.db_session() as conn:` ile aç (commit/rollback + close otomatik). `with get_connection()` bağlantıyı **kapatmaz**, kullanma.
- Maç sorgularında ortak `_MATCH_COLUMNS` / `_MATCH_JOINS` / `_CHRONOLOGICAL_DATE` parçalarını kullan; `DD.MM.YYYY` tarihleri düz metin olarak sıralanamaz.
- Python **3.9** uyumlu yaz (`X | Y` tip sözdizimi ve `match` kullanma).
- Arayüzde Nesine/Gemini kaynaklı her metni `innerHTML`'e basmadan önce `esc()` ile kaçışla.
- Bir maçın birden fazla tahmini olabilir; listelerde `_LATEST_PREDICTION_JOIN` ile sadece en son tahmini kullan (aksi halde satırlar çoğalır).
- Kullanıcıya dönük metinler, prompt ve yorumlar **Türkçe**; fonksiyon/değişken isimleri İngilizce.
- Gemini çıktısı `response_mime_type="application/json"` + Pydantic şema ile alınır; şema alanı değişirse `predictions` tablosu, `save_prediction` ve `Gemini.md` birlikte güncellenmeli.

## Dikkat

- `.env` ve `*.db` gitignore'da — `GEMINI_API_KEY`'i asla koda veya dokümana yazma.
- `betting_engine.db` gerçek veri içerir; şema değişikliği `CREATE TABLE IF NOT EXISTS` ile mevcut tabloyu güncellemez, gerekirse `ALTER TABLE` migration ekle.
- Testler geçici dizinde (`tempfile.gettempdir()`) `test_betting_engine.db` oluşturur/siler; gerçek DB'ye dokunmaz.
- Dashboard `debug=True` ile çalışıyorsa Flask reloader dosya değişikliklerinde sunucuyu otomatik yeniden başlatır ve `update_expired_matches` gerçek DB'ye yazar. Düzenleme sırasında modül kısa süre bile import edilemez hale gelirse reloader kapanır; çok adımlı düzenlemelerde önce bağımlılıkları (import'lar, yardımcı fonksiyonlar) ekle.
- `Gemini.md` şemasında `match_stats` tablosu (H2H, son maçlar, eksik oyuncu, puan tablosu JSON'ları) eksik — gerçek şema için `database.init_db`'ye bak.
