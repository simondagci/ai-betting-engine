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
MAX_ODDS_ROWS = 25
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
    """Basketbol tahmini: beraberlik, karşılıklı gol ve Alt/Üst yoktur; bunun yerine maç sonu toplam sayı tahmin edilir."""
    analysis_summary: str = Field(description="3-4 cümlelik Türkçe detaylı taktiksel analiz ve değerlendirme")
    predicted_ht_result: str = Field(description="İlk Yarı Tahmini: İY 1, İY 0 veya İY 2")
    predicted_ht_score: str = Field(description="Tahmini İLK YARI skoru (Örn: '42-38', '45-45')")
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
    """⭐ en güçlü tercih adayları: Gemini'nin kendi tahmin marketleri (MS, İY, 3 gol ALT/ÜST, KG), oran sınırı yok."""
    return rules.bet_candidates(match_context.get("odds", []), _sport_type(match_context), markets=rules.BEST_BET_MARKETS)

def _alt_bet_candidates(match_context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """2. tercih adayları: oranı MIN_BEST_BET_ODDS ve üstü tüm seçenekler (Çifte Şans dahil)."""
    return rules.bet_candidates(match_context.get("odds", []), _sport_type(match_context), config.MIN_BEST_BET_ODDS)

def _format_best_bet_section(match_context: Dict[str, Any]) -> str:
    """
    ⭐ en güçlü tercih ve 2. tercih bölümü. Seçenekler oran ve piyasa olasılığıyla verilir: Gemini en çok güvendiği seçimi
    yazar; o seçimin oranı MIN_BEST_BET_ODDS altındaysa, oranı eşik ve üstü seçenekler arasından bir 2. tercih de yazar.
    """
    min_odds = config.MIN_BEST_BET_ODDS
    candidates = rules.bet_candidates(match_context.get("odds", []), _sport_type(match_context))
    header = "--- EN GÜÇLÜ TERCİH (best_bet) VE 2. TERCİH (alt_bet) ---"
    if not candidates:
        return (f"{header}\nBu maçta uygun oran yok: best_bet_selection için kendi MS tahminini yaz, "
                "alt_bet_selection alanına 'YOK', alt_bet_confidence için 0 ver.")
    lines = [f"- {c['selection']} ({c['market_label']}) | Oran {c['odds']:.2f} | Piyasa olasılığı %{100 / c['odds']:.0f}"
             for c in candidates]
    return "\n".join([
        header,
        "Seçenekler (oran | oranın ima ettiği piyasa olasılığı):",
        *lines,
        "1) best_bet: Çifte Şans HARİÇ seçenekler arasından gerçekleşeceğine EN ÇOK güvendiğin TEK seçeneği yaz "
        "(listedeki yazımıyla AYNEN, örn. 'MS 1', 'ALT'), gerçekleşme olasılığını (best_bet_confidence, 1-100) ve 1 cümlelik gerekçesini (best_bet_reason).",
        f"2) alt_bet: best_bet seçeneğinin oranı {min_odds:.2f} altındaysa, oranı {min_odds:.2f} ve üstü seçenekler (Çifte Şans dahil) "
        "arasından gerçekleşeceğine EN ÇOK güvendiğin TEK seçeneği alt_bet_selection'a, olasılığını alt_bet_confidence'a yaz. "
        f"best_bet oranı {min_odds:.2f} ve üstüyse veya uygun seçenek yoksa alt_bet_selection 'YOK', alt_bet_confidence 0 olsun.",
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

def _build_basketball_prompt(match_context: Dict[str, Any]) -> str:
    match_info = match_context.get("match", {})
    home_team = match_info.get("home_team", "Ev Sahibi")
    away_team = match_info.get("away_team", "Deplasman")
    data = _data_sections(match_context, score_label="Sayı")
    line = _total_points_line(match_context)
    line_rule = (f"- Bu maç için bültenin belirlediği Toplam Sayı Alt/Üst baremi: {line:g}. Tahmin ettiğin toplam sayı "
                 "(predicted_total_points) bu sınırın üzerindeyse ÜST, altındaysa ALT sayılacaktır.") if line is not None else ""

    return f"""
Sen profesyonel bir basketbol veri analisti, kıdemli bahis stratejisti ve yapay zeka tahmin motorusun.
Aşağıda verilen karşılaşma bilgilerini, bahis oranlarını ve Google Search aracını kullanarak internetteki güncel haberleri, takım formlarını, sakatlıkları ve rotasyon durumlarını analiz et.

--- MAÇ BİLGİLERİ ---
Spor Türü: BASKETBOL
Lig / Turnuva: {match_info.get("league_name", "Bilinmeyen Lig")}
Tarih ve Saat: {match_info.get("match_date", "")} {match_info.get("match_time", "")}
Ev Sahibi Takım: {home_team}
Deplasman Takımı: {away_team}

--- İDDAA BAHİS ORANLARI ---
{data["odds"]}

--- VERİTABANI İSTATİSTİKLERİ ---
(Satır formatı: Tarih | Lig | Ev Sahibi Skor Deplasman (İY skoru) [ilgili takım için sonuç: G=Galibiyet, M=Mağlubiyet])

1. Ev Sahibi Takım ({home_team}) Son Maçları:
{data["home_last"]}

2. Deplasman Takımı ({away_team}) Son Maçları:
{data["away_last"]}

3. Aralarındaki İkili Mücadele Geçmişi (H2H):
{data["h2h"]}

4. Lig Puan Durumu (Sayı: atılan:yenilen):
{data["table"]}

5. Sakat ve Cezalı Oyuncular:
{data["missing"]}

--- ÇOK ÖNEMLİ BASKETBOL KURALLARI ---
- Maç sonucu uzatmalar dahildir, beraberlik YOKTUR: predicted_ft_result sadece "MS 1" veya "MS 2" olabilir.
- Basketbolda karşılıklı gol (KG) yoktur.
- MAÇ SONU TOPLAM SAYI tahmini yap (predicted_total_points): iki takımın maç sonundaki toplam sayısı, uzatmalar dahil.
  Takımların son maçlardaki attığı / yediği sayıları ve puan durumundaki sayı ortalamalarını dikkate al.
- predicted_score ile predicted_total_points tutarlı olmalı (skordaki iki sayının toplamı = toplam sayı).
{line_rule}

{data["best_bet"]}

Lütfen Google Search ile takımların en güncel durumlarını (sakatlıklar, rotasyon, back-to-back maçlar, teknik direktör açıklamaları) tara ve aşağıdaki JSON formatında profesyonel tahmin oluştur.
"""

def build_prompt(match_context: Dict[str, Any]) -> str:
    match_info = match_context.get("match", {})
    sport_type = match_info.get("sport_type", rules.FOOTBALL)
    if rules.is_basketball(sport_type):
        return _build_basketball_prompt(match_context)

    home_team = match_info.get("home_team", "Ev Sahibi")
    away_team = match_info.get("away_team", "Deplasman")
    data = _data_sections(match_context, score_label="Gol")

    prompt = f"""
Sen profesyonel bir futbol veri analisti, kıdemli bahis stratejisti ve yapay zeka tahmin motorusun.
Aşağıda verilen karşılaşma bilgilerini, bahis oranlarını ve Google Search aracını kullanarak internetteki güncel haberleri, takım formlarını, sakatlıkları ve saha koşullarını analiz et.

--- MAÇ BİLGİLERİ ---
Spor Türü: {sport_type}
Lig / Turnuva: {match_info.get("league_name", "Bilinmeyen Lig")}
Tarih ve Saat: {match_info.get("match_date", "")} {match_info.get("match_time", "")}
Ev Sahibi Takım: {home_team}
Deplasman Takımı: {away_team}

--- İDDAA BAHİS ORANLARI ---
{data["odds"]}

--- VERİTABANI İSTATİSTİKLERİ ---
(Satır formatı: Tarih | Lig | Ev Sahibi Skor Deplasman (İY skoru) [ilgili takım için sonuç: G=Galibiyet, B=Beraberlik, M=Mağlubiyet])

1. Ev Sahibi Takım ({home_team}) Son Maçları:
{data["home_last"]}

2. Deplasman Takımı ({away_team}) Son Maçları:
{data["away_last"]}

3. Aralarındaki İkili Mücadele Geçmişi (H2H):
{data["h2h"]}

4. Lig Puan Durumu:
{data["table"]}

5. Sakat ve Cezalı Oyuncular:
{data["missing"]}

--- ÇOK ÖNEMLİ İŞ KURALI (ALT / ÜST SINIRI: 3 GOL) ---
Bu sistemde geleneksel 2.5 baremi yerine KESİNLİKLE 3 GOL KURALI uygulanmaktadır:
- Eğer maçta atılacak toplam gol sayısı 4 veya daha fazla ise (Gol sayısı >= 4): Tahmin kesinlikle "ÜST" olmalıdır.
- Eğer maçta atılacak toplam gol sayısı 3 veya daha az ise (Gol sayısı <= 3): Tahmin kesinlikle "ALT" olmalıdır.
(Örn: 2-1 veya 3-0 biten maç 3 gol olduğu için ALT kabul edilir; 2-2 veya 3-1 biten maç 4 gol olduğu için ÜST kabul edilir).

{data["best_bet"]}

Lütfen Google Search ile takımların en güncel durumlarını (kadro eksikleri, teknik direktör açıklamaları, form grafiği, hava durumu) tara ve aşağıdaki JSON formatında profesyonel tahmin oluştur.
"""
    return prompt

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
