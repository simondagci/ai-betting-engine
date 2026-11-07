import json
import time
from typing import Dict, Any, List, Optional, Type
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from google.genai.errors import ServerError
import config
import rules

DEFAULT_MODEL = config.GEMINI_MODEL
MAX_RETRIES = 3

# Prompt'a giden veri sınırları (ham JSON yerine kompakt satırlar gönderildiği için kayıtlar yarıda kesilmez)
MAX_HISTORY_ROWS = 10
MAX_TABLE_ROWS = 30
MAX_ODDS_ROWS = 40
# Oran listesinde önce bu marketler gönderilir
PRIORITY_MARKETS = ("Maç Sonucu", "İlk Yarı Sonucu", "Karşılıklı Gol", "Çifte Şans",
                    "2.5 Gol Alt/Üst", "3.5 Gol Alt/Üst", "1.5 Gol Alt/Üst",
                    rules.TOTAL_POINTS_MARKET)

# ==============================================================================
# YANIT ŞEMALARI
# ==============================================================================

class MatchPrediction(BaseModel):
    """Futbol tahmini."""
    analysis_summary: str = Field(description="3-4 cümlelik Türkçe detaylı taktiksel analiz ve değerlendirme")
    predicted_ht_result: str = Field(description="İlk Yarı Tahmini: İY 1, İY 0 veya İY 2")
    predicted_ht_score: str = Field(description="Tahmini İLK YARI skoru (Örn: '1-0', '0-0', '1-1')")
    predicted_ft_result: str = Field(description="Maç Sonu Tahmini: MS 1, MS 0 veya MS 2")
    predicted_over_under_3: str = Field(description="3 Gol Sınırına Göre Alt/Üst Tahmini: Toplam gol >= 4 ise 'ÜST', toplam gol <= 3 ise 'ALT'")
    predicted_kg: str = Field(description="Karşılıklı Gol Tahmini: 'KG VAR' veya 'KG YOK'")
    predicted_score: str = Field(description="Tahmini maç sonu skoru (Örn: '2-1', '1-1', '3-0')")
    confidence_score: int = Field(description="Tahminin gerçekleşme olasılığına dair 1 ile 100 arasında integer güven skoru")
    # Zorunlu alanlar: şemada 'required' olarak işaretlenir, böylece Gemini her yanıtta doldurur
    best_bet_selection: str = Field(description="EN GÜÇLÜ TERCİH: listedeki (Çifte Şans hariç) gerçekleşeceğine EN ÇOK güvendiğin seçenek, AYNEN yazımıyla (örn. 'MS 1', 'KG VAR', 'ALT')")
    best_bet_confidence: int = Field(description="En güçlü tercihin gerçekleşme olasılığı (1-100)")
    best_bet_reason: str = Field(description="En güçlü tercihin 1 cümlelik Türkçe gerekçesi")
    alt_bet_selection: str = Field(description="2. TERCİH: en güçlü tercihin oranı eşiğin altındaysa, oranı eşik ve üstü seçenekler arasından EN ÇOK güvendiğin seçenek (AYNEN yazımıyla); gerek yoksa veya uygun seçenek yoksa 'YOK'")
    alt_bet_confidence: int = Field(description="2. tercihin gerçekleşme olasılığı (1-100); 'YOK' ise 0")

class BasketballPrediction(BaseModel):
    """Basketbol tahmini: beraberlik ve karşılıklı gol yoktur; maç sonu toplam sayı ve Alt/Üst tahmin edilir."""
    analysis_summary: str = Field(description="3-4 cümlelik Türkçe detaylı taktiksel analiz ve değerlendirme")
    predicted_ht_result: str = Field(description="İlk Yarı Tahmini (beraberlik yok): İY 1 veya İY 2")
    predicted_ht_score: str = Field(description="Tahmini İLK YARI skoru (Örn: '42-38', '48-44')")
    predicted_ft_result: str = Field(description="Maç Sonu Tahmini (uzatmalar dahil, beraberlik yok): MS 1 veya MS 2")
    predicted_score: str = Field(description="Tahmini maç skoru, uzatmalar dahil (Örn: '112-105')")
    predicted_total_points: int = Field(description="Maç Sonu Toplam Sayı tahmini: iki takımın sayılarının toplamı, uzatmalar dahil (Örn: 217)")
    confidence_score: int = Field(description="Tahminin gerçekleşme olasılığına dair 1 ile 100 arasında integer güven skoru")
    best_bet_selection: str = Field(description="EN GÜÇLÜ TERCİH: listedeki (Çifte Şans hariç) gerçekleşeceğine EN ÇOK güvendiğin seçenek, AYNEN yazımıyla (örn. 'MS 1', 'KG VAR', 'ALT')")
    best_bet_confidence: int = Field(description="En güçlü tercihin gerçekleşme olasılığı (1-100)")
    best_bet_reason: str = Field(description="En güçlü tercihin 1 cümlelik Türkçe gerekçesi")
    alt_bet_selection: str = Field(description="2. TERCİH: en güçlü tercihin oranı eşiğin altındaysa, oranı eşik ve üstü seçenekler arasından EN ÇOK güvendiğin seçenek (AYNEN yazımıyla); gerek yoksa veya uygun seçenek yoksa 'YOK'")
    alt_bet_confidence: int = Field(description="2. tercihin gerçekleşme olasılığı (1-100); 'YOK' ise 0")

def get_gemini_client() -> genai.Client:
    if not config.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY ortam değişkeni bulunamadı. Lütfen .env dosyasını kontrol edin.")
    # Zaman aşımı olmadan bağlantı koptuğunda istek sonsuza kadar bekleyip toplu analizi kilitleyebilir
    return genai.Client(api_key=config.GEMINI_API_KEY,
                        http_options=types.HttpOptions(timeout=config.GEMINI_TIMEOUT_SECONDS * 1000))

# ==============================================================================
# PROMPT
# ==============================================================================

def _json_or_placeholder(value: Any, placeholder: str) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) if value else placeholder

def _format_history(items: Any, placeholder: str) -> str:
    """
    Son maç / H2H listesini tek satırlık kayıtlara çevirir: '- 2026-10-25 | 1. Lig | Batman 3-2 Esenler (İY 0-0) [M]'.
    Uygulama formatında olmayan (eski / bilinmeyen) veriler kısaltılmış JSON olarak gönderilir.
    """
    if not items:
        return placeholder
    if not isinstance(items, list) or not all(isinstance(i, dict) and "home" in i for i in items):
        return _json_or_placeholder(items, placeholder)[:1500]

    lines = []
    for item in items[:MAX_HISTORY_ROWS]:
        line = f"- {item.get('date', '')} | {item.get('league', '')} | {item.get('home', '')} {item.get('score') or 'vs'} {item.get('away', '')}"
        if item.get("ht_score"):
            line += f" (İY {item['ht_score']})"
        if item.get("result"):
            line += f" [{item['result']}]"
        lines.append(line)
    return "\n".join(lines)

def _format_league_table(table: Any, score_label: str = "Gol") -> str:
    rows = (table or {}).get("LeagueTable") if isinstance(table, dict) else None
    if not rows:
        return "Veritabanında puan durumu verisi yok."
    lines = []
    for r in rows[:MAX_TABLE_ROWS]:
        line = f"{r.get('Rank')}. {r.get('TeamName')} | O:{r.get('Played')} G:{r.get('Won')}"
        if r.get("Draw") is not None:
            line += f" B:{r.get('Draw')}"
        line += f" M:{r.get('Lost')}"
        if r.get("GoalsFor") is not None:
            line += f" | {score_label} {r.get('GoalsFor')}:{r.get('GoalsAgainst')}"
        line += f" | Puan {r.get('Points')}"
        if r.get("Form"):
            line += f" | Son 5: {' '.join(r['Form'])}"
        lines.append(line)
    return "\n".join(lines)

def _format_odds(odds: List[Dict[str, Any]]) -> str:
    if not odds:
        return "Açılan oran bilgisi bulunamadı."
    priority = {name: i for i, name in enumerate(PRIORITY_MARKETS)}
    ordered = sorted(odds, key=lambda o: priority.get(o.get("market_name"), len(priority)))
    return "\n".join(
        f"- {o.get('market_name', 'Bahis')}: {o.get('option_name')} -> Oran: {o.get('odds_value')}"
        for o in ordered[:MAX_ODDS_ROWS]
    )

def _sport_type(match_context: Dict[str, Any]) -> str:
    return match_context.get("match", {}).get("sport_type", rules.FOOTBALL)

def _total_points_line(match_context: Dict[str, Any]) -> Optional[float]:
    """Basketbol Alt/Üst sınırı: maç kaydından, yoksa maçın oranlarından."""
    line = match_context.get("match", {}).get("total_points_line")
    return line if line is not None else rules.main_total_points_line(match_context.get("odds", []))

def _best_bet_candidates(match_context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """⭐ en güçlü tercih adayları: Gemini'nin kendi tahmin marketleri (MS, İY, 3 gol ALT/ÜST, KG); oranı MIN_BEST_BET_ODDS ve üstü olmalı."""
    return rules.bet_candidates(match_context.get("odds", []), _sport_type(match_context), config.MIN_BEST_BET_ODDS,
                                markets=rules.BEST_BET_MARKETS)

def _alt_bet_candidates(match_context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """2. tercih adayları: oranı MIN_BEST_BET_ODDS ve üstü tüm seçenekler (Çifte Şans dahil)."""
    return rules.bet_candidates(match_context.get("odds", []), _sport_type(match_context), config.MIN_BEST_BET_ODDS)

def _format_best_bet_section(match_context: Dict[str, Any]) -> str:
    """
    ⭐ en güçlü tercih bölümü. Seçenekler oran ve piyasa olasılığıyla verilir; yalnızca oranı MIN_BEST_BET_ODDS ve üstü
    olanlar aday olur (1.10'luk favori ⭐ olamaz). Bu yüzden 2. tercih (alt_bet) gerekmez: her zaman 'YOK' / 0.
    """
    min_odds = config.MIN_BEST_BET_ODDS
    candidates = _best_bet_candidates(match_context)
    header = "--- EN GÜÇLÜ TERCİH (best_bet) VE 2. TERCİH (alt_bet) ---"
    if not candidates:
        return (f"{header}\nBu maçta oranı {min_odds:.2f} ve üstü uygun seçenek yok: best_bet_selection için 'YOK', "
                "best_bet_confidence için 0 ver; alt_bet_selection 'YOK', alt_bet_confidence 0 olsun.")
    lines = [f"- {c['selection']} ({c['market_label']}) | Oran {c['odds']:.2f} | Piyasa olasılığı %{100 / c['odds']:.0f}"
             for c in candidates]
    return "\n".join([
        header,
        f"Seçenekler (yalnızca oranı {min_odds:.2f} ve üstü olanlar; oran | oranın ima ettiği piyasa olasılığı):",
        *lines,
        "1) best_bet: yukarıdaki seçenekler arasından gerçekleşeceğine EN ÇOK güvendiğin TEK seçeneği yaz "
        "(listedeki yazımıyla AYNEN, örn. 'MS 1', 'ALT'), gerçekleşme olasılığını (best_bet_confidence, 1-100) ve 1 cümlelik gerekçesini (best_bet_reason). "
        f"Oranı {min_odds:.2f} altındaki seçenekler listede yoktur ve seçilemez.",
        "2) alt_bet: kullanılmıyor; alt_bet_selection her zaman 'YOK', alt_bet_confidence 0 olsun.",
        "Olasılıkları verilere dayanarak GERÇEKÇİ tahmin et, abartma.",
    ])

def _data_sections(match_context: Dict[str, Any], score_label: str) -> Dict[str, str]:
    """Maç geçmişi sadece veri kaynağından (iddaa.com / Nesine) gelir; Gemini'den bu verileri araması istenmez."""
    no_last_matches = "Veritabanında son maç verisi yok."
    return {
        "best_bet": _format_best_bet_section(match_context),
        "odds": _format_odds(match_context.get("odds", [])),
        "h2h": _format_history(match_context.get("h2h"), "Veritabanında ikili mücadele verisi yok."),
        "home_last": _format_history(match_context.get("home_last"), no_last_matches),
        "away_last": _format_history(match_context.get("away_last"), no_last_matches),
        "table": _format_league_table(match_context.get("league_table"), score_label),
        "missing": _json_or_placeholder(match_context.get("missing"), "Yerel sakat/cezalı verisi yok (İnternetten kontrol ediniz).")[:800],
    }

def _format_market_prior(match_context: Dict[str, Any]) -> str:
    """
    Bahis şirketi marjından arındırılmış (normalize) piyasa olasılıkları. Piyasa iyi kalibre bir başlangıç noktasıdır:
    Gemini bu olasılıklardan yola çıkar ve yalnızca somut kanıtla sapar.
    Yalnızca eksiksiz marketler (MS 3 seçenek, İY 3, KG 2, Alt/Üst 2; basketbolda MS 2 ve ana Toplam Sayı baremi) gösterilir.
    """
    sport = _sport_type(match_context)
    odds = match_context.get("odds", [])
    basketball = rules.is_basketball(sport)
    wanted = {"ft": 2 if basketball else 3} if basketball else {"ft": 3, "ht": 3, "over_under": 2, "kg": 2}
    by_market: Dict[str, List[Dict[str, Any]]] = {}
    for c in rules.bet_candidates(odds, sport, markets=tuple(wanted) + (("total_points",) if basketball else ())):
        by_market.setdefault(c["market"], []).append(c)

    lines = []
    for market, group in by_market.items():
        if market == "total_points":
            line = _total_points_line(match_context)
            group = [c for c in group if line is not None and rules.parse_total_points_option(c["selection"])
                     and rules.parse_total_points_option(c["selection"])[0] == line]
            size = 2
        else:
            size = wanted[market]
        if len(group) != size:
            continue
        raw = [1.0 / c["odds"] for c in group]
        total = sum(raw)
        parts = " | ".join(f"{c['selection']} %{100 * r / total:.0f}" for c, r in zip(group, raw))
        lines.append(f"- {group[0]['market_label']}: {parts} (marj: %{100 * (total - 1):.1f})")
    return "\n".join(lines) if lines else "Eksiksiz market bulunamadı; piyasa olasılığı hesaplanamadı."

def _build_basketball_prompt(match_context: Dict[str, Any]) -> str:
    match_info = match_context.get("match", {})
    home_team = match_info.get("home_team", "Ev Sahibi")
    away_team = match_info.get("away_team", "Deplasman")
    data = _data_sections(match_context, score_label="Sayı")
    line = _total_points_line(match_context)

    line_rule = (
        f"Resmi Toplam Sayı baremi: {line:g}. Toplam sayı bu baremin ÜZERİNDEYSE ÜST, eşit veya altındaysa ALT sayılır. "
        "predicted_score toplamı ile predicted_total_points aynı olmalıdır."
        if line is not None else
        "Toplam sayı baremi bulunamadı; toplam sayıyı yine tahmin et ama bareme veya orana dair bir şey uydurma."
    )

    return f"""
ROLÜN:
Sen profesyonel bir basketbol veri analisti ve olasılık modelleyicisisin. Amacın favoriyi veya popüler takımı söylemek değil,
sonuçların GERÇEK olasılıklarını iyi kalibre etmektir. Başarın, söylediğin %70'lerin gerçekten yaklaşık %70 tutmasıyla ölçülür.

==================================================
1. MAÇ VE PİYASA
==================================================

Lig: {match_info.get("league_name", "Bilinmeyen Lig")}
Tarih: {match_info.get("match_date", "")} {match_info.get("match_time", "")}
Ev sahibi: {home_team}
Deplasman: {away_team}

Bülten oranları:
{data["odds"]}

PİYASA OLASILIKLARI (marj arındırılmış, başlangıç noktan):
{_format_market_prior(match_context)}

==================================================
2. VERİTABANI İSTATİSTİKLERİ
==================================================

1. Ev Sahibi Son Maçları:
{data["home_last"]}

2. Deplasman Son Maçları:
{data["away_last"]}

İKİ TAKIMIN H2H GEÇMİŞİ:
{data["h2h"]}

LİG PUAN DURUMU:
{data["table"]}

SAKAT VE CEZALI OYUNCULAR:
{data["missing"]}

==================================================
3. YÖNTEM (SIRAYLA UYGULA)
==================================================

1) Başlangıç: normalize piyasa olasılıklarını kabul et. Piyasa, bilgiyi çok iyi fiyatlar; düşük oranlı favori çoğu zaman gerçekten favoridir.
   Favoriyi yalnızca somut kanıt varsa (aşağıdakiler) zayıflat; "sürpriz olabilir" gibi genel gerekçeyle piyasadan sapma.
2) Veriden güç farkı çıkar: son 10 maçta atılan/yenen sayı ortalaması ve fark, iç saha / deplasman ayrımı, puan durumundaki
   konum ve net sayı farkı. Rakip kalitesini ve maç sırasını (en yeni maç en önemli) dikkate al.
3) Google Search'ü YALNIZCA şunlar için kullan: onaylı sakatlık / dinlenme (özellikle ana oyun kurucu, skorer, pivot),
   back-to-back veya yoğun seyahat, somut motivasyon durumu (eleme maçı, sıralama kaygısı). İleri istatistik (ORtg, pace vb.)
   veya hakem verisi arayıp bulamamak tahmini değiştirmez; bulamadığını yok say, uydurma.
4) Düzeltme: kanıt başına küçük, toplamda piyasadan en fazla ~10 puan sap. Doğrulanmamış haberle sapma.
5) Toplam sayı: iki takımın beklenen sayısını ayrı düşün (kendi ortalaması ile rakibin yediği sayının karışımı, tempo farkı).
   Tek başına son maç ortalamasına bakma; barem piyasanın en iyi toplam sayı tahminidir, baremden büyük sapma için güçlü kanıt gerekir.

==================================================
4. TUTARLILIK KURALLARI
==================================================

- Basketbolda MS ve İY'de beraberlik YOKTUR: predicted_ft_result yalnızca "MS 1" / "MS 2", predicted_ht_result yalnızca "İY 1" / "İY 2".
- predicted_ft_result, kendi olasılığın en yüksek olan takımdır; predicted_score ile aynı yönü göstermelidir.
- Skor uzatmalar dahil resmi skordur. İlk yarı skoru, ilk yarı sonucuyla tutarlı olmalı.
- Basketbolda KG yoktur.
- MAÇ SONU TOPLAM SAYI (predicted_total_points): iki takımın uzatmalar dahil toplam sayısı; predicted_score ile aynı olmalı.
- {line_rule}
- Tahmin edilen toplam sayı bareme çok yakınsa Alt/Üst güvenini 50'ye yaklaştır.

==================================================
5. GÜVEN PUANI = GERÇEKLEŞME OLASILIĞI
==================================================

confidence_score ve best_bet_confidence, söylediğin seçimin gerçekleşme olasılığıdır (%). Puanı süslemek için şişirme.
- Çok dengeli maç: 50-58. Belirgin favori: 62-75. Piyasa %80+ veriyorsa ve kanıt çelişmiyorsa 78-88 verebilirsin.
- Kesin görünen maçlarda bile %92 üstünü kullanma.
- Veri eksikse, kilit oyuncu belirsizse veya kaynak bulamadıysan puanı düşür.
- Güveni takım ismine, şampiyonluk geçmişine veya orana göre değil, hesapladığın olasılığa göre ver.

==================================================
6. EN GÜÇLÜ TERCİH VE İKİNCİ TERCİH
==================================================

{data["best_bet"]}

- best_bet, listedeki seçenekler (Çifte Şans hariç) arasından GERÇEKLEŞME OLASILIĞI EN YÜKSEK olandır.
  Liste zaten oranı düşük seçenekleri içermez; yüksek oran bir avantaj değildir, yalnızca olasılığa bak.
- Yazımı listedeki gibi AYNEN kullan; listede olmayan market veya oran üretme.
- alt_bet kullanılmıyor: her zaman 'YOK'.

==================================================
7. ÇIKTI
==================================================

Yanıtı vermeden önce kontrol et: skor toplamı = predicted_total_points, MS yönü skorla uyumlu, İY tutarlı, seçimler listede var.
SADECE Pydantic şemasına uygun JSON döndür; markdown veya açıklama ekleme.
analysis_summary: 3-4 cümlelik Türkçe analiz; piyasadan saptıysan nedenini ve kullandığın somut kanıtı yaz.
Bulamadığın bilgiyi uydurma; belirsizliği güven puanına yansıt.
"""

def _build_football_prompt(match_context: Dict[str, Any]) -> str:
    match_info = match_context.get("match", {})
    home_team = match_info.get("home_team", "Ev Sahibi")
    away_team = match_info.get("away_team", "Deplasman")
    data = _data_sections(match_context, score_label="Gol")

    return f"""
ROLÜN:
Sen kıdemli bir futbol veri bilimcisi ve olasılık modelleyicisisin. Amacın favoriyi veya popüler takımı söylemek değil,
sonuçların GERÇEK olasılıklarını iyi kalibre etmektir. Başarın, söylediğin %60'ların gerçekten yaklaşık %60 tutmasıyla ölçülür.

==================================================
1. MAÇ VE PİYASA
==================================================

Lig: {match_info.get("league_name", "Bilinmeyen Lig")}
Tarih: {match_info.get("match_date", "")} {match_info.get("match_time", "")}
Ev sahibi: {home_team}
Deplasman: {away_team}

İddaa oranları:
{data["odds"]}

PİYASA OLASILIKLARI (marj arındırılmış, başlangıç noktan):
{_format_market_prior(match_context)}

==================================================
2. VERİTABANI İSTATİSTİKLERİ
==================================================

1. Ev Sahibi Son Maçları:
{data["home_last"]}

2. Deplasman Son Maçları:
{data["away_last"]}

İKİ TAKIMIN H2H GEÇMİŞİ:
{data["h2h"]}

LİG PUAN DURUMU:
{data["table"]}

SAKAT VE CEZALI OYUNCULAR:
{data["missing"]}

==================================================
3. YÖNTEM (SIRAYLA UYGULA)
==================================================

1) Başlangıç: normalize piyasa olasılıklarını kabul et. Piyasa, bilgiyi çok iyi fiyatlar; 1.20-1.40 oranlı favori çoğu zaman
   gerçekten kazanır. Favoriyi yalnızca somut kanıt varsa zayıflat; "sürpriz olabilir" gibi genel gerekçeyle piyasadan sapma.
2) Veriden güç farkı çıkar: son 10 maçta atılan/yenen gol, iç saha ve deplasman ayrımı, puan durumundaki konum ve averaj.
   Rakip kalitesini ve maç sırasını (en yeni maç en önemli) dikkate al. H2H ve eski teknik direktör dönemi maçları düşük ağırlıklıdır.
3) Google Search'ü YALNIZCA şunlar için kullan: onaylı sakatlık / ceza / muhtemel 11 (kaleci, stoper, ana golcü, oyun kurucu),
   teknik direktör değişikliği, yoğun fikstür (son 4 günde maç, kupa yorgunluğu), kümede kalma / şampiyonluk gibi somut motivasyon.
   xG, hakem, hava durumu gibi verileri arayıp bulamamak tahmini değiştirmez; bulamadığını yok say, uydurma.
4) Düzeltme: kanıt başına küçük, toplamda piyasadan en fazla ~10 puan sap. Doğrulanmamış haberle sapma.
5) Gol modeli: iki takımın beklenen golünü ayrı hesapla (kendi atma ortalaması ile rakibin yeme ortalamasının karışımı,
   iç saha avantajı), Poisson mantığıyla skor dağılımını düşün. En olası skor ile en olası sonuç aynı şey değildir.

==================================================
4. TUTARLILIK KURALLARI
==================================================

- predicted_ft_result, DÜZELTİLMİŞ olasılığı en yüksek olan sonuçtur. Beraberlik (MS 0) ancak gerçekten en yüksek olasılıktaysa
  seçilir; futbolda MS 0 genelde %22-30 aralığındadır, dengeli maçta bile 1 veya 2'nin gerisinde kalabilir.
- İlk yarı bağımsız modellenir: İY 0 (beraberlik) futbolda tipik olarak %40-50'dir ve çoğu maçta en olası İY sonucudur.
  İY 1/2'yi yalnızca bir taraf ilk yarıda belirgin üstünse seç.
- predicted_score, predicted_ft_result ile; predicted_ht_score, predicted_ht_result ile tutarlı olmalı.
- 3 GOL KURALI KESİNLİKLE UYGULANACAK (KESİNLİKLE 3 GOL KURALI): toplam gol 0-3 ise ALT, 4 ve üstü ise ÜST.
  (2-1, 3-0, 1-2 → ALT; 2-2, 3-1, 4-0 → ÜST.) predicted_over_under_3 bu kurala ve skora uygun olmalı.
  3.5 baremi yüksektir: ÜST için güçlü bir gol beklentisi (iki takım toplamı ~3.3+) gerekir.
- KG için iki takımın da gol atma olasılığını ayrı hesapla; yüksek toplam gol tek başına KG VAR değildir. KG ile skor tutarlı olmalı.

==================================================
5. GÜVEN PUANI = GERÇEKLEŞME OLASILIĞI
==================================================

confidence_score ve best_bet_confidence, söylediğin seçimin gerçekleşme olasılığıdır (%). Puanı süslemek için şişirme.
- MS gibi 3 sonuçlu marketlerde en olası sonuç bile nadiren %65'i aşar; belirgin favoride 55-70, dengeli maçta 38-48.
- İki sonuçlu marketlerde (KG, Alt/Üst) belirsizlik yüksektir: çoğu zaman 52-68.
- Piyasa %80+ veriyorsa ve kanıt çelişmiyorsa 78-88 verebilirsin; %92 üstünü kullanma.
- Veri eksikse, kilit oyuncu belirsizse veya kaynak bulamadıysan puanı düşür.
- Güveni takım ismine, popülerliğe veya orana göre değil, hesapladığın olasılığa göre ver.

==================================================
6. EN GÜÇLÜ TERCİH VE İKİNCİ TERCİH
==================================================

{data["best_bet"]}

- best_bet, listedeki seçenekler (Çifte Şans hariç) arasından GERÇEKLEŞME OLASILIĞI EN YÜKSEK olandır.
  Liste zaten oranı düşük seçenekleri içermez; yüksek oran bir avantaj değildir, yalnızca olasılığa bak.
  Kendi olasılığını normalize piyasa olasılığıyla kıyasla.
- Yazımı listedeki gibi AYNEN kullan; listede olmayan market veya oran üretme.
- alt_bet kullanılmıyor: her zaman 'YOK'.

==================================================
7. ÇIKTI
==================================================

Yanıtı vermeden önce kontrol et: skor ↔ MS sonucu, İY skoru ↔ İY sonucu, toplam gol ↔ ALT/ÜST, skor ↔ KG, seçimler listede var.
SADECE Pydantic şemasına uygun JSON döndür; markdown veya açıklama ekleme.
analysis_summary: 3-4 cümlelik Türkçe analiz; piyasadan saptıysan nedenini ve kullandığın somut kanıtı yaz.
Bulamadığın bilgiyi uydurma; belirsizliği güven puanına yansıt.
"""

def build_prompt(match_context: Dict[str, Any]) -> str:
    match_info = match_context.get("match", {})
    sport_type = match_info.get("sport_type", rules.FOOTBALL)
    if rules.is_basketball(sport_type):
        return _build_basketball_prompt(match_context)
    return _build_football_prompt(match_context)

# ==============================================================================
# YANIT İŞLEME
# ==============================================================================

def _response_schema(sport_type: str) -> Type[BaseModel]:
    return BasketballPrediction if rules.is_basketball(sport_type) else MatchPrediction

def _prediction_config(use_google_search: bool, sport_type: str = rules.FOOTBALL) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=_response_schema(sport_type),
        tools=[{"google_search": {}}] if use_google_search else None
    )

def _total_from_score(score: Optional[str]) -> Optional[int]:
    try:
        home, away = (int(part) for part in str(score).replace(" ", "").split("-"))
        return home + away
    except (TypeError, ValueError):
        return None

def finalize_basketball_prediction(prediction: Dict[str, Any], match_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Basketbol tahminini tutarlı hale getirir: maç sonu toplam sayı, tahmini skordaki iki sayının toplamına eşitlenir
    (skor okunamazsa modelin verdiği toplam kalır).
    Sınır varsa predicted_over_under_3 = "ÜST" if predicted_total_points > sınır else "ALT", yoksa None.
    predicted_kg None kalır.
    """
    score_total = _total_from_score(prediction.get("predicted_score"))
    if score_total is not None:
        prediction["predicted_total_points"] = score_total

    line = _total_points_line(match_context) if match_context else None
    total = prediction.get("predicted_total_points")
    prediction["predicted_over_under_3"] = ("ÜST" if total > line else "ALT") if line is not None and total is not None else None
    prediction["predicted_kg"] = None
    return prediction

def _parse_response(response: Any) -> Optional[Dict[str, Any]]:
    """Gemini yanıtını sözlüğe çevirir. Pydantic parsed model varsa doğrudan onu kullanır; boş yanıtta None döner."""
    if getattr(response, "parsed", None):
        return response.parsed.model_dump()
    if response.text:
        return json.loads(response.text)
    return None

def _pick_candidate(candidates: List[Dict[str, Any]], selection: Optional[str]) -> Optional[Dict[str, Any]]:
    target = rules.normalize_selection(selection)
    return next((c for c in candidates if rules.normalize_selection(c["selection"]) == target), None)

def finalize_best_bet(prediction: Dict[str, Any], match_context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Gemini'nin ⭐ en güçlü tercihini ve 2. tercihini doğrulayıp sözlüklere toplar:
    best_bet = {"market", "selection", "odds", "confidence", "reason"}, alt_bet = {"market", "selection", "odds", "confidence"}.
    - best_bet: aday listesinde (MS / İY / ALT-ÜST / KG) olmalı; değilse None.
    - alt_bet: sadece best_bet oranı MIN_BEST_BET_ODDS altındaysa (veya best_bet yoksa) tutulur ve seçimi oranı eşik ve üstü
      adaylardan olmalı; aksi halde ('YOK' dahil) None.
    """
    best = _pick_candidate(_best_bet_candidates(match_context), prediction.pop("best_bet_selection", None))
    best_confidence = prediction.pop("best_bet_confidence", None)
    reason = prediction.pop("best_bet_reason", None)
    alt = _pick_candidate(_alt_bet_candidates(match_context), prediction.pop("alt_bet_selection", None))
    alt_confidence = prediction.pop("alt_bet_confidence", None)
    prediction.pop("best_bet_market", None)

    prediction["best_bet"] = ({"market": best["market"], "selection": best["selection"], "odds": best["odds"],
                               "confidence": best_confidence, "reason": reason} if best else None)
    needs_alt = best is None or best["odds"] < config.MIN_BEST_BET_ODDS
    prediction["alt_bet"] = ({"market": alt["market"], "selection": alt["selection"], "odds": alt["odds"],
                              "confidence": alt_confidence} if needs_alt and alt and alt_confidence else None)
    return prediction

def _finalize(prediction: Dict[str, Any], match_context: Dict[str, Any]) -> Dict[str, Any]:
    if rules.is_basketball(_sport_type(match_context)):
        finalize_basketball_prediction(prediction, match_context)
    return finalize_best_bet(prediction, match_context)

def generate_prediction(match_context: Dict[str, Any], model_name: str = DEFAULT_MODEL) -> Dict[str, Any]:
    """
    Verilen maç bağlamı için Google Gemini AI modelini Google Search Grounding ile çalıştırır ve yapılandırılmış tahmin döner.
    Futbol ve basketbol için farklı prompt ve yanıt şeması kullanılır.
    """
    client = get_gemini_client()
    prompt = build_prompt(match_context)
    sport_type = match_context.get("match", {}).get("sport_type", rules.FOOTBALL)
    last_exception = None

    for attempt in range(MAX_RETRIES):
        is_last_attempt = attempt == MAX_RETRIES - 1
        try:
            response = client.models.generate_content(model=model_name, contents=prompt, config=_prediction_config(True, sport_type))
            result = _parse_response(response)
            if result is not None:
                return _finalize(result, match_context)
        except ServerError as e:
            last_exception = e
            if is_last_attempt:
                raise
            time.sleep(3 * (attempt + 1))
        except Exception as e:
            last_exception = e
            if is_last_attempt:
                raise
            # Google Search tool kısıtlaması veya arama hatası olursa tools'suz dene
            try:
                response = client.models.generate_content(model=model_name, contents=prompt, config=_prediction_config(False, sport_type))
                result = _parse_response(response)
                if result is not None:
                    return _finalize(result, match_context)
            except Exception:
                time.sleep(2)

    raise RuntimeError(f"Gemini AI tahmin üretimi başarısız oldu: {last_exception}")
