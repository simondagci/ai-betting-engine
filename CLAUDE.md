# CLAUDE.md

iddaa.com (varsayılan) veya Nesine bülteninden futbol (ve basketbol) maçlarını çekip SQLite'a kaydeden, istatistikleri derleyip Google Gemini ile tahmin üreten **kişisel kullanım** amaçlı Python uygulaması. Arayüz: terminal menüsü (`app.py`) + Flask dashboard (`web_server.py`).

## Dokümantasyon Haritası (Önce Bunları Oku)

| Dosya | İçerik | Durum |
| :--- | :--- | :--- |
| [Gemini.md](Gemini.md) | İş akışı, iş kuralları, DB şeması, config, Gemini prompt şablonu | **Ana kaynak (source of truth)** |
| [mdFiles/APIs/iddaa/README.md](mdFiles/APIs/iddaa/README.md) | **iddaa.com API'leri** (varsayılan kaynak): bülten, istatistik, sonuç + Nesine karşılık tablosu | **Güncel** |
| [mdFiles/APIs/01_bulten_api.md](mdFiles/APIs/01_bulten_api.md) | Nesine bülten servisi + `sg.EA[]` alan sözlüğü (`C`, `TYPE`, `HN`, `AN`, `MA`/`OCA`…). Uygulama içi bülten formatı hâlâ budur | Güncel (format referansı) |
| [mdFiles/APIs/02_last_matches_api.md](mdFiles/APIs/02_last_matches_api.md) | Nesine: takımların son maçları | Türkiye dışından erişilemez |
| [mdFiles/APIs/03_competition_history_api.md](mdFiles/APIs/03_competition_history_api.md) | Nesine: H2H geçmişi (`filter=false`) | Türkiye dışından erişilemez |
| [mdFiles/APIs/04_missing_players_api.md](mdFiles/APIs/04_missing_players_api.md) | Nesine: sakat/cezalı oyuncular (iddaa.com'da karşılığı yok) | Türkiye dışından erişilemez |
| [mdFiles/APIs/05_league_table_api.md](mdFiles/APIs/05_league_table_api.md) | Nesine: lig puan durumu | Türkiye dışından erişilemez |
| [mdFiles/APIs/06_match_results_api.md](mdFiles/APIs/06_match_results_api.md) | Nesine: tarih bazlı maç sonuçları (`BetType` 1/2) | Kullanılmıyor |
| [IDDAA_API_ENDPOINTS.md](IDDAA_API_ENDPOINTS.md) | 6 API'nin tek dosyada özeti + curl örnekleri | `mdFiles/APIs/` ile mükerrer |
| [SDSDIDDAA_ANALYSIS.md](SDSDIDDAA_ANALYSIS.md) | Eski Salesforce Apex projesinden göç raporu | **Tarihsel** — şema ve 2.5 gol sınırı artık geçerli değil |

API davranışı değişirse `mdFiles/APIs/iddaa/README.md` (veya Nesine için `0X_*.md`) dosyasını güncelle; iş kuralı değişirse `Gemini.md`'yi güncelle.

## Modüller

| Dosya | Sorumluluk |
| :--- | :--- |
| `config.py` | Tüm ayarlar `.env` üzerinden (`os.getenv` + varsayılan). Yeni ayar buraya eklenir, `.env.example` de güncellenir. |
| `rules.py` | **İş kurallarının tek kaynağı:** İY/MS sonuç etiketi, 3 gol Alt/Üst, KG, tahmin eşleştirme |
| `match_data.py` | Gemini'ye gönderme kuralı: son maçlar (iki takım) + H2H yoksa maç gönderilmez (`load_context_for_analysis`, `REQUIRED_SECTIONS`); istatistik birleştirme (`merge_stats`) |
| `background_job.py` | Genel arka plan iş altyapısı (`BackgroundJob`): tek iş, ilerleme / kalan süre, iptal, art arda hata sınırı, `StopJob` |
| `prediction_batch.py` | "Maçları Analiz Et": bitmemiş, analiz edilmemiş, son maçlar + H2H dolu, ligi izinli maçları arka planda Gemini'ye gönderir (`/api/admin/jobs/predictions`). Gemini kapatılırsa veya art arda 5 hata olursa durur |
| `stats_sync.py` | Toplu istatistik işi: bülten sonrası arka plan thread'i (web, `/api/admin/jobs/stats` ilerleme) veya terminalde ilerleme çubuğu. Aynı anda tek iş; yarıda kalırsa kalan maçlar sonraki çalıştırmada işlenir |
| `data_source.py` | Aktif veri kaynağı seçimi (`.env` `DATA_SOURCE=iddaa\|nesine`). `app.py` / `web_server.py` kaynak modüllerini doğrudan değil bunun üzerinden çağırır |
| `iddaa_api.py` | iddaa.com bülten / istatistik / sonuç çağrıları; veriyi Nesine bülten formatına ve uygulama istatistik formatına dönüştürür |
| `nesine_api.py` | Nesine HTTP çağrıları (`fetch_*`, `fetch_match_stats`), market sınıflandırma (`_classify_market`) + `extract_odds_from_event`, `_clean_nesine_data_json` |
| `database.py` | SQLite şeması (`init_db`), `db_session`, tüm sorgular, saat dilimi dönüşümü, 150 dk kuralı, doğruluk hesabı |
| `gemini_engine.py` | Prompt oluşturma (`build_prompt`), `MatchPrediction` Pydantic şeması, Gemini çağrısı + retry/fallback |
| `app.py` | Terminal menüsü — `MENU_ITEMS` listesi (etiket, fonksiyon) + `main_menu()` |
| `web_server.py` | Flask dashboard + `/api/...` JSON endpoint'leri (bülten / sonuç / toplu istatistik senkronizasyonu, tek maç istatistik, skor girişi dahil) |
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
- **Basketbol farklıdır:** KG ve Alt/Üst yok, MS beraberliksiz (uzatmalar dahil); Gemini "Maç Sonu Toplam Sayı" (`predicted_total_points`) tahmin eder. Spora göre dallanma `rules.is_basketball(sport_type)` ile yapılır; `evaluate_prediction` sport_type parametresi alır. Basketbol oranları `nesine_api._classify_basketball_market` ile etiketlenir (futbol sınıflandırıcısı basketbol marketlerini yanlış etiketler).
- **150 dakika kuralı:** başlama saatinden `MATCH_COMPLETION_DELAY_MINUTES` (150) dk sonra `match_completed = 1`, `status = 'COMPLETED'`.
- **Gemini izinleri iki katmanlı:** genel anahtar **ve** `REQUIRE_LEAGUE_APPROVAL` açıksa yalnızca `leagues.gemini_analysis = 1` olan ligler. Yeni ligler varsayılan kapalı (`0`).
  - Genel anahtarın etkin değeri `database.is_gemini_enabled(conn)`: Web › Lig Yönetimi › Ayarlar'dan kaydedilen `settings` tablosu değeri, yoksa `.env` `ENABLE_GEMINI_ANALYSIS`. Kodda `config.ENABLE_GEMINI_ANALYSIS`'i doğrudan okuma.
  - Gemini'ye istek atan her web endpoint'i `_gemini_disabled_response` ile kontrol eder (403 `GEMINI_DISABLED`). Tek maç analizi lig izni kapalıysa 409 `LEAGUE_DISABLED` döner; `{"enable_league": true}` ile izin açılıp analiz yapılır.
  - **En güçlü tercih (⭐):** Gemini sadece market seçer (`best_bet_market`); seçim değeri `gemini_engine.finalize_best_bet` ile ilgili tahmin alanından alınır, böylece tahminle çelişmez. Market anahtarları `rules.BEST_BET_MARKETS` (`ft` / `ht` / `over_under` / `kg`); değerlendirme `evaluate_prediction(...)["best_bet"]`. Lig bazında oran `database.get_league_prediction_stats` (maç başına son tahmin).
- **Son maçlar ve H2H yoksa Gemini'ye gönderilmez:** web (409 `MISSING_STATS`), lig analizi (`skipped`) ve terminal aynı kuralı `match_data.load_context_for_analysis` ile uygular (göndermeden önce veri kaynağından bir kez daha denenir).
- **Gemini sadece maç analizi (tahmin) için kullanılır.** Maç detayları, son maçlar, H2H ve puan durumu yalnızca veri kaynağından (`data_source`) gelir; Gemini ile istatistik çekme (eski "İnternetten Tara" / `fetch_live_team_stats`) kaldırıldı, geri ekleme.
- **Spor ayrımı:** bülten `TYPE "1"` = futbol, `"2"` = basketbol, `"-1"` = uzun vadeli (atlanır). Skor tipi futbolda `FT`, basketbolda `Current`; ilk yarı her ikisinde `HT`.
- **Saat dilimi:** bülten saatleri `Europe/Istanbul`, gösterim `Europe/Berlin` (`convert_tr_to_local_time`).

## Kod Kuralları

- **Döngüde SQL yok:** bülten senkronizasyonu (`sync_bulletin_matches_and_odds`) önce mevcut kayıtları hafızaya alır, sonra `executemany` ile toplu yazar. Yeni toplu işlemlerde aynı deseni kullan.
- **Mükerrer kayıt yok:** zaten var olan maç kodları atlanır (`get_existing_match_codes`).
- **Veri kaynağı:** Nesine istatistik servisleri Türkiye dışından bölge engeline takılır; varsayılan kaynak iddaa.com. Maç kodları (`C` = `i`) ve lig ID'leri (`LC` = `ci`) iki kaynakta aynı, lig/takım adları farklı olabilir: ligleri isimle değil `leagues.external_id` ile eşleştir. iddaa oranlarında Nesine ile aynı olan `wodd` (online oran) kullanılır.
- Nesine istatistik servisleri `Data` alanında **string içinde kaçışlı JSON** döner ve hata durumunda HTML dönebilir — her zaman `_clean_nesine_data_json` ile parse et, boş `{}` durumunu ele al.
- DB fonksiyonları `conn`'u parametre alır; `get_connection` `sqlite3.Row` döner. Çağıran tarafta bağlantıyı `with database.db_session() as conn:` ile aç (commit/rollback + close otomatik). `with get_connection()` bağlantıyı **kapatmaz**, kullanma.
- Maç sorgularında ortak `_MATCH_COLUMNS` / `_MATCH_JOINS` / `_CHRONOLOGICAL_DATE` parçalarını kullan; `DD.MM.YYYY` tarihleri düz metin olarak sıralanamaz.
- Python **3.9** uyumlu yaz (`X | Y` tip sözdizimi ve `match` kullanma).
- Arayüzde iddaa.com / Nesine / Gemini kaynaklı her metni `innerHTML`'e basmadan önce `esc()` ile kaçışla.
- Bir maçın birden fazla tahmini olabilir; listelerde `_LATEST_PREDICTION_JOIN` ile sadece en son tahmini kullan (aksi halde satırlar çoğalır).
- Kullanıcıya dönük metinler, prompt ve yorumlar **Türkçe**; fonksiyon/değişken isimleri İngilizce.
- Gemini prompt'u son maç / H2H / puan durumunu ham JSON yerine kompakt satırlarla gönderir (`_format_history`, `_format_league_table`); karakter sınırıyla kesme yapma, satır sayısıyla sınırla. "Oynanmaması gerekenler" (avoid_bets) token tasarrufu için kaldırıldı; `predictions.avoid_bets_json` eski sütun olarak kaldı, kullanılmıyor — geri ekleme.
- Gemini çıktısı `response_mime_type="application/json"` + Pydantic şema ile alınır; şema alanı değişirse `predictions` tablosu, `save_prediction` ve `Gemini.md` birlikte güncellenmeli.

## Dikkat

- `.env` ve `*.db` gitignore'da — `GEMINI_API_KEY`'i asla koda veya dokümana yazma.
- `betting_engine.db` gerçek veri içerir; şema değişikliği `CREATE TABLE IF NOT EXISTS` ile mevcut tabloyu güncellemez, gerekirse `ALTER TABLE` migration ekle.
- Testler geçici dizinde (`tempfile.gettempdir()`) `test_betting_engine.db` oluşturur/siler; gerçek DB'ye dokunmaz.
- Arka plan işleri (istatistik, toplu analiz) web sunucusu sürecinde çalışır: uzun bir iş sürerken kod dosyası değiştirmek debug reloader'ı tetikler ve işi durdurur.
- Arka plan istatistik işi web sunucusu sürecinde çalışır: sunucu yeniden başlarsa (debug reloader dahil) iş durur, tekrar başlatıldığında kalan maçlardan devam eder.
- Dashboard `debug=True` ile çalışıyorsa Flask reloader dosya değişikliklerinde sunucuyu otomatik yeniden başlatır ve `update_expired_matches` gerçek DB'ye yazar. Düzenleme sırasında modül kısa süre bile import edilemez hale gelirse reloader kapanır; çok adımlı düzenlemelerde önce bağımlılıkları (import'lar, yardımcı fonksiyonlar) ekle.
- `Gemini.md` şemasında `match_stats` tablosu (H2H, son maçlar, eksik oyuncu, puan tablosu JSON'ları) eksik — gerçek şema için `database.init_db`'ye bak.
