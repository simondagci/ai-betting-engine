import os
import json
import time
from typing import Dict, Any, Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from google.genai.errors import ServerError, ClientError
import config

load_dotenv()

DEFAULT_MODEL = getattr(config, "GEMINI_MODEL", "gemini-3.8-flash")

class MatchPrediction(BaseModel):
    analysis_summary: str = Field(description="3-4 cümlelik Türkçe detaylı taktiksel analiz ve değerlendirme")
    predicted_ht_result: str = Field(description="İlk Yarı Tahmini: İY 1, İY 0 veya İY 2")
    predicted_ft_result: str = Field(description="Maç Sonu Tahmini: MS 1, MS 0 veya MS 2")
    predicted_over_under_3: str = Field(description="3 Gol Sınırına Göre Alt/Üst Tahmini: Toplam gol >= 4 ise 'ÜST', toplam gol <= 3 ise 'ALT'")
    predicted_kg: str = Field(description="Karşılıklı Gol Tahmini: 'KG VAR' veya 'KG YOK'")
    predicted_score: str = Field(description="Tahmini maç skoru (Örn: '2-1', '1-1', '3-0')")
    confidence_score: int = Field(description="Tahminin gerçekleşme olasılığına dair 1 ile 100 arasında integer güven skoru")

def get_gemini_client() -> genai.Client:
    api_key = getattr(config, "GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY ortam değişkeni bulunamadı. Lütfen .env dosyasını kontrol edin.")
    return genai.Client(api_key=api_key)

def build_prompt(match_context: Dict[str, Any]) -> str:
    match_info = match_context.get("match", {})
    home_team = match_info.get("home_team", "Ev Sahibi")
    away_team = match_info.get("away_team", "Deplasman")
    league = match_info.get("league_name", "Bilinmeyen Lig")
    match_date = match_info.get("match_date", "")
    match_time = match_info.get("match_time", "")
    sport_type = match_info.get("sport_type", "FOOTBALL")
    
    odds = match_context.get("odds", [])
    if odds:
        # Önemli marketleri öne çıkar
        main_odds = [f"- {o.get('market_name', 'Bahis')}: {o.get('option_name')} -> Oran: {o.get('odds_value')}" for o in odds[:20]]
        odds_str = "\n".join(main_odds)
    else:
        odds_str = "Açılan oran bilgisi bulunamadı."
    
    h2h = match_context.get("h2h")
    h2h_str = json.dumps(h2h, ensure_ascii=False, indent=2) if h2h else "Yerel ikili mücadele verisi yok (İnternetten kontrol ediniz)."
    
    home_last = match_context.get("home_last")
    home_last_str = json.dumps(home_last, ensure_ascii=False, indent=2) if home_last else "Yerel son maç verisi yok (İnternetten kontrol ediniz)."
    
    away_last = match_context.get("away_last")
    away_last_str = json.dumps(away_last, ensure_ascii=False, indent=2) if away_last else "Yerel son maç verisi yok (İnternetten kontrol ediniz)."
    
    missing = match_context.get("missing")
    missing_str = json.dumps(missing, ensure_ascii=False, indent=2) if missing else "Yerel sakat/cezalı verisi yok (İnternetten kontrol ediniz)."

    prompt = f"""
Sen profesyonel bir futbol veri analisti, kıdemli bahis stratejisti ve yapay zeka tahmin motorusun. 
Aşağıda verilen karşılaşma bilgilerini, bahis oranlarını ve Google Search aracını kullanarak internetteki güncel haberleri, takım formlarını, sakatlıkları ve saha koşullarını analiz et.

--- MAÇ BİLGİLERİ ---
Spor Türü: {sport_type}
Lig / Turnuva: {league}
Tarih ve Saat: {match_date} {match_time}
Ev Sahibi Takım: {home_team}
Deplasman Takımı: {away_team}

--- İDDAA BAHİS ORANLARI ---
{odds_str}

--- VERİTABANI İSTATİSTİKLERİ ---
1. Ev Sahibi Takım ({home_team}) Son Maçları:
{home_last_str[:1200]}

2. Deplasman Takımı ({away_team}) Son Maçları:
{away_last_str[:1200]}

3. Aralarındaki İkili Mücadele Geçmişi (H2H):
{h2h_str[:1200]}

4. Sakat ve Cezalı Oyuncular:
{missing_str[:800]}

--- ÇOK ÖNEMLİ İŞ KURALI (ALT / ÜST SINIRI: 3 GOL) ---
Bu sistemde geleneksel 2.5 baremi yerine KESİNLİKLE 3 GOL KURALI uygulanmaktadır:
- Eğer maçta atılacak toplam gol sayısı 4 veya daha fazla ise (Gol sayısı >= 4): Tahmin kesinlikle "ÜST" olmalıdır.
- Eğer maçta atılacak toplam gol sayısı 3 veya daha az ise (Gol sayısı <= 3): Tahmin kesinlikle "ALT" olmalıdır.
(Örn: 2-1 veya 3-0 biten maç 3 gol olduğu için ALT kabul edilir; 2-2 veya 3-1 biten maç 4 gol olduğu için ÜST kabul edilir).

Lütfen Google Search ile takımların en güncel durumlarını (kadro eksikleri, teknik direktör açıklamaları, form grafiği, hava durumu) tara ve aşağıdaki JSON formatında profesyonel tahmin oluştur.
"""
    return prompt

def generate_prediction(match_context: Dict[str, Any], model_name: str = DEFAULT_MODEL) -> Dict[str, Any]:
    """
    Verilen maç bağlamı için Google Gemini AI modelini Google Search Grounding ile çalıştırır ve yapılandırılmış tahmin döner.
    """
    client = get_gemini_client()
    prompt = build_prompt(match_context)
    
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=MatchPrediction,
        tools=[{"google_search": {}}]
    )
    
    max_retries = 3
    last_exception = None
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config
            )
            
            # Pydantic parsed model doğrudan kullanılabilir
            if hasattr(response, "parsed") and response.parsed:
                return response.parsed.model_dump()
            
            if response.text:
                return json.loads(response.text)
                
        except ServerError as e:
            last_exception = e
            if attempt < max_retries - 1:
                time.sleep(3 * (attempt + 1))
            else:
                raise e
        except Exception as e:
            last_exception = e
            # Google Search tool kısıtlaması veya arama hatası olursa tools'suz dene
            if attempt < max_retries - 1:
                try:
                    fallback_config = types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=MatchPrediction
                    )
                    res_fallback = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=fallback_config
                    )
                    if hasattr(res_fallback, "parsed") and res_fallback.parsed:
                        return res_fallback.parsed.model_dump()
                    if res_fallback.text:
                        return json.loads(res_fallback.text)
                except Exception:
                    time.sleep(2)
            else:
                raise e

    raise RuntimeError(f"Gemini AI tahmin üretimi başarısız oldu: {last_exception}")

def fetch_live_team_stats(home_team: str, away_team: str, model_name: str = DEFAULT_MODEL) -> Dict[str, Any]:
    """
    Google Search Grounding kullanarak iki takımın aralarındaki maçları (H2H)
    ve son oynadıkları maçları canlı olarak arar ve yapılandırılmış JSON döner.
    """
    client = get_gemini_client()
    prompt = f"""
Google Search aracını kullanarak futbol takımları '{home_team}' ve '{away_team}' için son maç ve ikili mücadele (H2H) verilerini ara.
Lütfen SADECE aşağıdaki JSON şemasına uygun saf JSON olarak yanıt ver:
{{
  "h2h": [
    {{"date": "YYYY-MM-DD", "home": "{home_team}", "away": "{away_team}", "score": "2-1", "league": "Lig Adı"}}
  ],
  "home_last": [
    {{"date": "YYYY-MM-DD", "home": "{home_team}", "away": "Rakip Takım", "score": "1-0", "league": "Lig Adı"}}
  ],
  "away_last": [
    {{"date": "YYYY-MM-DD", "home": "Rakip Takım", "away": "{away_team}", "score": "2-2", "league": "Lig Adı"}}
  ]
}}
- 'h2h': İki takımın birbirine karşı oynadığı son 3-5 resmi maç.
- 'home_last': '{home_team}' takımının oynadığı son 5 resmi maç ve skoru.
- 'away_last': '{away_team}' takımının oynadığı son 5 resmi maç ve skoru.
"""
    config_ai = types.GenerateContentConfig(
        response_mime_type="application/json",
        tools=[{"google_search": {}}]
    )
    
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=config_ai
        )
        if response.text:
            return json.loads(response.text)
    except Exception as e:
        print(f"Canlı istatistik çekilirken hata: {e}")
        
    return {"h2h": [], "home_last": [], "away_last": []}

