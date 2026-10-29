import os
import sys
import time
from typing import List, Dict, Any, Optional
import config
import database
import nesine_api
import gemini_engine
import web_server

# ANSI Renk Kodları
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    DIM = '\033[2m'
    ENDC = '\033[0m'

def print_banner():
    conn = database.get_connection()
    stats = database.get_db_stats(conn)
    conn.close()

    api_status = f"{Colors.GREEN}AÇIK{Colors.ENDC}" if config.ENABLE_GEMINI_ANALYSIS else f"{Colors.YELLOW}BEKLEMEDE (KAPALI){Colors.ENDC}"
    
    print(f"{Colors.CYAN}{Colors.BOLD}")
    print("=" * 68)
    print("   ⚽  AI BETTING ENGINE - GOOGLE GEMINI 3.8 FLASH  🤖   ")
    print("      Futbol Analiz, İstatistik & Akıllı Bahis Motoru      ")
    print("=" * 68)
    print(f"{Colors.ENDC}", end="")
    print(f" ⚙️  Gemini API Durumu : {api_status} | İzinli Lig: {Colors.CYAN}{stats['enabled_leagues']}/{stats['leagues']}{Colors.ENDC} | Kayıtlı Maç: {Colors.CYAN}{stats['matches']}{Colors.ENDC}")
    print(f"{Colors.CYAN}{'-' * 68}{Colors.ENDC}\n")

def sync_bulletin() -> None:
    print(f"\n{Colors.YELLOW}🔄 Nesine İddaa Bülteni Çekiliyor...{Colors.ENDC}")
    events = nesine_api.fetch_bulletin()
    if not events:
        print(f"{Colors.RED}❌ Bültenden maç verisi çekilemedi! İnternet bağlantınızı kontrol edin.{Colors.ENDC}")
        return
    
    conn = database.get_connection()
    # Önce tarihi geçmiş mevcut maçları match_completed=1 yap
    expired_cnt = database.update_expired_matches(conn)
    
    print(f"📥 Toplam {len(events)} bülten etkinliği alındı. Hangi maçlar kaydedilsin?")
    print("  1. Sadece Futbol Maçları (Önerilen)")
    print("  2. Tüm Karşılaşmalar (Futbol + Basketbol)")
    sub_choice = input(f"{Colors.BOLD}Seçiminiz (1/2, Varsayılan: 1): {Colors.ENDC}").strip() or "1"
    
    allowed_types = ("1",) if sub_choice == "1" else ("1", "2")
    
    # Toplu ve yüksek performanslı senkronizasyon (Döngü içi SQL yok)
    sync_res = database.sync_bulletin_matches_and_odds(
        conn, 
        events, 
        allowed_types, 
        default_league_gemini_analysis=config.DEFAULT_LEAGUE_GEMINI_ANALYSIS
    )
    conn.close()
    
    print(f"\n{Colors.GREEN}{Colors.BOLD}✅ Bülten Senkronizasyonu Başarıyla Tamamlandı! (Toplu SQL İşlemi){Colors.ENDC}")
    print(f"   ➕ Yeni Eklenen Futbol Maçı     : {Colors.CYAN}{sync_res['new_football']}{Colors.ENDC}")
    if sub_choice != "1":
        print(f"   ➕ Yeni Eklenen Basketbol Maçı  : {Colors.CYAN}{sync_res['new_basketball']}{Colors.ENDC}")
    print(f"   ⏩ Atlanan (Zaten DB'de Kayıtlı): {Colors.YELLOW}{sync_res['skipped']}{Colors.ENDC}")
    if expired_cnt > 0:
        print(f"   ⏰ Tarihi Geçen/Biten Maç Sayısı: {Colors.BLUE}{expired_cnt}{Colors.ENDC}")
    print(f"   💰 Kaydedilen Yeni Bahis Oranı  : {Colors.CYAN}{sync_res['odds_total']}{Colors.ENDC}")

def list_and_search_matches() -> None:
    conn = database.get_connection()
    print(f"\n{Colors.BOLD}🔎 MAÇ LİSTELEME VE ARAMA{Colors.ENDC}")
    query = input("Aramak istediğiniz Takım, Lig veya Maç Kodu (Boş = Tümü): ").strip()
    status_filter = input("Durum Filtresi (1: Tümü, 2: PENDING, 3: ANALYZED, 4: COMPLETED, Varsayılan: 1): ").strip()
    status_map = {"2": "PENDING", "3": "ANALYZED", "4": "COMPLETED"}
    status = status_map.get(status_filter, "")
    
    matches = database.search_matches(conn, query=query, status=status, limit=30)
    if not matches:
        print(f"{Colors.YELLOW}ℹ️ Kriterlere uygun maç bulunamadı.{Colors.ENDC}")
        conn.close()
        return

    print(f"\n{Colors.CYAN}{'KOD':<8} {'TARİH / SAAT':<18} {'LİG':<22} {'KARŞILAŞMA':<30} {'DURUM':<10} {'BİTTİ Mİ?':<12} {'GEMINI':<8}{Colors.ENDC}")
    print("-" * 115)
    for m in matches:
        dt = f"{m['match_date']} {m['match_time']}"
        teams = f"{m['home_team']} vs {m['away_team']}"
        st_color = Colors.GREEN if m['status'] == 'ANALYZED' else (Colors.BLUE if m['status'] == 'COMPLETED' else Colors.YELLOW)
        comp_str = f"{Colors.BLUE}EVET{Colors.ENDC}" if m['match_completed'] else f"{Colors.GREEN}HAYIR{Colors.ENDC}"
        gem_perm = f"{Colors.GREEN}AÇIK{Colors.ENDC}" if m['league_gemini_analysis'] else f"{Colors.DIM}KAPALI{Colors.ENDC}"
        print(f"{m['code']:<8} {dt:<18} {str(m['league_name'])[:20]:<22} {teams[:28]:<30} {st_color}{m['status']:<10}{Colors.ENDC} {comp_str:<21} {gem_perm:<8}")
    print("-" * 115)
    print(f"Toplam {len(matches)} maç gösteriliyor (Maks: 30).\n")
    conn.close()

def manage_leagues() -> None:
    conn = database.get_connection()
    while True:
        print(f"\n{Colors.BOLD}🏆 LİGLER VE GEMINI ANALİZ İZİNLERİ YÖNETİMİ{Colors.ENDC}")
        print("  1. Tüm Ligleri Listele")
        print("  2. Lig Ara / Filtrele")
        print("  3. Sadece Analiz İzni Açık (gemini_analysis=1) Ligleri Listele")
        print("  4. Bir Ligin Analiz İznini Aç / Kapat (ID ile)")
        print("  5. Tüm Liglerin Analiz İznini Toplu AÇ")
        print("  6. Tüm Liglerin Analiz İznini Toplu KAPAT")
        print("  7. Ana Menüye Dön")
        print("-" * 64)
        
        choice = input(f"{Colors.BOLD}İşlem seçiniz (1-7): {Colors.ENDC}").strip()
        
        if choice in ("1", "2", "3"):
            q = ""
            only_en = False
            if choice == "2":
                q = input("Aramak istediğiniz Lig Adı: ").strip()
            elif choice == "3":
                only_en = True
                
            leagues = database.get_leagues(conn, query=q, only_enabled=only_en)
            if not leagues:
                print(f"{Colors.YELLOW}ℹ️ Kriterlere uygun lig bulunamadı.{Colors.ENDC}")
                continue
                
            print(f"\n{Colors.CYAN}{'ID':<6} {'LİG ADI':<40} {'SPOR':<10} {'MAÇ':<6} {'GEMINI ANALİZİ'}{Colors.ENDC}")
            print("-" * 80)
            for l in leagues[:50]:
                perm_text = f"{Colors.GREEN}✅ AÇIK (True){Colors.ENDC}" if l["gemini_analysis"] else f"{Colors.RED}❌ KAPALI (False){Colors.ENDC}"
                print(f"{l['id']:<6} {str(l['name'])[:38]:<40} {l['sport_type']:<10} {l['match_count']:<6} {perm_text}")
            print("-" * 80)
            print(f"Toplam {len(leagues)} lig listelendi (Gösterilen maks: 50).\n")
            
        elif choice == "4":
            lid_str = input("İznini değiştirmek istediğiniz Lig ID'sini girin: ").strip()
            if not lid_str.isdigit():
                print(f"{Colors.RED}⚠️ Geçerli bir sayı giriniz.{Colors.ENDC}")
                continue
            lid = int(lid_str)
            curr = database.is_league_analysis_enabled(conn, lid)
            new_val = not curr
            database.set_league_gemini_analysis(conn, lid, new_val)
            conn.commit()
            status_str = f"{Colors.GREEN}AÇIK (1){Colors.ENDC}" if new_val else f"{Colors.RED}KAPALI (0){Colors.ENDC}"
            print(f"{Colors.GREEN}✅ Lig (ID: {lid}) için Gemini analizi {status_str} olarak güncellendi.{Colors.ENDC}")
            
        elif choice == "5":
            cnt = database.set_all_leagues_gemini_analysis(conn, True)
            conn.commit()
            print(f"{Colors.GREEN}✅ Toplam {cnt} ligin Gemini analiz izni AÇILDI.{Colors.ENDC}")
            
        elif choice == "6":
            cnt = database.set_all_leagues_gemini_analysis(conn, False)
            conn.commit()
            print(f"{Colors.YELLOW}⚠️ Toplam {cnt} ligin Gemini analiz izni KAPATILDI.{Colors.ENDC}")
            
        elif choice == "7":
            break
            
    conn.close()

def sync_match_stats() -> None:
    print(f"\n{Colors.YELLOW}📊 Takım İstatistikleri Çekiliyor (H2H, Son Maçlar, Eksikler)...{Colors.ENDC}")
    conn = database.get_connection()
    matches = database.get_pending_matches(conn, limit=20)
    if not matches:
        print(f"{Colors.YELLOW}ℹ️ Veritabanında işlenecek maç bulunamadı.{Colors.ENDC}")
        conn.close()
        return
    
    print(f"🔎 İlk {len(matches)} maç için istatistik servisleri sorgulanıyor...")
    success_count = 0
    
    for idx, match in enumerate(matches, 1):
        match_id = match["id"]
        code = match["code"]
        print(f"[{idx}/{len(matches)}] KOD: {code} -> {match['home_team']} vs {match['away_team']}...", end=" ")
        
        h2h = nesine_api.fetch_competition_history(code)
        last_matches = nesine_api.fetch_last_matches(code)
        missing = nesine_api.fetch_missing_players(code)
        league_table = nesine_api.fetch_league_table(code)
        
        home_last = last_matches.get("HomeTeamMatches") if isinstance(last_matches, dict) else None
        away_last = last_matches.get("AwayTeamMatches") if isinstance(last_matches, dict) else None
        
        database.save_match_stats(conn, match_id, h2h, home_last, away_last, missing, league_table)
        conn.commit()
        success_count += 1
        print(f"{Colors.GREEN}OK{Colors.ENDC}")
        time.sleep(0.2)
        
    conn.close()
    print(f"{Colors.GREEN}✅ {success_count} maçın istatistik bilgisi işlendi.{Colors.ENDC}")

def display_prediction_card(match_info: Dict[str, Any], pred: Dict[str, Any], odds: List[Dict[str, Any]] = None):
    print(f"\n{Colors.GREEN}{'='*64}{Colors.ENDC}")
    print(f"{Colors.BOLD}🤖 GEMINI AI MAÇ TAHMİN KARTI{Colors.ENDC}")
    print(f"{Colors.CYAN}Maç Kodu   : {match_info.get('code')}{Colors.ENDC}")
    print(f"Karşılaşma : {Colors.BOLD}{match_info.get('home_team')} vs {match_info.get('away_team')}{Colors.ENDC}")
    print(f"Lig / Saat : {match_info.get('league_name')} | {match_info.get('match_date')} {match_info.get('match_time')}")
    print(f"{Colors.GREEN}{'-'*64}{Colors.ENDC}")
    
    # Oran Özeti
    if odds:
        ms1 = next((o['odds_value'] for o in odds if o['market_name'] == 'Maç Sonucu' and o['option_name'] == 'MS 1'), '-')
        ms0 = next((o['odds_value'] for o in odds if o['market_name'] == 'Maç Sonucu' and o['option_name'] == 'MS 0'), '-')
        ms2 = next((o['odds_value'] for o in odds if o['market_name'] == 'Maç Sonucu' and o['option_name'] == 'MS 2'), '-')
        kg_v = next((o['odds_value'] for o in odds if o['market_name'] == 'Karşılıklı Gol' and 'VAR' in o['option_name']), '-')
        u25 = next((o['odds_value'] for o in odds if '2.5' in o['market_name'] and 'Üst' in o['option_name']), '-')
        print(f"📌 İddaa Oranları : MS 1: {ms1} | MS 0: {ms0} | MS 2: {ms2} | 2.5 Üst: {u25} | KG Var: {kg_v}")
        print(f"{Colors.GREEN}{'-'*64}{Colors.ENDC}")

    conf = pred.get('confidence_score', 0)
    conf_color = Colors.GREEN if conf >= 75 else (Colors.YELLOW if conf >= 60 else Colors.RED)
    
    print(f"⏱️  İlk Yarı Tahmini     : {Colors.BOLD}{pred.get('predicted_ht_result')}{Colors.ENDC}")
    print(f"🏆 Maç Sonu Tahmini     : {Colors.BOLD}{pred.get('predicted_ft_result')}{Colors.ENDC}")
    print(f"⚽ 3 Gol Sınırı (Alt/Üst): {Colors.BOLD}{pred.get('predicted_over_under_3')}{Colors.ENDC} {Colors.DIM}(>=4 gol: ÜST, <=3 gol: ALT){Colors.ENDC}")
    print(f"🤝 Karşılıklı Gol       : {Colors.BOLD}{pred.get('predicted_kg')}{Colors.ENDC}")
    print(f"🎯 Tahmini Skor         : {Colors.BOLD}{pred.get('predicted_score')}{Colors.ENDC}")
    print(f"🌟 Güven Skoru          : {conf_color}{Colors.BOLD}%{conf}{Colors.ENDC}")
    print(f"\n📝 {Colors.BOLD}Taktiksel Analiz Özeti:{Colors.ENDC}")
    print(f"   {Colors.CYAN}{pred.get('analysis_summary')}{Colors.ENDC}")
    print(f"{Colors.GREEN}{'='*64}{Colors.ENDC}\n")

def run_single_prediction() -> None:
    # 1. Global config kontrolü
    if not config.ENABLE_GEMINI_ANALYSIS:
        print(f"\n{Colors.YELLOW}⚠️  BİLGİ: config.py dosyasında 'ENABLE_GEMINI_ANALYSIS = False' durumundadır.")
        print(f"Yani Gemini analizleri şu an genel olarak duraklatılmıştır.{Colors.ENDC}")
        confirm = input("Bu maç için yine de Gemini analizi almak istiyor musunuz? (evet / hayır): ").strip().lower()
        if confirm not in ("evet", "yes", "e", "y"):
            print("Analiz iptal edildi.")
            return

    code = input("\nTahmin yapılacak Maç Kodunu (C) girin: ").strip()
    if not code:
        return
        
    conn = database.get_connection()
    match_row = database.get_match_by_code(conn, code)
    if not match_row:
        print(f"{Colors.RED}❌ {code} kodlu maç veritabanında bulunamadı! Önce bülteni çekmeyi deneyin.{Colors.ENDC}")
        conn.close()
        return

    # 2. Lig bazlı izin kontrolü (gemini_analysis == 1)
    if config.REQUIRE_LEAGUE_APPROVAL and not match_row["league_gemini_analysis"]:
        league_name = match_row["league_name"]
        print(f"\n{Colors.YELLOW}⚠️  LİG İZNİ GEREKLİ:{Colors.ENDC}")
        print(f"Karşılaşmanın bağlı olduğu '{league_name}' ligi için Gemini analizi şu an KAPALIDIR (gemini_analysis = 0).")
        enable_league = input(f"'{league_name}' ligi için Gemini analiz iznini AÇIP analizi başlatmak istiyor musunuz? (evet / hayır): ").strip().lower()
        if enable_league in ("evet", "yes", "e", "y"):
            database.set_league_gemini_analysis(conn, match_row["league_id"], True)
            conn.commit()
            print(f"{Colors.GREEN}✅ Lig izni aktif edildi.{Colors.ENDC}")
        else:
            print("Analiz iptal edildi.")
            conn.close()
            return

    match_id = match_row["id"]
    context = database.get_match_full_context(conn, match_id)
    conn.close()
    
    if not context:
        print(f"{Colors.RED}❌ Maç bağlamı oluşturulamadı.{Colors.ENDC}")
        return

    print(f"\n{Colors.YELLOW}🔮 Gemini AI ({config.GEMINI_MODEL}) Google Search ile canlı analiz yapıyor...{Colors.ENDC}")
    try:
        prediction = gemini_engine.generate_prediction(context, model_name=config.GEMINI_MODEL)
        with database.get_connection() as c:
            database.save_prediction(c, match_id, prediction)
            c.commit()
        
        display_prediction_card(context["match"], prediction, context.get("odds"))
    except Exception as e:
        print(f"{Colors.RED}❌ Gemini AI tahmini üretilirken hata oluştu: {e}{Colors.ENDC}")

def run_batch_prediction() -> None:
    # 1. Global config kontrolü
    if not config.ENABLE_GEMINI_ANALYSIS:
        print(f"\n{Colors.YELLOW}⚠️  BİLGİ: config.py dosyasında 'ENABLE_GEMINI_ANALYSIS = False' durumundadır.")
        confirm = input("Toplu Gemini analizini yine de başlatmak istiyor musunuz? (evet / hayır): ").strip().lower()
        if confirm not in ("evet", "yes", "e", "y"):
            print("İşlem iptal edildi.")
            return

    try:
        count_str = input("\nKaç adet bekleyen maç analiz edilsin? (Varsayılan: 3): ").strip()
        count = int(count_str) if count_str.isdigit() else 3
    except ValueError:
        count = 3

    conn = database.get_connection()
    # Sadece izinli liglerin maçlarını al
    matches = database.get_pending_matches(conn, limit=count, only_gemini_enabled=config.REQUIRE_LEAGUE_APPROVAL)
    conn.close()

    if not matches:
        print(f"\n{Colors.YELLOW}ℹ️ Gemini analizi açık (gemini_analysis = 1) olan bekleyen maç bulunamadı.{Colors.ENDC}")
        print(f"Lütfen 'Ligler & Gemini Analiz İzinleri' menüsünden analiz yapmak istediğiniz ligleri aktif edin.")
        return

    print(f"\n{Colors.YELLOW}🚀 {len(matches)} maç için toplu Gemini AI analizi başlatılıyor...{Colors.ENDC}")
    
    for idx, m in enumerate(matches, 1):
        match_id = m["id"]
        with database.get_connection() as c:
            context = database.get_match_full_context(c, match_id)
        if not context:
            continue

        m_info = context["match"]
        print(f"\n[{idx}/{len(matches)}] 🔍 [{m_info['league_name']}] {m_info['home_team']} vs {m_info['away_team']} analiz ediliyor...")
        
        try:
            prediction = gemini_engine.generate_prediction(context, model_name=config.GEMINI_MODEL)
            with database.get_connection() as c:
                database.save_prediction(c, match_id, prediction)
                c.commit()
            
            display_prediction_card(m_info, prediction, context.get("odds"))
        except Exception as e:
            print(f"{Colors.RED}❌ Analiz başarısız oldu: {e}{Colors.ENDC}")
            
        time.sleep(config.BATCH_REQUEST_DELAY)

    print(f"{Colors.GREEN}✅ Toplu tahmin işlemi tamamlandı!{Colors.ENDC}")

def generate_smart_coupon() -> None:
    print(f"\n{Colors.BOLD}🎟️  GÜNÜN BANKO KUPONU (AI SMART SLIP){Colors.ENDC}")
    conn = database.get_connection()
    high_conf = database.get_high_confidence_predictions(conn, min_confidence=config.MIN_CONFIDENCE_FOR_COUPON, limit=3)
    conn.close()

    if not high_conf:
        print(f"{Colors.YELLOW}ℹ️ Henüz güven skoru %{config.MIN_CONFIDENCE_FOR_COUPON} ve üzeri olan yeterli maç analiz edilmedi.")
        print(f"Lütfen önce maç analizi yapın.{Colors.ENDC}")
        return

    print(f"{Colors.GREEN}{'='*64}{Colors.ENDC}")
    print(f"   🏆 GÜNÜN BANKO SEÇİMLERİ (GÜVEN SKORU %{config.MIN_CONFIDENCE_FOR_COUPON}+) 🏆   ")
    print(f"{Colors.GREEN}{'='*64}{Colors.ENDC}")
    
    for idx, p in enumerate(high_conf, 1):
        print(f"\n{Colors.BOLD}Maç {idx}: [{p['code']}] {p['home_team']} vs {p['away_team']}{Colors.ENDC}")
        print(f"Lig: {p['league_name']} | Tarih: {p['match_date']} {p['match_time']}")
        print(f"🎯 Önerilen Tercih : {Colors.GREEN}{p['predicted_ft_result']}{Colors.ENDC} veya {Colors.CYAN}{p['predicted_over_under_3']} (3 Gol Sınırı){Colors.ENDC}")
        print(f"🤝 KG Tercihi       : {p['predicted_kg']}")
        print(f"🎯 Beklenen Skor    : {p['predicted_score']}")
        print(f"🌟 Güven Skoru      : {Colors.BOLD}%{p['confidence_score']}{Colors.ENDC}")
        print(f"💡 Neden?           : {Colors.DIM}{p['ai_analysis_summary'][:150]}...{Colors.ENDC}")

    print(f"{Colors.GREEN}{'-'*64}{Colors.ENDC}")
    print(f"✅ Kupon {len(high_conf)} maçtan derlenmiştir. Bol şanslar!")
    print(f"{Colors.GREEN}{'='*64}{Colors.ENDC}\n")

def view_predictions() -> None:
    conn = database.get_connection()
    preds = database.get_predictions(conn, limit=20)
    conn.close()

    if not preds:
        print(f"{Colors.YELLOW}ℹ️ Henüz kaydedilmiş bir yapay zeka tahmini bulunamadı.{Colors.ENDC}")
        return

    print(f"\n{Colors.CYAN}{'KOD':<8} {'KARŞILAŞMA':<35} {'İY':<6} {'MS':<6} {'3 GOL':<6} {'KG':<8} {'SKOR':<6} {'GÜVEN':<8}{Colors.ENDC}")
    print("-" * 90)
    for p in preds:
        teams = f"{p['home_team']} vs {p['away_team']}"
        print(f"{p['code']:<8} {teams[:33]:<35} {p['predicted_ht_result']:<6} {p['predicted_ft_result']:<6} {p['predicted_over_under_3']:<6} {p['predicted_kg']:<8} {p['predicted_score']:<6} %{p['confidence_score']:<6}")
    print("-" * 90)
    print(f"Toplam {len(preds)} tahmin listelendi.\n")

def enter_match_result() -> None:
    print(f"\n{Colors.BOLD}⚽ BİTEN MAÇ SKORU GİRİŞİ{Colors.ENDC}")
    code = input("Sonucu girilecek Maç Kodunu (C) girin: ").strip()
    if not code:
        return

    conn = database.get_connection()
    match_row = database.get_match_by_code(conn, code)
    if not match_row:
        print(f"{Colors.RED}❌ {code} kodlu maç veritabanında bulunamadı!{Colors.ENDC}")
        conn.close()
        return

    match_id = match_row["id"]
    print(f"👉 Karşılaşma: {match_row['home_team']} vs {match_row['away_team']}")
    
    try:
        ht_home = int(input("İlk Yarı Ev Sahibi Gol Sayısı (HT Home): ").strip())
        ht_away = int(input("İlk Yarı Deplasman Gol Sayısı (HT Away): ").strip())
        ft_home = int(input("Maç Sonu Ev Sahibi Gol Sayısı (FT Home): ").strip())
        ft_away = int(input("Maç Sonu Deplasman Gol Sayısı (FT Away): ").strip())
        
        database.save_result(conn, match_id, ht_home, ht_away, ft_home, ft_away)
        conn.commit()
        
        total_g = ft_home + ft_away
        ou_status = "ÜST (>= 4 Gol)" if total_g > 3 else "ALT (<= 3 Gol)"
        kg_status = "KG VAR" if (ft_home > 0 and ft_away > 0) else "KG YOK"
        
        print(f"\n{Colors.GREEN}✅ Maç Sonucu Başarıyla Kaydedildi!{Colors.ENDC}")
        print(f"   • İlk Yarı Skoru : {ht_home} - {ht_away}")
        print(f"   • Maç Sonu Skoru : {ft_home} - {ft_away}")
        print(f"   • 3 Gol Durumu   : {ou_status}")
        print(f"   • KG Durumu      : {kg_status}")
    except ValueError:
        print(f"{Colors.RED}❌ Lütfen geçerli tam sayı skorlar girin.{Colors.ENDC}")
    finally:
        conn.close()

def display_accuracy_report() -> None:
    print(f"\n{Colors.BOLD}📈 YAPAY ZEKA BAŞARI VE DOĞRULUK PERFORMANS RAPORU{Colors.ENDC}")
    conn = database.get_connection()
    metrics = database.calculate_prediction_accuracy(conn)
    conn.close()

    total = metrics["total_evaluated"]
    if total == 0:
        print(f"{Colors.YELLOW}ℹ️ Başarı oranı hesaplayabilmek için hem tahmini üretilmiş hem de maç sonucu girilmiş en az bir karşılaşma olmalıdır.")
        print(f"Lütfen biten bir maçın skorunu girin.{Colors.ENDC}")
        return

    print(f"{Colors.CYAN}{'='*64}{Colors.ENDC}")
    print(f"📊 Değerlendirilen Tamamlanmış Maç Sayısı: {Colors.BOLD}{total}{Colors.ENDC}")
    print(f"{Colors.CYAN}{'-'*64}{Colors.ENDC}")
    
    def bar(acc):
        filled = int(acc // 5)
        return f"[{'█' * filled}{'-' * (20 - filled)}]"

    print(f"⏱️  İlk Yarı (İY) Doğruluğu   : %{metrics['ht_accuracy']:<5} {bar(metrics['ht_accuracy'])} ({metrics['ht_correct']}/{total})")
    print(f"🏆 Maç Sonu (MS) Doğruluğu    : %{metrics['ft_accuracy']:<5} {bar(metrics['ft_accuracy'])} ({metrics['ft_correct']}/{total})")
    print(f"⚽ 3 Gol Alt/Üst Doğruluğu    : %{metrics['over_under_3_accuracy']:<5} {bar(metrics['over_under_3_accuracy'])} ({metrics['ou_correct']}/{total})")
    print(f"🤝 Karşılıklı Gol Doğruluğu   : %{metrics['kg_accuracy']:<5} {bar(metrics['kg_accuracy'])} ({metrics['kg_correct']}/{total})")
    print(f"🎯 Tam Skor İsabeti           : %{metrics['score_accuracy']:<5} {bar(metrics['score_accuracy'])} ({metrics['score_correct']}/{total})")
    print(f"{Colors.CYAN}{'-'*64}{Colors.ENDC}")
    
    overall = metrics["overall_accuracy"]
    o_color = Colors.GREEN if overall >= 70 else (Colors.YELLOW if overall >= 50 else Colors.RED)
    print(f"🌟 {Colors.BOLD}GENEL YAPAY ZEKA BAŞARI ORANI: {o_color}%{overall}{Colors.ENDC}")
    print(f"{Colors.CYAN}{'='*64}{Colors.ENDC}\n")

def display_system_stats() -> None:
    conn = database.get_connection()
    stats = database.get_db_stats(conn)
    conn.close()

    print(f"\n{Colors.BOLD}🗄️  SİSTEM VE VERİTABANI DURUMU{Colors.ENDC}")
    print("-" * 50)
    print(f"  • Gemini API Genel Durumu  : {'AÇIK' if config.ENABLE_GEMINI_ANALYSIS else 'KAPALI (config.py)'}")
    print(f"  • Kayıtlı Takım Sayısı     : {stats['teams']}")
    print(f"  • Kayıtlı Lig Sayısı       : {stats['leagues']}")
    print(f"  • İzin Verilen Lig Sayısı  : {stats['enabled_leagues']} (gemini_analysis=1)")
    print(f"  • Toplam Maç Sayısı        : {stats['matches']}")
    print(f"  • Bekleyen (PENDING) Maç   : {stats['pending']}")
    print(f"  • Analiz Edilen (ANALYZED) : {stats['analyzed']}")
    print(f"  • Biten (COMPLETED) Maç    : {stats['completed']}")
    print(f"  • Üretilen AI Tahmini      : {stats['predictions']}")
    print(f"  • Kaydedilen Maç Sonucu    : {stats['results']}")
    print("-" * 50)
    
    clean = input("Tüm veritabanını sıfırlamak istiyor musunuz? (evet / hayır): ").strip().lower()
    if clean in ("evet", "yes", "y"):
        database.clear_db()
        print(f"{Colors.GREEN}✅ Veritabanı başarıyla temizlendi ve sıfırlandı.{Colors.ENDC}")

def main_menu() -> None:
    database.init_db()
    while True:
        print_banner()
        print(f"{Colors.BOLD}Lütfen bir işlem seçiniz:{Colors.ENDC}")
        print("  1. 🔄 Güncel İddaa Bültenini Çek ve Kaydet")
        print("  2. 🔎 Bülteni ve Maçları Listele / Ara")
        print("  3. 🏆 Ligler & Gemini Analiz İzinleri Yönetimi (gemini_analysis)")
        print("  4. 📊 Takım İstatistiklerini Çek (H2H, Son Maçlar)")
        print("  5. 🔮 Tek Maç İçin Canlı Gemini AI Tahmini Al")
        print("  6. 🚀 Bekleyen Maçlar İçin Toplu Gemini AI Analizi")
        print("  7. 🎟️  Günün Banko Kuponunu Hazırla (AI Smart Slip)")
        print("  8. 📋 Daha Önce Yapılan AI Tahminlerini İncele")
        print("  9. ⚽ Biten Karşılaşma Skorunu Gir / Güncelle")
        print(" 10. 📈 Yapay Zeka Başarı ve Doğruluk Raporu (ROI)")
        print(" 11. ⚙️  Sistem Durumu ve Veritabanı Yönetimi")
        print(" 12. 🌐 Web Dashboard'u Başlat (Localhost)")
        print(" 13. 🚪 Çıkış")
        print("-" * 64)
        
        choice = input(f"{Colors.BOLD}Seçiminiz (1-13): {Colors.ENDC}").strip()
        
        if choice == "1":
            sync_bulletin()
        elif choice == "2":
            list_and_search_matches()
        elif choice == "3":
            manage_leagues()
        elif choice == "4":
            sync_match_stats()
        elif choice == "5":
            run_single_prediction()
        elif choice == "6":
            run_batch_prediction()
        elif choice == "7":
            generate_smart_coupon()
        elif choice == "8":
            view_predictions()
        elif choice == "9":
            enter_match_result()
        elif choice == "10":
            display_accuracy_report()
        elif choice == "11":
            display_system_stats()
        elif choice == "12":
            web_server.start_server()
        elif choice == "13":
            print(f"\n{Colors.GREEN}👋 Yapay Zeka Bahis Motoru kapatılıyor. Bol kazançlar dileriz!{Colors.ENDC}")
            sys.exit(0)
        else:
            print(f"\n{Colors.RED}⚠️ Geçersiz seçim! Lütfen 1-13 arasında bir sayı giriniz.{Colors.ENDC}")

        input(f"\n{Colors.DIM}Devam etmek için ENTER'a basın...{Colors.ENDC}")

if __name__ == "__main__":
    main_menu()
