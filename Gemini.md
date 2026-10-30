# Gemini.md — Kodlama Asistanı Bağlam Dosyası

> Bu dosya **Gemini kodlama asistanının** proje bağlamıdır (Claude için karşılığı `CLAUDE.md`). Bu projede iki asistan
> birlikte çalışır: Gemini kod yazar, Claude refactor eder. Kod yazmadan önce bu dosyayı oku; mimari, iş kuralı veya
> modül sorumluluğu değiştiğinde **bu dosyayı ve `CLAUDE.md`'yi birlikte güncelle**.
>
> Not: Uygulamanın maç tahmini için çağırdığı Gemini API'si bu dosyayı okumaz; gerçek tahmin prompt'u
> `gemini_engine.py` içinde oluşturulur (bkz. §8).

## 1. Uygulama Amacı

iddaa.com (varsayılan) veya Nesine bülteninden futbol ve basketbol maçlarını çekip SQLite'a kaydeden, istatistikleri
(son maçlar, H2H, puan durumu) derleyip Google Gemini API ile tahmin üreten **kişisel kullanım** amaçlı Python uygulaması.
Arayüz: terminal menüsü (`app.py`) + Flask dashboard (`web_server.py`, http://127.0.0.1:5001).

```bash
pip install -r requirements.txt
cp .env.example .env          # GEMINI_API_KEY vb. doldur
python3 main.py                # terminal menüsü
python3 -c "import web_server; web_server.start_server()"   # dashboard
python3 -m unittest test_app -v
```

---

## 2. Modül Haritası

| Dosya | Sorumluluk |
| :--- | :--- |
| `config.py` | Tüm ayarlar `.env` üzerinden (`os.getenv` + varsayılan). Yeni ayar buraya **ve** `.env.example`'a eklenir. |
| `rules.py` | **İş kurallarının tek kaynağı:** İY/MS sonuç etiketi, 3 gol Alt/Üst, KG, tahmin değerlendirme (`evaluate_prediction`), değerli tercih adayları (`bet_candidates`, `BET_OPTIONS`), seçim değerlendirme (`selection_won`), Türkçe karakter normalizasyonu (`normalize_selection`) |
| `match_data.py` | Gemini'ye gönderme kuralı: `REQUIRED_SECTIONS` (ev son maçlar, deplasman son maçlar, H2H), `load_context_for_analysis` (eksikse veri kaynağından bir kez daha dener), `merge_stats`, `fetch_and_save_stats` |
| `background_job.py` | Genel arka plan iş altyapısı `BackgroundJob`: aynı anda tek iş, paralel worker'lar (ortak kuyruk), ilerleme / kalan süre, iptal, art arda hata sınırı, `StopJob` |
| `stats_sync.py` | Toplu istatistik işi (bülten sonrası otomatik başlar). Web'de arka plan thread'i, terminalde ilerleme çubuğu. Yarıda kalırsa kalan maçlar sonraki çalıştırmada işlenir |
| `prediction_batch.py` | "🔮 Maçları Analiz Et": bitmemiş, analiz edilmemiş, istatistikleri dolu, ligi izinli maçları `GEMINI_PARALLEL_REQUESTS` paralel istekle Gemini'ye gönderir. Gemini kapatılırsa (`StopJob`) veya art arda 5 hata olursa durur |
| `data_source.py` | Aktif veri kaynağı seçimi (`DATA_SOURCE=iddaa\|nesine`). `app.py` / `web_server.py` kaynak modüllerini **doğrudan değil bunun üzerinden** çağırır |
| `iddaa_api.py` | iddaa.com bülten / istatistik / sonuç çağrıları; veriyi Nesine bülten formatına ve uygulama istatistik formatına dönüştürür |
| `nesine_api.py` | Nesine HTTP çağrıları, market sınıflandırma (`_classify_market`, basketbol için `_classify_basketball_market`), `extract_odds_from_event`, `_clean_nesine_data_json` |
| `database.py` | SQLite şeması (`init_db` + `ALTER TABLE` migration'ları), `db_session`, tüm sorgular, saat dilimi dönüşümü, 150 dk kuralı, doğruluk hesabı, `settings` tablosu |
| `gemini_engine.py` | Prompt oluşturma (`build_prompt`, `_build_basketball_prompt`), Pydantic şemalar (`MatchPrediction`, `BasketballPrediction`), Gemini çağrısı + retry/fallback + zaman aşımı, `finalize_best_bet` |
| `app.py` | Terminal menüsü — `MENU_ITEMS` listesi (etiket, fonksiyon) + `main_menu()` |
| `web_server.py` | Flask dashboard + `/api/...` JSON endpoint'leri (bkz. §7) |
| `templates/index.html` | Maç listesi (sıralanabilir tablo, `?view=`), iş ilerleme çubukları, Lig Yönetimi (`#admin`: Ligler & Bülten, Ayarlar) |
| `templates/match_detail.html` | `/matches/<id>`: highlights, Quick Actions, İstatistikler / Oranlar / Detaylar sekmeleri, ⭐ değerli tercih kutusu |
| `static/app.css`, `static/common.js` | Ortak açık tema (Salesforce Lightning esinli) ve JS yardımcıları (`esc`, `api`, `showToast`, `openModal`, `requestPrediction`, `pickMainOdds`) |
| `main.py` | Sadece `app.main_menu()` çağırır |
| `test_app.py` | `unittest` testleri (ağ çağrıları mock'lanır, geçici DB kullanılır) |

API dokümanları: `mdFiles/APIs/iddaa/README.md` (iddaa.com, varsayılan), `mdFiles/APIs/0X_*.md` (Nesine; bülten alan
sözlüğü `01_bulten_api.md` — uygulama içi bülten formatı hâlâ budur). `SDSDIDDAA_ANALYSIS.md` tarihseldir, şeması ve 2.5 gol sınırı geçerli değildir.

---

## 3. İş Akışı

1. **Bülten:** iddaa.com `sportsbook/events` tek seferde çekilir, Nesine `sg.EA[]` formatına dönüştürülür. `bri == 0` olan
   özel etkinlikler ("Haftalık Bahisler" vb.) maç değildir, atlanır. Zaten kayıtlı maç kodları atlanır (mükerrer yok).
2. **İstatistik:** bülten sonrası istatistiği olmayan bitmemiş maçlar için arka planda otomatik çekilir (maç başına 4 paralel
   istek: ev son maçlar, deplasman son maçlar, H2H, puan durumu). Sakat/cezalı oyuncu iddaa.com'da yok.
   Maç detayları ve son maçlar **yalnızca veri kaynağından** gelir; Gemini istatistik çekmek için kullanılmaz.
3. **Gönderim kuralı:** ev ve deplasman son maçları ile H2H yoksa maç Gemini'ye **gönderilmez**.
4. **Tahmin:** tek maç (detay sayfasındaki buton) veya toplu "🔮 Maçları Analiz Et". Gemini **sadece butona basınca**
   çağrılır, sayfa açılışında asla.
5. **Sonuç:** Lig Yönetimi › "Sonuçları Çek" iddaa.com `live-results` servisinden (`s == 6` = bitti) skorları yazar;
   manuel skor girişi de vardır. Tahmin doğruluğu ve lig bazında ⭐ isabet oranı otomatik hesaplanır.

---

## 4. İş Kuralları (Kodda Zorunlu)

- **Alt/Üst sınırı 3 goldür, 2.5 değil:** toplam gol `> 3` (≥4) → ÜST, `≤ 3` → ALT (bülten karşılığı 3.5 baremi).
  `rules.OVER_UNDER_GOAL_LIMIT`, `MatchPrediction.predicted_over_under_3`. Sonuç/gol hesabını başka yerde yeniden yazma, `rules.py`'yi kullan.
- **KG VAR:** her iki takım da ≥1 gol.
- **Basketbol farklıdır:**
  - KG ve Alt/Üst yok; MS beraberliksiz (uzatmalar dahil: MS 1 / MS 2); İY 1 / İY 0 / İY 2.
  - Gemini **Maç Sonu Toplam Sayı** (`predicted_total_points`) tahmin eder; tahmini skordaki iki sayının toplamına eşitlenir.
  - Skor girişinde üst sınır yok (futbolda 0–99). Basketbol sonuçlarına 3 gol / KG yazılmaz (NULL).
  - Spora göre dallanma `rules.is_basketball(sport_type)` ile; `evaluate_prediction` `sport_type` parametresi alır.
  - Basketbol oranları `nesine_api._classify_basketball_market` ile etiketlenir (futbol sınıflandırıcısı yanlış etiketler).
- **Spor ayrımı:** bülten `TYPE "1"` = futbol, `"2"` = basketbol, `"-1"` = uzun vadeli (atlanır). Skor tipi futbolda `FT`, basketbolda `Current`; ilk yarı her ikisinde `HT`.
- **150 dakika kuralı:** başlama saatinden `MATCH_COMPLETION_DELAY_MINUTES` (150) dk sonra `match_completed = 1`, `status = 'COMPLETED'`.
- **Saat dilimi:** bülten saatleri `Europe/Istanbul`, gösterim `Europe/Berlin` (`convert_tr_to_local_time`).
- **Gemini izinleri iki katmanlı:** genel anahtar **ve** `REQUIRE_LEAGUE_APPROVAL` açıksa yalnızca `leagues.gemini_analysis = 1` olan ligler. Yeni ligler varsayılan kapalı (`0`).
  - Genel anahtarın etkin değeri `database.is_gemini_enabled(conn)`: Web › Lig Yönetimi › Ayarlar'dan kaydedilen `settings` değeri, yoksa `.env` `ENABLE_GEMINI_ANALYSIS`. **`config.ENABLE_GEMINI_ANALYSIS`'i doğrudan okuma.**
  - Gemini'ye istek atan her web endpoint'i `_gemini_disabled_response` ile kontrol eder (403 `GEMINI_DISABLED`).
  - Tek maç analizi: lig izni kapalıysa 409 `LEAGUE_DISABLED` (`{"enable_league": true}` ile izin açılıp analiz yapılır); istatistik eksikse 409 `MISSING_STATS`.
- **⭐ Değerli tercih (value bet):**
  - Prompt'a sadece oranı `MIN_BEST_BET_ODDS` (1.30) ve üstü adaylar (`rules.bet_candidates`) oran ve piyasa olasılığıyla gider.
    Futbol: MS, İY, 3 gol ALT/ÜST (3.5 baremi), KG, Çifte Şans. Basketbol: sadece MS ve İY.
  - Gemini her aday için kendi olasılığını tahmin eder; olasılık × oran (beklenen değer) en yüksek ve 1'den büyük seçimi
    `best_bet_selection` olarak yazar, hiçbiri değerli değilse `'YOK'`.
  - `gemini_engine.finalize_best_bet` seçimin aday listesinde olduğunu ve beklenen değerin > 1 olduğunu doğrular; değilse tercih boşaltılır.
  - Değerli tercih "en olası" tahminden farklı olabilir; değerlendirme `rules.selection_won` ile **seçimin kendisine** göre yapılır. Oran `predictions.best_bet_odds`'a yazılır.
- **Gemini sadece maç analizi (tahmin) için kullanılır.** Eski "İnternetten Tara" / `fetch_live_team_stats` kaldırıldı — **geri ekleme**.
- **"Oynanmaması gerekenler" (avoid_bets)** token tasarrufu için kaldırıldı; `predictions.avoid_bets_json` eski sütun olarak duruyor, kullanılmıyor — **geri ekleme**.

---

## 5. Kod Kuralları

- **Python 3.9** uyumlu yaz: `X | Y` tip sözdizimi ve `match` ifadesi kullanma (`Optional[...]`, `Dict[...]` kullan).
- **Dil:** kullanıcıya dönük metinler, prompt ve yorumlar **Türkçe**; fonksiyon/değişken isimleri **İngilizce**.
- **DB bağlantısı:** DB fonksiyonları `conn`'u parametre alır; `get_connection` `sqlite3.Row` döner. Çağıran tarafta
  `with database.db_session() as conn:` kullan (commit/rollback + close otomatik). `with get_connection()` bağlantıyı **kapatmaz**, kullanma.
  Uzun süren Gemini isteği sırasında bağlantıyı açık tutma (bkz. `prediction_batch.analyze_one`).
- **Döngüde SQL yok:** önce mevcut kayıtları hafızaya al, sonra `executemany` ile toplu yaz (örnek: `sync_bulletin_matches_and_odds`).
- **Mükerrer kayıt yok:** var olan maç kodları atlanır (`get_existing_match_codes`).
- **Maç sorguları:** ortak `_MATCH_COLUMNS` / `_MATCH_JOINS` / `_CHRONOLOGICAL_DATE` parçalarını kullan; `DD.MM.YYYY` tarihleri düz metin olarak sıralanamaz.
- **Birden fazla tahmin:** bir maçın birden fazla tahmini olabilir; listelerde `_LATEST_PREDICTION_JOIN` ile sadece en sonu kullan (aksi halde satırlar çoğalır).
- **Veri kaynağı:** kaynak modüllerini doğrudan çağırma, `data_source` üzerinden git. Maç kodları (`C` = `i`) ve lig ID'leri
  (`LC` = `ci`) iki kaynakta aynı; lig/takım adları farklı olabilir → ligleri isimle değil `leagues.external_id` ile eşleştir.
  iddaa oranlarında Nesine ile aynı olan `wodd` (online oran) kullanılır.
- **Nesine istatistik** servisleri `Data` alanında string içinde kaçışlı JSON döner, hata durumunda HTML dönebilir → her zaman `_clean_nesine_data_json` ile parse et, boş `{}` durumunu ele al.
- **Arka plan işi** yazacaksan `BackgroundJob`'ı kullan (yeni thread/kuyruk altyapısı yazma). Worker bir sonuç anahtarı
  (`outcomes`) döner; işi tamamen durdurmak için `StopJob` fırlat.
- **Gemini çıktısı** `response_mime_type="application/json"` + Pydantic şema ile alınır. Şema alanı değişirse `predictions`
  tablosu (migration), `save_prediction`, bu dosya ve `CLAUDE.md` birlikte güncellenmeli.
- **Prompt verisi:** son maç / H2H / puan durumu ham JSON yerine kompakt satırlarla gider (`_format_history`, `_format_league_table`); karakter sınırıyla kesme yapma, satır sayısıyla sınırla (`MAX_HISTORY_ROWS`, `MAX_TABLE_ROWS`, `MAX_ODDS_ROWS`).
- **Arayüz güvenliği:** iddaa.com / Nesine / Gemini kaynaklı her metni `innerHTML`'e basmadan önce `esc()` ile kaçışla. Görünürlük `hidden` özelliğiyle yönetilir (`[hidden]{display:none!important}`).
- **Yeni ayar:** `config.py` + `.env.example`; gizli anahtarları asla koda yazma.
- **Test:** yeni davranış için `test_app.py`'ye test ekle; ağ çağrılarını mock'la. Paralel iş testlerinde deterministik sonuç gerekiyorsa `workers = 1` patch'le.

---

## 6. Veritabanı Şeması (SQLite — gerçek kaynak `database.init_db`)

| Tablo | Sütunlar |
| :--- | :--- |
| `teams` | `id`, `name`, `sport_type` |
| `leagues` | `id`, `name`, `sport_type`, `gemini_analysis` (0/1), `external_id` (kaynak lig ID'si, eşleştirme anahtarı) |
| `matches` | `id`, `code` (bülten maç kodu), `home_team_id`, `away_team_id`, `league_id`, `match_date` (`DD.MM.YYYY`), `match_time`, `sport_type`, `status`, `match_completed` (0/1) |
| `odds` | `id`, `match_id`, `market_type`, `market_name`, `option_name`, `odds_value` |
| `match_stats` | `id`, `match_id` (UNIQUE), `h2h_json`, `home_last_matches_json`, `away_last_matches_json`, `missing_players_json`, `league_table_json`, `updated_at` |
| `predictions` | `id`, `match_id`, `predicted_ht_result`, `predicted_ft_result`, `predicted_over_under_3`, `predicted_kg`, `predicted_score`, `confidence_score`, `ai_analysis_summary`, `predicted_total_points` (basketbol), `best_bet_market`, `best_bet_selection`, `best_bet_confidence`, `best_bet_reason`, `best_bet_odds`, `over_under_line`, `avoid_bets_json` (kullanılmıyor), `created_at` |
| `results` | `id`, `match_id`, `ht_home_score`, `ht_away_score`, `ft_home_score`, `ft_away_score`, `is_over_3_goals`, `is_kg_var` (basketbolda NULL), `updated_at` |
| `settings` | `key` (PK), `value`, `updated_at` — örn. panelden kaydedilen Gemini genel anahtarı |

`CREATE TABLE IF NOT EXISTS` mevcut tabloyu güncellemez: yeni sütun için `init_db` içine `ALTER TABLE` migration ekle.
`betting_engine.db` gerçek veri içerir.

---

## 7. Yapılandırma (`config.py` / `.env`)

| Anahtar | Varsayılan | Açıklama |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | — | Zorunlu; sadece `.env`'de |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Tahmin modeli |
| `ENABLE_GEMINI_ANALYSIS` | `False` | Genel anahtar; panelde kaydedilen `settings` değeri geçersiz kılar ("Varsayılana Dön" ile `.env`'e döner) |
| `REQUIRE_LEAGUE_APPROVAL` | `True` | Sadece `gemini_analysis = 1` ligler |
| `DEFAULT_LEAGUE_GEMINI_ANALYSIS` | `0` | Yeni liglerin varsayılan izni |
| `MATCH_COMPLETION_DELAY_MINUTES` | `150` | Maç bitti sayılma süresi |
| `MIN_CONFIDENCE_FOR_COUPON` | `70` | Banko kupon güven eşiği |
| `BATCH_REQUEST_DELAY` | `1.5` | Toplu analizde her worker'ın istekler arası beklemesi (sn) |
| `GEMINI_PARALLEL_REQUESTS` | `3` | Toplu analizde eşzamanlı Gemini isteği (artırmak 429 riskini artırır) |
| `MIN_BEST_BET_ODDS` | `1.30` | ⭐ değerli tercih adayı için minimum oran |
| `GEMINI_TIMEOUT_SECONDS` | `120` | Tek Gemini isteği zaman aşımı (bilgisayar uyursa / ağ koparsa sonsuza kadar beklemez) |
| `DB_FILE_PATH` | `betting_engine.db` | Testler bunu geçici dizine yönlendirir |
| `DATA_SOURCE` | `iddaa` | `iddaa` veya `nesine` (Nesine istatistikleri Türkiye dışından engelli) |
| `IDDAA_SPORTSBOOK_URL`, `IDDAA_STATS_URL` | `sportsbookv2` / `statisticsv2.iddaa.com` | iddaa servisleri |
| `NESINE_*_URL` | — | Nesine servisleri |
| `WEB_HOST`, `WEB_PORT` | `127.0.0.1`, `5001` | Dashboard |
| `SOURCE_TIMEZONE`, `DISPLAY_TIMEZONE` | `Europe/Istanbul`, `Europe/Berlin` | Saat dilimleri |

---

## 8. Web API Endpoint'leri (`web_server.py`)

| Metot | Yol | Açıklama |
| :--- | :--- | :--- |
| GET | `/`, `/matches/<id>` | Liste ve maç detay sayfaları |
| GET | `/api/matches/unfinished`, `/api/matches/completed` | Maç listeleri (en son tahminle) |
| GET | `/api/matches/<id>/details` | Maç, oranlar, istatistik, tahminler, sonuç, değerlendirme, ayarlar |
| POST | `/api/matches/<id>/sync_stats` | Tek maç istatistiğini veri kaynağından çek |
| POST | `/api/matches/<id>/result` | Manuel skor girişi |
| POST | `/api/matches/<id>/analyze` | Tek maç Gemini analizi (403 `GEMINI_DISABLED`, 409 `MISSING_STATS` / `LEAGUE_DISABLED`) |
| POST | `/api/admin/sync_bulletin` | Bülteni çek, ardından istatistik işini başlat |
| POST | `/api/admin/sync_results` | Bitmiş maç sonuçlarını çek (futbol + basketbol) |
| POST | `/api/admin/sync_stats` | Toplu istatistik işini başlat |
| GET / POST | `/api/admin/jobs/stats`, `/api/admin/jobs/stats/cancel` | İstatistik işi ilerlemesi / iptal |
| GET / POST | `/api/admin/jobs/predictions`, `.../start`, `.../cancel` | Toplu analiz ilerlemesi (uygun maç sayısıyla) / başlat / iptal |
| GET / POST | `/api/admin/leagues`, `/api/admin/leagues/<id>/toggle`, `/api/admin/leagues/toggle_all` | Lig listesi (⭐ isabet dahil) ve izinleri |
| POST | `/api/admin/leagues/<id>/analyze` | Lig maçlarını analiz et (istatistiği eksikler `skipped`) |
| GET | `/api/stats` | Tahmin doğruluk raporu |
| GET / POST | `/api/admin/settings`, `/api/admin/settings/gemini` | Gemini genel anahtarı |

---

## 9. Gemini Tahmin Prompt'u (Özet)

Gerçek prompt `gemini_engine.py` içindedir (futbol: `build_prompt`, basketbol: `_build_basketball_prompt`); `{...}` alanları
her maç için çalışma anında doldurulur. Prompt değişirse bu özeti de güncelle.

- **Araçlar:** Google Search grounding (`google_search`); araçla başarısız olursa araçsız bir kez daha denenir. `MAX_RETRIES = 3`.
- **Çıktı:** `response_mime_type="application/json"` + `response_schema` (`MatchPrediction` / `BasketballPrediction`).

```text
Sen profesyonel bir futbol veri analisti, kıdemli bahis stratejisti ve yapay zeka tahmin motorusun.
...
--- MAÇ BİLGİLERİ ---                   Spor Türü / Lig / Tarih ve Saat / Ev Sahibi / Deplasman
--- İDDAA BAHİS ORANLARI ---            ← {data["odds"]}: ana marketler önce, en fazla 25 oran
--- VERİTABANI İSTATİSTİKLERİ ---       (her maç tek satır: Tarih | Lig | Ev Skor Dep (İY) [G/B/M])
1. Ev Sahibi Son Maçları (≤10)  2. Deplasman Son Maçları (≤10)  3. H2H (≤10)
4. Lig Puan Durumu (Sıra. Takım | O G B M | Gol | Puan | Son 5)  5. Sakat/Cezalı (iddaa.com'da yok)
--- ÇOK ÖNEMLİ İŞ KURALI (ALT / ÜST SINIRI: 3 GOL) ---
Toplam gol >= 4 ise "ÜST", <= 3 ise "ALT".
--- DEĞERLİ TERCİH (best_bet) ---       ← {data["value"]}: oranı ≥ MIN_BEST_BET_ODDS adaylar
- MS 2 (Maç Sonu) | Oran 2.88 | Piyasa olasılığı %35
...
Kendi olasılığın x oran değeri EN YÜKSEK ve 1'den büyük TEK seçeneği (listedeki yazımıyla) seç, yoksa 'YOK'.
```

**Futbol şeması (`MatchPrediction`):** `analysis_summary`, `predicted_ht_result`, `predicted_ft_result`,
`predicted_over_under_3`, `predicted_kg`, `predicted_score`, `confidence_score`, `best_bet_selection`,
`best_bet_confidence`, `best_bet_reason`.

**Basketbol (`BasketballPrediction`):** rol "basketbol veri analisti"; 3 gol kuralı ve KG yok; `predicted_total_points`
istenir; MS beraberliksiz; değerli tercih adayları sadece MS ve İY.

---

## 10. Dikkat

- `.env` ve `*.db` gitignore'da — `GEMINI_API_KEY`'i asla koda veya dokümana yazma; DB kayıtlarını commit etme.
- Testler `tempfile.gettempdir()` altında `test_betting_engine.db` oluşturur/siler; gerçek DB'ye dokunmaz.
- Arka plan işleri (istatistik, toplu analiz) web sunucusu sürecinde çalışır. Dashboard `debug=True` ile çalışırken bir `.py`
  dosyasını değiştirmek Flask reloader'ı tetikler ve çalışan işi durdurur (tekrar başlatılınca kalan maçlardan devam eder).
- Düzenleme sırasında modül kısa süre bile import edilemez hale gelirse reloader kapanır: çok adımlı düzenlemelerde önce
  bağımlılıkları (import'lar, yardımcı fonksiyonlar) ekle.
- Reloader açılışta `update_expired_matches` çalıştırır ve gerçek DB'ye yazar.
