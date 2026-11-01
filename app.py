import sys
import time
from typing import List, Dict, Any, Callable, Optional
import config
import database
import data_source
import gemini_engine
import match_data
import python_engine
import rules
import stats_sync

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

YES_ANSWERS = ("evet", "yes", "e", "y")

def ask_yes_no(prompt: str) -> bool:
    return input(prompt).strip().lower() in YES_ANSWERS

def confirm_when_gemini_disabled(detail_line: Optional[str], question: str, cancel_message: str) -> bool:
    """Genel Gemini anahtarı (panel ayarı / .env) kapalıysa kullanıcıya yine de devam etmek isteyip istemediğini sorar."""
    with database.db_session() as conn:
        if database.is_gemini_enabled(conn):
            return True
    print(f"\n{Colors.YELLOW}⚠️  BİLGİ: Gemini analizleri genel olarak KAPALI (Web › Lig Yönetimi › Ayarlar veya .env ENABLE_GEMINI_ANALYSIS).")
    if detail_line:
        print(f"{detail_line}{Colors.ENDC}")
    if ask_yes_no(question):
        return True
    print(cancel_message)
    return False

def print_banner():
    with database.db_session() as conn:
        stats = database.get_db_stats(conn)
        gemini_enabled = database.is_gemini_enabled(conn)

    api_status = f"{Colors.GREEN}AÇIK{Colors.ENDC}" if gemini_enabled else f"{Colors.YELLOW}BEKLEMEDE (KAPALI){Colors.ENDC}"

    print(f"{Colors.CYAN}{Colors.BOLD}")
    print("=" * 68)
    print("   ⚽  AI BETTING ENGINE - GOOGLE GEMINI 3.8 FLASH  🤖   ")
    print("      Futbol Analiz, İstatistik & Akıllı Bahis Motoru      ")
    print("=" * 68)
    print(f"{Colors.ENDC}", end="")
    print(f" ⚙️  Gemini API Durumu : {api_status} | İzinli Lig: {Colors.CYAN}{stats['enabled_leagues']}/{stats['leagues']}{Colors.ENDC} | Kayıtlı Maç: {Colors.CYAN}{stats['matches']}{Colors.ENDC}")
    print(f"{Colors.CYAN}{'-' * 68}{Colors.ENDC}\n")

def sync_bulletin() -> None:
    print(f"\n{Colors.YELLOW}🔄 İddaa Bülteni Çekiliyor ({data_source.source_name()})...{Colors.ENDC}")
    events = data_source.fetch_bulletin()
    if not events:
        print(f"{Colors.RED}❌ Bültenden maç verisi çekilemedi! İnternet bağlantınızı kontrol edin.{Colors.ENDC}")
        return

    print(f"📥 Toplam {len(events)} bülten etkinliği alındı. Hangi maçlar kaydedilsin?")
    print("  1. Sadece Futbol Maçları (Önerilen)")
    print("  2. Tüm Karşılaşmalar (Futbol + Basketbol)")
    sub_choice = input(f"{Colors.BOLD}Seçiminiz (1/2, Varsayılan: 1): {Colors.ENDC}").strip() or "1"
    allowed_types = ("1",) if sub_choice == "1" else ("1", "2")

    with database.db_session() as conn:
        # Önce tarihi geçmiş mevcut maçları match_completed=1 yap
        expired_cnt = database.update_expired_matches(conn)
        # Toplu ve yüksek performanslı senkronizasyon (Döngü içi SQL yok)
        sync_res = database.sync_bulletin_matches_and_odds(
            conn,
            events,
            allowed_types,
            default_league_gemini_analysis=config.DEFAULT_LEAGUE_GEMINI_ANALYSIS
        )

    print(f"\n{Colors.GREEN}{Colors.BOLD}✅ Bülten Senkronizasyonu Başarıyla Tamamlandı! (Toplu SQL İşlemi){Colors.ENDC}")
    print(f"   ➕ Yeni Eklenen Futbol Maçı     : {Colors.CYAN}{sync_res['new_football']}{Colors.ENDC}")
    if sub_choice != "1":
        print(f"   ➕ Yeni Eklenen Basketbol Maçı  : {Colors.CYAN}{sync_res['new_basketball']}{Colors.ENDC}")
    print(f"   ⏩ Atlanan (Zaten DB'de Kayıtlı): {Colors.YELLOW}{sync_res['skipped']}{Colors.ENDC}")
    if expired_cnt > 0:
        print(f"   ⏰ Tarihi Geçen/Biten Maç Sayısı: {Colors.BLUE}{expired_cnt}{Colors.ENDC}")
    print(f"   💰 Kaydedilen Yeni Bahis Oranı  : {Colors.CYAN}{sync_res['odds_total']}{Colors.ENDC}")

    # Bülten sonrası: istatistiği olmayan tüm maçların son maçları / H2H / puan durumu çekilir
    sync_match_stats()

def list_and_search_matches() -> None:
    print(f"\n{Colors.BOLD}🔎 MAÇ LİSTELEME VE ARAMA{Colors.ENDC}")
    query = input("Aramak istediğiniz Takım, Lig veya Maç Kodu (Boş = Tümü): ").strip()
    status_filter = input("Durum Filtresi (1: Tümü, 2: PENDING, 3: ANALYZED, 4: COMPLETED, Varsayılan: 1): ").strip()
    status = {"2": "PENDING", "3": "ANALYZED", "4": "COMPLETED"}.get(status_filter, "")

    with database.db_session() as conn:
        matches = database.search_matches(conn, query=query, status=status, limit=30)
    if not matches:
        print(f"{Colors.YELLOW}ℹ️ Kriterlere uygun maç bulunamadı.{Colors.ENDC}")
        return

    status_colors = {"ANALYZED": Colors.GREEN, "COMPLETED": Colors.BLUE}
    print(f"\n{Colors.CYAN}{'KOD':<8} {'TARİH / SAAT':<18} {'LİG':<22} {'KARŞILAŞMA':<30} {'DURUM':<10} {'BİTTİ Mİ?':<12} {'GEMINI':<8}{Colors.ENDC}")
    print("-" * 115)
    for m in matches:
        dt = f"{m['match_date']} {m['match_time']}"
        teams = f"{m['home_team']} vs {m['away_team']}"
        st_color = status_colors.get(m['status'], Colors.YELLOW)
        comp_str = f"{Colors.BLUE}EVET{Colors.ENDC}" if m['match_completed'] else f"{Colors.GREEN}HAYIR{Colors.ENDC}"
        gem_perm = f"{Colors.GREEN}AÇIK{Colors.ENDC}" if m['league_gemini_analysis'] else f"{Colors.DIM}KAPALI{Colors.ENDC}"
        print(f"{m['code']:<8} {dt:<18} {str(m['league_name'])[:20]:<22} {teams[:28]:<30} {st_color}{m['status']:<10}{Colors.ENDC} {comp_str:<21} {gem_perm:<8}")
    print("-" * 115)
    print(f"Toplam {len(matches)} maç gösteriliyor (Maks: 30).\n")

def _print_leagues(conn, query: str = "", only_enabled: bool = False) -> None:
    leagues = database.get_leagues(conn, query=query, only_enabled=only_enabled)
    if not leagues:
        print(f"{Colors.YELLOW}ℹ️ Kriterlere uygun lig bulunamadı.{Colors.ENDC}")
        return

    print(f"\n{Colors.CYAN}{'ID':<6} {'LİG ADI':<40} {'SPOR':<10} {'MAÇ':<6} {'GEMINI ANALİZİ'}{Colors.ENDC}")
    print("-" * 80)
    for l in leagues[:50]:
        perm_text = f"{Colors.GREEN}✅ AÇIK (True){Colors.ENDC}" if l["gemini_analysis"] else f"{Colors.RED}❌ KAPALI (False){Colors.ENDC}"
        print(f"{l['id']:<6} {str(l['name'])[:38]:<40} {l['sport_type']:<10} {l['match_count']:<6} {perm_text}")
    print("-" * 80)
    print(f"Toplam {len(leagues)} lig listelendi (Gösterilen maks: 50).\n")

def manage_leagues() -> None:
    with database.db_session() as conn:
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

            if choice == "1":
                _print_leagues(conn)
            elif choice == "2":
                _print_leagues(conn, query=input("Aramak istediğiniz Lig Adı: ").strip())
            elif choice == "3":
                _print_leagues(conn, only_enabled=True)

            elif choice == "4":
                lid_str = input("İznini değiştirmek istediğiniz Lig ID'sini girin: ").strip()
                if not lid_str.isdigit():
                    print(f"{Colors.RED}⚠️ Geçerli bir sayı giriniz.{Colors.ENDC}")
                    continue
                lid = int(lid_str)
                new_val = not database.is_league_analysis_enabled(conn, lid)
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

def _progress_line(done: int, total: int, started: float, current: str) -> str:
    width = 30
    filled = int(width * done / total) if total else width
    eta = int((time.time() - started) / done * (total - done)) if done else 0
    return (f"\r   [{'█' * filled}{'-' * (width - filled)}] {done}/{total} "
            f"(%{done * 100 // total if total else 100}) · kalan ~{eta // 60} dk {eta % 60:02d} sn · {current[:35]:<35}")

def sync_match_stats() -> None:
    """İstatistiği olmayan tüm bitmemiş maçların son maçlarını, H2H'sini ve puan durumunu ilerleme çubuğuyla çeker."""
    matches = stats_sync.pending_matches()
    if not matches:
        print(f"\n{Colors.GREEN}✅ Tüm bitmemiş maçların istatistikleri zaten mevcut.{Colors.ENDC}")
        return

    print(f"\n{Colors.YELLOW}📊 {len(matches)} maçın istatistikleri çekiliyor ({data_source.source_name()}). "
          f"Durdurmak için CTRL+C (çekilen veriler kalır).{Colors.ENDC}")
    started = time.time()
    try:
        counts = stats_sync.run(
            matches,
            on_progress=lambda p: print(_progress_line(p["processed"], len(matches), started, p["current"]), end="", flush=True)
        )
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}⏹  Durduruldu. Kalan maçlar bir sonraki çalıştırmada işlenir.{Colors.ENDC}")
        return
    print(f"\n{Colors.GREEN}✅ Tamamlandı: {counts['updated']} maçın istatistikleri kaydedildi, "
          f"{counts['empty']} maç için veri bulunamadı, {counts['errors']} hata.{Colors.ENDC}")

def _find_odds(odds: List[Dict[str, Any]], predicate: Callable[[Dict[str, Any]], bool]) -> Any:
    return next((o['odds_value'] for o in odds if predicate(o)), '-')

def display_prediction_card(match_info: Dict[str, Any], pred: Dict[str, Any], odds: List[Dict[str, Any]] = None):
    print(f"\n{Colors.GREEN}{'='*64}{Colors.ENDC}")
    print(f"{Colors.BOLD}🤖 GEMINI AI MAÇ TAHMİN KARTI{Colors.ENDC}")
    print(f"{Colors.CYAN}Maç Kodu   : {match_info.get('code')}{Colors.ENDC}")
    print(f"Karşılaşma : {Colors.BOLD}{match_info.get('home_team')} vs {match_info.get('away_team')}{Colors.ENDC}")
    print(f"Lig / Saat : {match_info.get('league_name')} | {match_info.get('match_date')} {match_info.get('match_time')}")
    print(f"{Colors.GREEN}{'-'*64}{Colors.ENDC}")

    basketball = rules.is_basketball(match_info.get('sport_type'))

    # Oran Özeti
    if odds:
        ms1, ms0, ms2 = (
            _find_odds(odds, lambda o, opt=opt: o['market_name'] == 'Maç Sonucu' and o['option_name'] == opt)
            for opt in ('MS 1', 'MS 0', 'MS 2')
        )
        if basketball:
            print(f"📌 İddaa Oranları : MS 1: {ms1} | MS 2: {ms2}")
        else:
            kg_v = _find_odds(odds, lambda o: o['market_name'] == 'Karşılıklı Gol' and 'VAR' in o['option_name'])
            u25 = _find_odds(odds, lambda o: '2.5' in o['market_name'] and 'Üst' in o['option_name'])
            print(f"📌 İddaa Oranları : MS 1: {ms1} | MS 0: {ms0} | MS 2: {ms2} | 2.5 Üst: {u25} | KG Var: {kg_v}")
        print(f"{Colors.GREEN}{'-'*64}{Colors.ENDC}")

    conf = pred.get('confidence_score', 0)
    conf_color = Colors.GREEN if conf >= 75 else (Colors.YELLOW if conf >= 60 else Colors.RED)

    print(f"⏱️  İlk Yarı Tahmini     : {Colors.BOLD}{pred.get('predicted_ht_result')}{Colors.ENDC}")
    print(f"🏆 Maç Sonu Tahmini     : {Colors.BOLD}{pred.get('predicted_ft_result')}{Colors.ENDC}")
    if basketball:
        print(f"🏀 Maç Sonu Toplam Sayı : {Colors.BOLD}{pred.get('predicted_total_points')}{Colors.ENDC}")
        if pred.get('predicted_over_under_3'):
            print(f"📊 Alt / Üst Tahmini    : {Colors.BOLD}{pred.get('predicted_over_under_3')}{Colors.ENDC}")
    else:
        print(f"⚽ 3 Gol Sınırı (Alt/Üst): {Colors.BOLD}{pred.get('predicted_over_under_3')}{Colors.ENDC} {Colors.DIM}(>=4 gol: ÜST, <=3 gol: ALT){Colors.ENDC}")
        print(f"🤝 Karşılıklı Gol       : {Colors.BOLD}{pred.get('predicted_kg')}{Colors.ENDC}")
    print(f"🎯 Tahmini Skor         : {Colors.BOLD}{pred.get('predicted_score')}{Colors.ENDC}")
    print(f"🌟 Güven Skoru          : {conf_color}{Colors.BOLD}%{conf}{Colors.ENDC}")
    best_bet = pred.get('best_bet')
    if best_bet:
        odds = f"oran {best_bet['odds']:.2f}, " if best_bet.get('odds') else ""
        print(f"⭐ En Güçlü Tercih     : {Colors.YELLOW}{Colors.BOLD}{best_bet.get('selection')}{Colors.ENDC} "
              f"({odds}%{best_bet.get('confidence')}) {Colors.DIM}{best_bet.get('reason') or ''}{Colors.ENDC}")
    alt_bet = pred.get('alt_bet')
    if alt_bet:
        odds = f"oran {alt_bet['odds']:.2f}, " if alt_bet.get('odds') else ""
        print(f"2. Tercih             : {Colors.BOLD}{alt_bet.get('selection')}{Colors.ENDC} ({odds}%{alt_bet.get('confidence')})")
    print(f"\n📝 {Colors.BOLD}Taktiksel Analiz Özeti:{Colors.ENDC}")
    print(f"   {Colors.CYAN}{pred.get('analysis_summary')}{Colors.ENDC}")
    print(f"{Colors.GREEN}{'='*64}{Colors.ENDC}\n")

def _predict_and_save(match_id: int) -> None:
    """
    Maç bağlamını yükler, Gemini tahmini üretir, kaydeder ve tahmin kartını gösterir.
    Son maçlar / H2H verisi yoksa maç Gemini'ye gönderilmez.
    """
    with database.db_session() as conn:
        context, missing = match_data.load_context_for_analysis(conn, match_id)
    if not context:
        print(f"{Colors.RED}❌ Maç bağlamı oluşturulamadı.{Colors.ENDC}")
        return
    if missing:
        print(f"{Colors.YELLOW}⏭  {match_data.missing_stats_message(missing)}{Colors.ENDC}")
        return

    prediction = gemini_engine.generate_prediction(context, model_name=config.GEMINI_MODEL)
    with database.db_session() as conn:
        database.save_prediction(conn, match_id, prediction)
        try:
            python_engine.generate_python_prediction(conn, match_id)
        except Exception:
            pass
    display_prediction_card(context["match"], prediction, context.get("odds"))

def run_single_prediction() -> None:
    # 1. Global config kontrolü
    if not confirm_when_gemini_disabled(
        "Yani Gemini analizleri şu an genel olarak duraklatılmıştır.",
        "Bu maç için yine de Gemini analizi almak istiyor musunuz? (evet / hayır): ",
        "Analiz iptal edildi."
    ):
        return

    code = input("\nTahmin yapılacak Maç Kodunu (C) girin: ").strip()
    if not code:
        return

    with database.db_session() as conn:
        match_row = database.get_match_by_code(conn, code)
        if not match_row:
            print(f"{Colors.RED}❌ {code} kodlu maç veritabanında bulunamadı! Önce bülteni çekmeyi deneyin.{Colors.ENDC}")
            return

        # 2. Lig bazlı izin kontrolü (gemini_analysis == 1)
        if config.REQUIRE_LEAGUE_APPROVAL and not match_row["league_gemini_analysis"]:
            league_name = match_row["league_name"]
            print(f"\n{Colors.YELLOW}⚠️  LİG İZNİ GEREKLİ:{Colors.ENDC}")
            print(f"Karşılaşmanın bağlı olduğu '{league_name}' ligi için Gemini analizi şu an KAPALIDIR (gemini_analysis = 0).")
            if not ask_yes_no(f"'{league_name}' ligi için Gemini analiz iznini AÇIP analizi başlatmak istiyor musunuz? (evet / hayır): "):
                print("Analiz iptal edildi.")
                return
            database.set_league_gemini_analysis(conn, match_row["league_id"], True)
            print(f"{Colors.GREEN}✅ Lig izni aktif edildi.{Colors.ENDC}")

    print(f"\n{Colors.YELLOW}🔮 Gemini AI ({config.GEMINI_MODEL}) Google Search ile canlı analiz yapıyor...{Colors.ENDC}")
    try:
        _predict_and_save(match_row["id"])
    except Exception as e:
        print(f"{Colors.RED}❌ Gemini AI tahmini üretilirken hata oluştu: {e}{Colors.ENDC}")

def run_batch_prediction() -> None:
    # 1. Global config kontrolü
    if not confirm_when_gemini_disabled(
        None,
        "Toplu Gemini analizini yine de başlatmak istiyor musunuz? (evet / hayır): ",
        "İşlem iptal edildi."
    ):
        return

    count_str = input("\nKaç adet bekleyen maç analiz edilsin? (Varsayılan: 3): ").strip()
    count = int(count_str) if count_str.isdigit() else 3

    # Sadece izinli liglerin maçlarını al
    with database.db_session() as conn:
        matches = database.get_pending_matches(conn, limit=count, only_gemini_enabled=config.REQUIRE_LEAGUE_APPROVAL)

    if not matches:
        print(f"\n{Colors.YELLOW}ℹ️ Gemini analizi açık (gemini_analysis = 1) olan bekleyen maç bulunamadı.{Colors.ENDC}")
        print(f"Lütfen 'Ligler & Gemini Analiz İzinleri' menüsünden analiz yapmak istediğiniz ligleri aktif edin.")
        return

    print(f"\n{Colors.YELLOW}🚀 {len(matches)} maç için toplu Gemini AI analizi başlatılıyor...{Colors.ENDC}")

    for idx, m in enumerate(matches, 1):
        print(f"\n[{idx}/{len(matches)}] 🔍 [{m['league_name']}] {m['home_team']} vs {m['away_team']} analiz ediliyor...")
        try:
            _predict_and_save(m["id"])
        except Exception as e:
            print(f"{Colors.RED}❌ Analiz başarısız oldu: {e}{Colors.ENDC}")
        time.sleep(config.BATCH_REQUEST_DELAY)

    print(f"{Colors.GREEN}✅ Toplu tahmin işlemi tamamlandı!{Colors.ENDC}")

def generate_smart_coupon() -> None:
    print(f"\n{Colors.BOLD}🎟️  GÜNÜN BANKO KUPONU (AI SMART SLIP){Colors.ENDC}")
    min_conf = config.MIN_CONFIDENCE_FOR_COUPON
    with database.db_session() as conn:
        high_conf = database.get_high_confidence_predictions(conn, min_confidence=min_conf, limit=3)

    if not high_conf:
        print(f"{Colors.YELLOW}ℹ️ Henüz güven skoru %{min_conf} ve üzeri olan yeterli maç analiz edilmedi.")
        print(f"Lütfen önce maç analizi yapın.{Colors.ENDC}")
        return

    print(f"{Colors.GREEN}{'='*64}{Colors.ENDC}")
    print(f"   🏆 GÜNÜN BANKO SEÇİMLERİ (GÜVEN SKORU %{min_conf}+) 🏆   ")
    print(f"{Colors.GREEN}{'='*64}{Colors.ENDC}")

    for idx, p in enumerate(high_conf, 1):
        print(f"\n{Colors.BOLD}Maç {idx}: [{p['code']}] {p['home_team']} vs {p['away_team']}{Colors.ENDC}")
        print(f"Lig: {p['league_name']} | Tarih: {p['match_date']} {p['match_time']}")
        if rules.is_basketball(p.get('sport_type')):
            print(f"🎯 Önerilen Tercih : {Colors.GREEN}{p['predicted_ft_result']}{Colors.ENDC}")
            print(f"🏀 Toplam Sayı      : {p['predicted_total_points']}")
        else:
            print(f"🎯 Önerilen Tercih : {Colors.GREEN}{p['predicted_ft_result']}{Colors.ENDC} veya {Colors.CYAN}{p['predicted_over_under_3']} (3 Gol Sınırı){Colors.ENDC}")
            print(f"🤝 KG Tercihi       : {p['predicted_kg']}")
        print(f"🎯 Beklenen Skor    : {p['predicted_score']}")
        print(f"🌟 Güven Skoru      : {Colors.BOLD}%{p['confidence_score']}{Colors.ENDC}")
        print(f"💡 Neden?           : {Colors.DIM}{(p['ai_analysis_summary'] or '')[:150]}...{Colors.ENDC}")

    print(f"{Colors.GREEN}{'-'*64}{Colors.ENDC}")
    print(f"✅ Kupon {len(high_conf)} maçtan derlenmiştir. Bol şanslar!")
    print(f"{Colors.GREEN}{'='*64}{Colors.ENDC}\n")

def view_predictions() -> None:
    with database.db_session() as conn:
        preds = database.get_predictions(conn, limit=20)

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

    with database.db_session() as conn:
        match_row = database.get_match_by_code(conn, code)
        if not match_row:
            print(f"{Colors.RED}❌ {code} kodlu maç veritabanında bulunamadı!{Colors.ENDC}")
            return

        basketball = rules.is_basketball(match_row["sport_type"])
        unit = "Sayı" if basketball else "Gol"
        print(f"👉 Karşılaşma: {match_row['home_team']} vs {match_row['away_team']}")
        try:
            ht_home = int(input(f"İlk Yarı Ev Sahibi {unit} (HT Home): ").strip())
            ht_away = int(input(f"İlk Yarı Deplasman {unit} (HT Away): ").strip())
            ft_home = int(input(f"Maç Sonu Ev Sahibi {unit}{' (uzatmalar dahil)' if basketball else ''} (FT Home): ").strip())
            ft_away = int(input(f"Maç Sonu Deplasman {unit}{' (uzatmalar dahil)' if basketball else ''} (FT Away): ").strip())
            if min(ht_home, ht_away, ft_home, ft_away) < 0:
                raise ValueError
        except ValueError:
            print(f"{Colors.RED}❌ Lütfen geçerli tam sayı skorlar girin.{Colors.ENDC}")
            return

        database.save_result(conn, match_row["id"], ht_home, ht_away, ft_home, ft_away)

    print(f"\n{Colors.GREEN}✅ Maç Sonucu Başarıyla Kaydedildi!{Colors.ENDC}")
    print(f"   • İlk Yarı Skoru : {ht_home} - {ht_away}")
    print(f"   • Maç Sonu Skoru : {ft_home} - {ft_away}")
    if basketball:
        print(f"   • Toplam Sayı    : {ft_home + ft_away}")
    else:
        ou_status = "ÜST (>= 4 Gol)" if rules.is_over_3_goals(ft_home, ft_away) else "ALT (<= 3 Gol)"
        print(f"   • 3 Gol Durumu   : {ou_status}")
        print(f"   • KG Durumu      : {rules.kg_label(ft_home, ft_away)}")

def display_accuracy_report() -> None:
    print(f"\n{Colors.BOLD}📈 YAPAY ZEKA BAŞARI VE DOĞRULUK PERFORMANS RAPORU{Colors.ENDC}")
    with database.db_session() as conn:
        metrics = database.calculate_prediction_accuracy(conn)

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

    # Her metrik kendi değerlendirilen maç sayısıyla gösterilir (Alt/Üst ve KG sadece futbolda)
    rows = [
        ("⏱️  İlk Yarı (İY) Doğruluğu   ", "ht_accuracy", "ht_correct", "ht_total"),
        ("🏆 Maç Sonu (MS) Doğruluğu    ", "ft_accuracy", "ft_correct", "ft_total"),
        ("⚽ 3 Gol Alt/Üst Doğruluğu    ", "over_under_3_accuracy", "ou_correct", "ou_total"),
        ("🤝 Karşılıklı Gol Doğruluğu   ", "kg_accuracy", "kg_correct", "kg_total"),
        ("🎯 Tam Skor İsabeti           ", "score_accuracy", "score_correct", "score_total"),
        ("⭐ En Güçlü Tercih İsabeti    ", "best_bet_accuracy", "best_bet_correct", "best_bet_total"),
        ("2. Tercih İsabeti             ", "alt_bet_accuracy", "alt_bet_correct", "alt_bet_total"),
    ]
    for label, acc_key, correct_key, total_key in rows:
        if not metrics[total_key]:
            continue
        acc = metrics[acc_key]
        print(f"{label}: %{acc:<5} {bar(acc)} ({metrics[correct_key]}/{metrics[total_key]})")
    if metrics.get("total_points_evaluated"):
        print(f"🏀 Basketbol Toplam Sayı      : ortalama {metrics['total_points_avg_diff']} sayı sapma ({metrics['total_points_evaluated']} maç)")
    print(f"{Colors.CYAN}{'-'*64}{Colors.ENDC}")

    overall = metrics["overall_accuracy"]
    o_color = Colors.GREEN if overall >= 70 else (Colors.YELLOW if overall >= 50 else Colors.RED)
    print(f"🌟 {Colors.BOLD}GENEL YAPAY ZEKA BAŞARI ORANI: {o_color}%{overall}{Colors.ENDC}")
    print(f"{Colors.CYAN}{'='*64}{Colors.ENDC}\n")

def display_system_stats() -> None:
    with database.db_session() as conn:
        stats = database.get_db_stats(conn)
        gemini = database.get_gemini_setting(conn)

    gemini_source = "Panel ayarı" if gemini["source"] == "panel" else ".env varsayılanı"
    print(f"\n{Colors.BOLD}🗄️  SİSTEM VE VERİTABANI DURUMU{Colors.ENDC}")
    print("-" * 50)
    print(f"  • Gemini API Genel Durumu  : {'AÇIK' if gemini['enabled'] else 'KAPALI'} ({gemini_source})")
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

    # Geri dönüşü olmayan işlem: tek harfli 'e' kısayolu bilerek kabul edilmez
    clean = input("Tüm veritabanını sıfırlamak istiyor musunuz? (evet / hayır): ").strip().lower()
    if clean in ("evet", "yes", "y"):
        database.clear_db()
        print(f"{Colors.GREEN}✅ Veritabanı başarıyla temizlendi ve sıfırlandı.{Colors.ENDC}")

def start_web_dashboard() -> None:
    # web_server import edildiğinde Flask uygulaması oluşturulur; sadece ihtiyaç olduğunda yükle
    import web_server
    web_server.start_server()

def exit_app() -> None:
    print(f"\n{Colors.GREEN}👋 Yapay Zeka Bahis Motoru kapatılıyor. Bol kazançlar dileriz!{Colors.ENDC}")
    sys.exit(0)

MENU_ITEMS = [
    ("🔄 Güncel İddaa Bültenini Çek ve Kaydet", sync_bulletin),
    ("🔎 Bülteni ve Maçları Listele / Ara", list_and_search_matches),
    ("🏆 Ligler & Gemini Analiz İzinleri Yönetimi (gemini_analysis)", manage_leagues),
    ("📊 Takım İstatistiklerini Çek (H2H, Son Maçlar)", sync_match_stats),
    ("🔮 Tek Maç İçin Canlı Gemini AI Tahmini Al", run_single_prediction),
    ("🚀 Bekleyen Maçlar İçin Toplu Gemini AI Analizi", run_batch_prediction),
    ("🎟️  Günün Banko Kuponunu Hazırla (AI Smart Slip)", generate_smart_coupon),
    ("📋 Daha Önce Yapılan AI Tahminlerini İncele", view_predictions),
    ("⚽ Biten Karşılaşma Skorunu Gir / Güncelle", enter_match_result),
    ("📈 Yapay Zeka Başarı ve Doğruluk Raporu (ROI)", display_accuracy_report),
    ("⚙️  Sistem Durumu ve Veritabanı Yönetimi", display_system_stats),
    ("🌐 Web Dashboard'u Başlat (Localhost)", start_web_dashboard),
    ("🚪 Çıkış", exit_app),
]

def main_menu() -> None:
    database.init_db()
    menu_count = len(MENU_ITEMS)
    while True:
        print_banner()
        print(f"{Colors.BOLD}Lütfen bir işlem seçiniz:{Colors.ENDC}")
        for number, (label, _) in enumerate(MENU_ITEMS, 1):
            print(f"{number:>3}. {label}")
        print("-" * 64)

        choice = input(f"{Colors.BOLD}Seçiminiz (1-{menu_count}): {Colors.ENDC}").strip()
        if choice.isdigit() and 1 <= int(choice) <= menu_count:
            MENU_ITEMS[int(choice) - 1][1]()
        else:
            print(f"\n{Colors.RED}⚠️ Geçersiz seçim! Lütfen 1-{menu_count} arasında bir sayı giriniz.{Colors.ENDC}")

        input(f"\n{Colors.DIM}Devam etmek için ENTER'a basın...{Colors.ENDC}")

if __name__ == "__main__":
    main_menu()
