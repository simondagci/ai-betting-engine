import os
from dotenv import load_dotenv

load_dotenv()

# ==============================================================================
# AI BETTING ENGINE - SİSTEM VE ÇEVRE DEĞİŞKENLERİ (CONFIG & CREDENTIALS)
# ==============================================================================

# 1. Google Gemini API Kimlik Bilgileri ve Model
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

# 2. Genel Gemini API Analiz Anahtarı (Global Switch)
# False ise Gemini API'sine hiçbir otomatik analiz isteği gönderilmez.
ENABLE_GEMINI_ANALYSIS = os.getenv("ENABLE_GEMINI_ANALYSIS", "False").lower() in ("true", "1", "yes")

# 3. Lig İzni Zorunluluğu (League Approval)
# True ise; sadece veritabanında 'gemini_analysis == 1' (True) olan liglerin maçları analiz edilir.
REQUIRE_LEAGUE_APPROVAL = os.getenv("REQUIRE_LEAGUE_APPROVAL", "True").lower() in ("true", "1", "yes")

# 4. Yeni Eklenen Liglerin Varsayılan Analiz İzni (0: Kapalı, 1: Açık)
DEFAULT_LEAGUE_GEMINI_ANALYSIS = int(os.getenv("DEFAULT_LEAGUE_GEMINI_ANALYSIS", "0"))

# 5. Maç Tamamlandı Süre Eşiği (Dakika)
# Maç başlama saatinden 150 dakika (2.5 saat) geçtikten sonra match_completed = 1 kabul edilir.
MATCH_COMPLETION_DELAY_MINUTES = int(os.getenv("MATCH_COMPLETION_DELAY_MINUTES", "150"))

# 6. Banko Kupon İçin Minimum Güven Eşiği (0 - 100)
MIN_CONFIDENCE_FOR_COUPON = int(os.getenv("MIN_CONFIDENCE_FOR_COUPON", "70"))

# 7. Toplu Analizlerde İstekler Arası Bekleme Süresi (Saniye)
BATCH_REQUEST_DELAY = float(os.getenv("BATCH_REQUEST_DELAY", "1.5"))

# Toplu analizde (Maçları Analiz Et) aynı anda Gemini'ye gönderilen maç sayısı.
# Artırmak hızlandırır ama Gemini dakika başı istek limitine (429) takılma riskini artırır.
GEMINI_PARALLEL_REQUESTS = max(1, int(os.getenv("GEMINI_PARALLEL_REQUESTS", "3")))

# Tek bir Gemini isteği için zaman aşımı (saniye). İnternet koparsa / bilgisayar uyursa istek sonsuza kadar beklemez.
GEMINI_TIMEOUT_SECONDS = max(10, int(os.getenv("GEMINI_TIMEOUT_SECONDS", "120")))

# 8. Veritabanı Dosya Yolu
DB_FILE_PATH = os.getenv("DB_FILE_PATH", os.path.join(os.path.dirname(__file__), "betting_engine.db"))

# 9. Veri Kaynağı ve Servis URL Yapılandırmaları
# 'iddaa' (varsayılan): bülten, oran, istatistik ve sonuçlar iddaa.com'dan alınır.
# 'nesine': Nesine servisleri (istatistik servisleri Türkiye dışından erişimi engeller).
DATA_SOURCE = os.getenv("DATA_SOURCE", "iddaa").strip().lower()
IDDAA_SPORTSBOOK_URL = os.getenv("IDDAA_SPORTSBOOK_URL", "https://sportsbookv2.iddaa.com")
IDDAA_STATS_URL = os.getenv("IDDAA_STATS_URL", "https://statisticsv2.iddaa.com")
NESINE_BULLETIN_URL = os.getenv("NESINE_BULLETIN_URL", "https://bulten.nesine.com/api/bulten/getprebultenfull")
NESINE_STATS_BASE_URL = os.getenv("NESINE_STATS_BASE_URL", "https://istatistik.nesine.com/api/v2")
NESINE_LIVESCORE_URL = os.getenv("NESINE_LIVESCORE_URL", "https://ls.nesine.com/api/v1/LiveScore/GetIddaaResultObjectList")

# 10. Web Sunucusu Yapılandırması (Localhost Dashboard)
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "5001"))

# 11. Saat Dilimi Yapılandırması
# Nesine bülten kaynak saati: Türkiye (UTC+3)
SOURCE_TIMEZONE = os.getenv("SOURCE_TIMEZONE", "Europe/Istanbul")
# Gösterilecek yerel saat: Almanya / Berlin (CET / CEST)
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "Europe/Berlin")

