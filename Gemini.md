# Project Instructions & Business Rules

## 1. Uygulama Amacı (Overview)
Bu uygulama, yapay zekanın (Google Gemini API) futbol maç istatistiklerini ve güncel verileri analiz ederek kişisel kullanım amaçlı maç tahminleri ve kuponlar oluşturan bir sistemdir.

---

## 2. İş Akışı ve Yaşam Döngüsü (Workflow)

> **Veri kaynağı:** Varsayılan kaynak iddaa.com'dur (`mdFiles/APIs/iddaa/README.md`). Nesine istatistik servisleri Türkiye dışından erişimi engellediği için Nesine API'leri (`mdFiles/APIs/0X_*.md`) sadece `DATA_SOURCE=nesine` ile kullanılır.

1. **Bülten Okuma:** Bültendeki güncel maçlar ve oranlar tek seferde çekilir (iddaa.com `sportsbook/events`; Nesine karşılığı `mdFiles/APIs/01_bulten_api.md`).
2. **Detay Verilerini Toplama:** Bülten maçlarındaki takım ID'leri kullanılarak:
   - Takımların son oynadığı maçlar (`mdFiles/APIs/02_last_matches_api.md`)
   - Takımların aralarındaki H2H maçlar (`mdFiles/APIs/03_competition_history_api.md`)
   - Sakat/cezalı oyuncu bilgileri (sadece Nesine: `mdFiles/APIs/04_missing_players_api.md`; iddaa.com'da yok)
   - **Not:** Maç detayları ve son maçlar yalnızca veri kaynağından alınır; Gemini bu veriler için kullanılmaz, sadece 4. adımdaki analiz için çağrılır.
   - Lig puan durumu (`mdFiles/APIs/05_league_table_api.md`) çekilir.
   - Bülten çekildikten sonra istatistiği olmayan tüm bitmemiş maçlar için arka planda otomatik çekilir (ilerleme çubuğuyla; maç başına 4 paralel istek, ~0,6 sn/maç).
3. **Veritabanına Kayıt:** Toplanan bülten, istatistik ve oran verileri SQLite veritabanına eklenir.
   - **En güçlü tercih (⭐):** Gemini kendi tahminleri arasından en çok güvendiği tek marketi seçer (futbol: MS / İY / ALT-ÜST / KG, basketbol: MS / İY), güven yüzdesi ve gerekçesiyle. Seçim değeri ilgili tahminden alınır; `predictions.best_bet_*` sütunlarına yazılır. Sonuç girilince tuttu/tutmadı değerlendirilir; Lig Yönetimi tablosunda lig bazında ⭐ isabet oranı (maç başına son tahmin) gösterilir.
4. **Gönderim Kuralı:** Ev sahibinin ve deplasmanın son maçları ile aralarındaki maçlar (H2H) yoksa maç Gemini'ye **gönderilmez** (göndermeden önce veri kaynağından bir kez daha denenir).
   - **Toplu analiz:** "🔮 Maçları Analiz Et" butonu bu kurala uyan tüm bitmemiş ve daha önce analiz edilmemiş maçları (ligi izinli) arka planda sırayla Gemini'ye gönderir; ilerleme sayfanın üstündedir, Durdur ile kesilebilir.
5. **Gemini AI Tahminleme:** Derlenen veriler ve güncel internet bilgileri Gemini API'sine (Google Search Grounding aktif şekilde) gönderilir ve yapılandırılmış (Structured JSON) tahmin çıktısı alınır.
6. **Sonuç ve Başarı Oranı Takibi:** Tamamlanan maçların skorları Lig Yönetimi › "Sonuçları Çek" ile iddaa.com `live-results` servisinden çekilip SQLite'a yazılır (sadece normal süresinde biten maçlar) ve yapay zeka tahminlerinin başarı yüzdesi otomatik hesaplanır.

---

## 3. İş Kuralları (Business Rules)

* **Alt / Üst Sınırı (3 Gol):**
  - **ÜST:** Toplam gol sayısı **3'ten fazla** (Gol sayısı ≥ 4) ise **ÜST** kabul edilir.
  - **ALT:** Toplam gol sayısı **3 veya daha az** (Gol sayısı ≤ 3) ise **ALT** kabul edilir.
* **Basketbol Kuralları:**
  - Maç sonucu uzatmalar dahildir, beraberlik yoktur (MS 1 / MS 2). İlk yarı sonucu İY 1 / İY 0 / İY 2.
  - Basketbolda **Karşılıklı Gol ve Alt/Üst yoktur**; bunun yerine Gemini **Maç Sonu Toplam Sayı** (`predicted_total_points`, uzatmalar dahil) tahmini yapar. Toplam sayı, tahmini skordaki iki sayının toplamına eşitlenir.
  - Skor girişinde üst sınır yoktur (futbolda 0–99). Basketbol sonuçlarına 3 gol / KG yazılmaz.
  - Başarı raporunda basketbol için tahmini ile gerçek toplam sayı arasındaki ortalama fark gösterilir.
* **Takip Edilen Maç Detayları:**
  - İlk Yarı Skoru (HT)
  - Maç Sonu Skoru (FT)
  - Karşılıklı Gol Durumu (KG Var / KG Yok)
  - Alt / Üst Durumu (3 Alt / 3 Üst)

---

## 4. Veritabanı Mimarisi (SQLite Schema)

* **`teams`**: `id`, `name`, `sport_type`
* **`leagues`**: `id`, `name`, `sport_type`, `gemini_analysis` (0: Kapalı, 1: Açık)
* **`matches`**: `id`, `code`, `home_team_id`, `away_team_id`, `league_id`, `match_date`, `match_time`, `status`, `match_completed` (0: Gelecek/Oynanmamış, 1: Tamamlandı/Tarihi Geçti)
* **`odds`**: `id`, `match_id`, `market_type`, `option_name`, `odds_value`
* **`results`**: `id`, `match_id`, `ht_home_score`, `ht_away_score`, `ft_home_score`, `ft_away_score`, `is_over_3_goals`, `is_kg_var`
* **`predictions`**: `id`, `match_id`, `predicted_ht_result`, `predicted_ft_result`, `predicted_over_under_3`, `predicted_kg`, `predicted_score`, `confidence_score`, `ai_analysis_summary`, `predicted_total_points`, `best_bet_market`, `best_bet_selection`, `best_bet_confidence`, `best_bet_reason`, `created_at`

### Bülten ve Maç İşleme Kuralları:
- **Toplu İşlem Mimarisi (Zero SQL in Loop):** Bülten senkronizasyonu döngü içi tekil SQL sorguları yerine, hafıza içi önbellekleme (in-memory caching) ve `executemany` toplu eklemeleri ile yüksek performansla çalışır.
- **Mükerrer Kayıt Önleme:** Uygulama tekrar çalıştırıldığında veritabanında zaten kayıtlı olan maç kodları atlanır, gereksiz işlem ve istek yapılmaz.
- **Tarihi Geçen Maçlar (150 Dakika Kuralı):** Maçın oynanma tarihi ve saatinden **150 dakika (2.5 saat)** geçtikten sonra karşılaşma `match_completed = 1` (True) ve `status = 'COMPLETED'` olarak otomatik işaretlenir. 150 dakika dolmadığı sürece maç tamamlanmamış (`match_completed = 0`) kabul edilir.

---

## 5. Yapılandırma ve Parametre Dosyası (`config.py`)

* **`ENABLE_GEMINI_ANALYSIS`**: Gemini API'sine istek atılıp atılmayacağını belirleyen genel anahtar (Varsayılan: `False`). Web arayüzündeki **Lig Yönetimi › Ayarlar** sayfasından değiştirilebilir; panelden kaydedilen değer (`settings` tablosu) `.env` değerini geçersiz kılar ve "Varsayılana Dön" ile tekrar `.env` değerine dönülür.
* **`REQUIRE_LEAGUE_APPROVAL`**: Yalnızca `gemini_analysis == 1` olan liglerin analiz edilmesini sağlayan kural (Varsayılan: `True`).
* **`DEFAULT_LEAGUE_GEMINI_ANALYSIS`**: Yeni eklenen liglerin varsayılan analiz durumu (`0`: Kapalı).
* **`MATCH_COMPLETION_DELAY_MINUTES`**: Maçın tamamlandı kabul edilmesi için başlama saatinden sonra geçmesi gereken süre (`150` dakika).
* **`GEMINI_MODEL`**: Kullanılan model (`gemini-3.8-flash`).

---

## 6. Gemini AI Prompt ve Sistem Şablonu

Gemini API'ye gönderilecek prompt şablonu ve beklenen JSON şeması:

> Basketbol maçları için ayrı prompt ve şema kullanılır (`gemini_engine._build_basketball_prompt`, `BasketballPrediction`): 3 gol kuralı ve KG yoktur, `predicted_total_points` (Maç Sonu Toplam Sayı) istenir, ⭐ en güçlü tercih sadece İY / MS olabilir.

### System Prompt & Grounding:
* **Model:** `gemini-3.8-flash`
* **Araçlar:** Google Search Grounding (`google_search`) aktif kılınarak güncel sakatlık ve haberler canlı taranacaktır.
* **Çıktı Formatı:** `response_mime_type="application/json"` ile kesin JSON yanıtı.

### Prompt Şablonu:
```text
Sen profesyonel bir futbol veri analisti ve bahis stratejistisin. Aşağıda verilen istatistikleri ve güncel internet yorumlarını analiz ederek {ev_sahibi_takim} ile {deplasman_takim} maçı için ilk yarı ve maç sonu tahmini yap. 

Not: Alt/Üst analizlerinde temel sınırımız 3 goldür (Toplam gol 3'ten fazla ise ÜST, aksi takdirde ALT kabul edilir). 

--- MAÇ VERİLERİ --- 
(Her maç tek satır: Tarih | Lig | Ev Sahibi Skor Deplasman (İY skoru) [G/B/M]; takım başına son 10 maç)
1. Ev Sahibi Takım Son Maçları: {ev_sahibi_son_maclar} 
2. Deplasman Takımı Son Maçları: {deplasman_son_maclar} 
3. Aralarındaki Maçlar (H2H): {birbirleriyle_oynadigi_maclar} 
4. Lig Puan Durumu: {puan_durumu}  (Sıra. Takım | O G B M | Gol | Puan | Son 5)
5. Sakat / Cezalı Oyuncular: {sakat_cezali_oyuncular}
6. Güncel Haberler ve İnternet Yorumları: (Bu maçla ilgili güncel sakatlıklar, form durumları ve piyasa beklentilerini değerlendir) 
İddaa oranları: ana marketler (MS, İY, KG, Çifte Şans, Alt/Üst) önce olmak üzere en fazla 25 oran.


Aşağıdaki JSON formatında yanıt ver:
{
  "analysis_summary": "3-4 cümlelik özet analiz",
  "predicted_ht_result": "İY 1 / İY 0 / İY 2",
  "predicted_ft_result": "MS 1 / MS 0 / MS 2",
  "predicted_over_under_3": "ÜST / ALT",
  "predicted_kg": "KG VAR / KG YOK",
  "predicted_score": "2-1",
  "confidence_score": 85,
  "best_bet_market": "MS",
  "best_bet_confidence": 80,
  "best_bet_reason": "1 cümlelik gerekçe"
}
```
