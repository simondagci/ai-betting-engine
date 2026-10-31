import unittest
import os
import tempfile
import threading
import time
from unittest import mock

TEST_DB = os.path.join(tempfile.gettempdir(), "test_betting_engine.db")
# config import edilmeden önce: varsayılan DB yolunu kullanan kodlar (örn. web_server) gerçek veritabanına dokunmasın
os.environ["DB_FILE_PATH"] = TEST_DB

import config
import database
import nesine_api
import data_source
import iddaa_api
import stats_sync
import prediction_batch
import results_sync
import coupon
import gemini_engine
import rules

class TestBettingEngine(unittest.TestCase):

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        database.init_db(TEST_DB)

    def tearDown(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def test_database_initialization(self):
        conn = database.get_connection(TEST_DB)
        cursor = conn.cursor()
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row["name"] for row in cursor.fetchall()]
        
        self.assertIn("teams", tables)
        self.assertIn("leagues", tables)
        self.assertIn("matches", tables)
        self.assertIn("odds", tables)
        self.assertIn("match_stats", tables)
        self.assertIn("predictions", tables)
        self.assertIn("results", tables)
        
        # leagues tablosundaki gemini_analysis sütununu kontrol et
        cursor.execute("PRAGMA table_info(leagues);")
        columns = [row["name"] for row in cursor.fetchall()]
        self.assertIn("gemini_analysis", columns)

        # matches tablosundaki match_completed sütununu kontrol et
        cursor.execute("PRAGMA table_info(matches);")
        m_columns = [row["name"] for row in cursor.fetchall()]
        self.assertIn("match_completed", m_columns)
        conn.close()

    def test_league_gemini_analysis_permission(self):
        conn = database.get_connection(TEST_DB)
        
        # 1. İki farklı lig oluştur: Lig A (kapalı: 0), Lig B (açık: 1)
        l_a = database.get_or_create_league(conn, "Lig A (Kapalı)", "FOOTBALL", gemini_analysis=0)
        l_b = database.get_or_create_league(conn, "Lig B (Açık)", "FOOTBALL", gemini_analysis=1)
        
        self.assertFalse(database.is_league_analysis_enabled(conn, l_a))
        self.assertTrue(database.is_league_analysis_enabled(conn, l_b))
        
        # 2. İki gelecekteki maçı kaydet
        database.save_match(conn, "CODE_A", "Team A1", "Team A2", "Lig A (Kapalı)", "01.01.2099", "20:00")
        database.save_match(conn, "CODE_B", "Team B1", "Team B2", "Lig B (Açık)", "01.01.2099", "21:00")
        
        # 3. Bekleyen maçları filtreli ve filtresiz çek
        all_pending = database.get_pending_matches(conn, only_gemini_enabled=False)
        self.assertEqual(len(all_pending), 2)
        
        only_gemini = database.get_pending_matches(conn, only_gemini_enabled=True)
        self.assertEqual(len(only_gemini), 1)
        self.assertEqual(only_gemini[0]["code"], "CODE_B")
        self.assertEqual(only_gemini[0]["league_name"], "Lig B (Açık)")
        
        # 4. Lig A'nın iznini aç
        database.set_league_gemini_analysis(conn, l_a, True)
        self.assertTrue(database.is_league_analysis_enabled(conn, l_a))
        
        only_gemini_after = database.get_pending_matches(conn, only_gemini_enabled=True)
        self.assertEqual(len(only_gemini_after), 2)
        
        # 5. Topluca tüm izinleri kapat
        database.set_all_leagues_gemini_analysis(conn, False)
        only_gemini_closed = database.get_pending_matches(conn, only_gemini_enabled=True)
        self.assertEqual(len(only_gemini_closed), 0)
        conn.close()

    def test_skip_existing_matches(self):
        conn = database.get_connection(TEST_DB)
        
        # İlk kayıt
        id1 = database.save_match(conn, "DUP_100", "Team Orig 1", "Team Orig 2", "Test Lig", "01.01.2099", "19:00")
        self.assertIsNotNone(id1)
        
        # Tekrar aynı kodla kaydetmeyi dene (tekrar almaya gerek yok)
        id2 = database.save_match(conn, "DUP_100", "Team Changed 1", "Team Changed 2", "Different Lig", "01.01.2099", "19:00")
        self.assertEqual(id1, id2)
        
        # Orijinal takım adının korunduğunu doğrula
        match_data = database.get_match_by_code(conn, "DUP_100")
        self.assertEqual(match_data["home_team"], "Team Orig 1")
        
        codes = database.get_existing_match_codes(conn)
        self.assertIn("DUP_100", codes)
        conn.close()

    def test_match_completed_for_past_and_future_dates(self):
        conn = database.get_connection(TEST_DB)
        
        # 1. Gelecekteki maç: match_completed = 0 olmalı
        fut_id = database.save_match(conn, "FUTURE_1", "Real Madrid", "Barcelona", "La Liga", "01.01.2099", "20:00")
        fut_m = database.get_match_by_code(conn, "FUTURE_1")
        self.assertEqual(fut_m["match_completed"], 0)
        self.assertEqual(fut_m["status"], "PENDING")
        
        # 2. Geçmişteki maç: match_completed = 1 olmalı
        past_id = database.save_match(conn, "PAST_1", "Milan", "Inter", "Serie A", "01.01.2020", "20:00")
        past_m = database.get_match_by_code(conn, "PAST_1")
        self.assertEqual(past_m["match_completed"], 1)
        self.assertEqual(past_m["status"], "COMPLETED")
        
        # 3. get_pending_matches sadece gelecekteki (tamamlanmamış) maçı getirmeli
        pending = database.get_pending_matches(conn)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["code"], "FUTURE_1")
        
        # 4. save_result çağrıldığında match_completed kesinlikle 1 olmalı
        database.save_result(conn, fut_id, ht_home=1, ht_away=0, ft_home=2, ft_away=1)
        fut_after_result = database.get_match_by_code(conn, "FUTURE_1")
        self.assertEqual(fut_after_result["match_completed"], 1)
        self.assertEqual(fut_after_result["status"], "COMPLETED")
        conn.close()

    def test_match_completed_150_minutes_rule(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        conn = database.get_connection(TEST_DB)
        src_tz = ZoneInfo(config.SOURCE_TIMEZONE)
        now_tr = datetime.now(src_tz)
        
        # Maç 60 dakika önce başladı (henüz 150 dk dolmadı): match_completed = 0 olmalı
        started_60m_ago = now_tr - timedelta(minutes=60)
        d_str1 = started_60m_ago.strftime("%d.%m.%Y")
        t_str1 = started_60m_ago.strftime("%H:%M")
        database.save_match(conn, "LIVE_60M", "Team Live1", "Team Live2", "Live Lig", d_str1, t_str1)
        row1 = database.get_match_by_code(conn, "LIVE_60M")
        self.assertEqual(row1["match_completed"], 0, "60 dk geçmiş maç 150 dk dolmadığı için henüz tamamlanmamış sayılmalı")
        
        # Maç 160 dakika önce başladı (150 dk doldu): match_completed = 1 olmalı
        started_160m_ago = now_tr - timedelta(minutes=160)
        d_str2 = started_160m_ago.strftime("%d.%m.%Y")
        t_str2 = started_160m_ago.strftime("%H:%M")
        database.save_match(conn, "OVER_160M", "Team Fin1", "Team Fin2", "Past Lig", d_str2, t_str2)
        row2 = database.get_match_by_code(conn, "OVER_160M")
        self.assertEqual(row2["match_completed"], 1, "160 dk geçmiş maç 150 dk dolduğu için tamamlanmış sayılmalı")
        conn.close()

    def test_save_and_retrieve_match(self):
        conn = database.get_connection(TEST_DB)
        match_id = database.save_match(
            conn, 
            code="123456", 
            home_team="Galatasaray", 
            away_team="Fenerbahçe", 
            league_name="Süper Lig", 
            match_date="30.10.2099", 
            match_time="20:00"
        )
        self.assertIsNotNone(match_id)
        
        odds_list = [
            {"market_type": 1, "market_name": "Maç Sonucu", "option_name": "MS 1", "odds_value": 2.10},
            {"market_type": 1, "market_name": "Maç Sonucu", "option_name": "MS 2", "odds_value": 3.20}
        ]
        database.save_odds(conn, match_id, odds_list)
        
        stats_h2h = [{"match": "Galatasaray 2-1 Fenerbahçe"}]
        database.save_match_stats(conn, match_id, h2h_data=stats_h2h, home_last=[], away_last=[], missing=[], league_table=[])
        
        full_context = database.get_match_full_context(conn, match_id)
        self.assertIsNotNone(full_context)
        self.assertEqual(full_context["match"]["home_team"], "Galatasaray")
        self.assertEqual(full_context["match"]["away_team"], "Fenerbahçe")
        self.assertEqual(len(full_context["odds"]), 2)
        
        match_by_code = database.get_match_by_code(conn, "123456")
        self.assertIsNotNone(match_by_code)
        self.assertEqual(match_by_code["code"], "123456")
        conn.close()

    def test_three_goal_rule_in_results(self):
        conn = database.get_connection(TEST_DB)
        match_id = database.save_match(conn, "999", "TeamA", "TeamB", "LeagueX", "28.10.2099", "18:00")
        
        # Case 1: Total goals = 3 (1-2) -> Should be Under 3 (is_over_3_goals = 0)
        database.save_result(conn, match_id, ht_home=1, ht_away=0, ft_home=1, ft_away=2)
        cursor = conn.cursor()
        cursor.execute("SELECT is_over_3_goals, is_kg_var FROM results WHERE match_id = ?", (match_id,))
        res = cursor.fetchone()
        self.assertEqual(res["is_over_3_goals"], 0, "3 gol Toplam = 3 olduğu için 3 Gol Alt olmalı")
        self.assertEqual(res["is_kg_var"], 1)
        
        # Case 2: Total goals = 4 (2-2) -> Should be Over 3 (is_over_3_goals = 1)
        database.save_result(conn, match_id, ht_home=1, ht_away=1, ft_home=2, ft_away=2)
        cursor = conn.cursor()
        cursor.execute("SELECT is_over_3_goals, is_kg_var FROM results WHERE match_id = ?", (match_id,))
        res2 = cursor.fetchone()
        self.assertEqual(res2["is_over_3_goals"], 1, "4 gol Toplam > 3 olduğu için 3 Gol Üst olmalı")
        conn.close()

    def test_prediction_accuracy_calculation(self):
        conn = database.get_connection(TEST_DB)
        
        # Match 1: Accurate prediction
        m1_id = database.save_match(conn, "M1", "Real Madrid", "Barcelona", "La Liga", "29.10.2099", "22:00")
        database.save_prediction(conn, m1_id, {
            "predicted_ht_result": "İY 1",
            "predicted_ft_result": "MS 1",
            "predicted_over_under_3": "ÜST",
            "predicted_kg": "KG VAR",
            "predicted_score": "3-1",
            "confidence_score": 85,
            "analysis_summary": "Test analiz 1"
        })
        database.save_result(conn, m1_id, ht_home=1, ht_away=0, ft_home=3, ft_away=1)
        
        # Match 2: Partial accurate prediction
        m2_id = database.save_match(conn, "M2", "Arsenal", "Chelsea", "Premier League", "29.10.2099", "19:00")
        database.save_prediction(conn, m2_id, {
            "predicted_ht_result": "İY 0",
            "predicted_ft_result": "MS 1",
            "predicted_over_under_3": "ALT",
            "predicted_kg": "KG YOK",
            "predicted_score": "2-0",
            "confidence_score": 75,
            "analysis_summary": "Test analiz 2"
        })
        # Actual: HT 0-0 (İY 0 match!), FT 1-1 (MS 0 mismatch, total=2 ALT match!, KG VAR mismatch)
        database.save_result(conn, m2_id, ht_home=0, ht_away=0, ft_home=1, ft_away=1)

        accuracy = database.calculate_prediction_accuracy(conn)
        self.assertEqual(accuracy["total_evaluated"], 2)
        self.assertEqual(accuracy["ht_correct"], 2)  # Both HT correct
        self.assertEqual(accuracy["ht_accuracy"], 100.0)
        self.assertEqual(accuracy["ft_correct"], 1)  # Only M1 FT correct
        self.assertEqual(accuracy["ft_accuracy"], 50.0)
        self.assertEqual(accuracy["ou_correct"], 2)  # Both Over/Under correct (M1 4 goals ÜST, M2 2 goals ALT)
        self.assertEqual(accuracy["over_under_3_accuracy"], 100.0)
        self.assertEqual(accuracy["score_correct"], 1) # M1 score 3-1 exact match!
        conn.close()

    def test_extract_odds_from_event(self):
        dummy_event = {
            "MA": [
                {
                    "MT": 1, "MTID": 1, "MST": 1,
                    "OCA": [{"N": 1, "O": 1.75}, {"N": 2, "O": 3.40}, {"N": 3, "O": 4.10}]
                },
                {
                    "MT": 2, "MTID": 12, "MST": 101, "SOV": 2.5, "BM": 1,
                    "OCA": [{"N": 1, "O": 1.85}, {"N": 2, "O": 1.70}]
                },
                {
                    "MT": 2, "MTID": 38, "MST": 89,
                    "OCA": [{"N": 1, "O": 1.65}, {"N": 2, "O": 1.90}]
                }
            ]
        }
        odds = nesine_api.extract_odds_from_event(dummy_event)
        self.assertEqual(len(odds), 7)
        ms_1 = next(o for o in odds if o["market_name"] == "Maç Sonucu" and o["option_name"] == "MS 1")
        self.assertEqual(ms_1["odds_value"], 1.75)
        
        alt = next(o for o in odds if o["market_name"] == "2.5 Gol Alt/Üst" and o["option_name"] == "2.5 Alt")
        self.assertEqual(alt["odds_value"], 1.85)

        kg_var = next(o for o in odds if o["market_name"] == "Karşılıklı Gol" and o["option_name"] == "KG VAR")
        self.assertEqual(kg_var["odds_value"], 1.65)

    def test_prompt_builder(self):
        dummy_context = {
            "match": {
                "home_team": "Real Madrid",
                "away_team": "Barcelona",
                "league_name": "La Liga",
                "match_date": "01.11.2099",
                "match_time": "22:00"
            },
            "odds": [{"market_name": "Maç Sonucu", "option_name": "MS 1", "odds_value": 1.95}],
            "h2h": [{"result": "3-2"}],
            "home_last": [{"result": "2-0"}],
            "away_last": [{"result": "1-1"}],
            "missing": [{"Player": "Benzema"}]
        }
        prompt = gemini_engine.build_prompt(dummy_context)
        self.assertIn("Real Madrid", prompt)
        self.assertIn("Barcelona", prompt)
        self.assertIn("KESİNLİKLE 3 GOL KURALI", prompt)

    def test_config_defaults(self):
        self.assertFalse(config.ENABLE_GEMINI_ANALYSIS)
        self.assertTrue(config.REQUIRE_LEAGUE_APPROVAL)
        self.assertEqual(config.DEFAULT_LEAGUE_GEMINI_ANALYSIS, 0)
        self.assertEqual(config.GEMINI_MODEL, "gemini-3.8-flash")

    def test_bulk_sync_bulletin_matches_and_odds(self):
        conn = database.get_connection(TEST_DB)
        dummy_events = [
            {
                "C": "BULK_1", "HN": "Bulk Home 1", "AN": "Bulk Away 1", "league_name": "Bulk Lig A",
                "D": "01.12.2099", "T": "20:00", "TYPE": "1",
                "MA": [{"MT": 1, "MTID": 1, "MST": 1, "OCA": [{"N": 1, "O": 1.50}, {"N": 2, "O": 3.50}]}]
            },
            {
                "C": "BULK_2", "HN": "Bulk Home 2", "AN": "Bulk Away 2", "league_name": "Bulk Lig B",
                "D": "01.12.2099", "T": "21:00", "TYPE": "1",
                "MA": [{"MT": 2, "MTID": 38, "MST": 89, "OCA": [{"N": 1, "O": 1.70}, {"N": 2, "O": 1.95}]}]
            }
        ]
        
        # 1. İlk senkronizasyon: 2 yeni maç, 0 atlama
        res1 = database.sync_bulletin_matches_and_odds(conn, dummy_events, allowed_types=("1",))
        self.assertEqual(res1["new_football"], 2)
        self.assertEqual(res1["skipped"], 0)
        self.assertEqual(res1["odds_total"], 4)
        
        # 2. İkinci senkronizasyon (aynı verilerle): 0 yeni maç, 2 atlama
        res2 = database.sync_bulletin_matches_and_odds(conn, dummy_events, allowed_types=("1",))
        self.assertEqual(res2["new_football"], 0)
        self.assertEqual(res2["skipped"], 2)
        self.assertEqual(res2["odds_total"], 0)
        conn.close()

    def test_timezone_conversion(self):
        # 18:00 TRT (UTC+3) -> 16:00 CET (UTC+1)
        d, t = database.convert_tr_to_local_time("29.10.2026", "18:00")
        self.assertEqual(d, "29.10.2026")
        self.assertEqual(t, "16:00")
        
        # Gece yarısı geçiş testi: 01:00 TRT -> önceki gün 23:00 CET
        d2, t2 = database.convert_tr_to_local_time("30.10.2026", "01:00")
        self.assertEqual(d2, "29.10.2026")
        self.assertEqual(t2, "23:00")

    def test_chronological_match_sorting(self):
        conn = database.get_connection(TEST_DB)
        # Karışık tarihler ekle
        database.save_match(conn, "C1", "T1", "T2", "Lig", "05.11.2099", "20:00")
        database.save_match(conn, "C2", "T3", "T4", "Lig", "01.12.2099", "15:00")
        database.save_match(conn, "C3", "T5", "T6", "Lig", "28.10.2099", "18:00")
        database.save_match(conn, "C4", "T7", "T8", "Lig", "30.10.2099", "21:00")
        
        matches = database.get_unfinished_matches(conn)
        codes = [m["code"] for m in matches]
        # Beklenen kronolojik sıra: 28.10 -> 30.10 -> 05.11 -> 01.12
        self.assertEqual(codes, ["C3", "C4", "C1", "C2"])
        conn.close()

    def test_completed_matches_and_days_filter(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        conn = database.get_connection(TEST_DB)
        src_tz = ZoneInfo(config.SOURCE_TIMEZONE)
        now_tr = datetime.now(src_tz)

        # 1. Dün oynanmış maç (1 gün önce)
        d_1day = (now_tr - timedelta(days=1)).strftime("%d.%m.%Y")
        m1_id = database.save_match(conn, "PAST_1D", "Team P1", "Team P2", "Lig", d_1day, "18:00")
        database.save_result(conn, m1_id, ht_home=1, ht_away=0, ft_home=2, ft_away=0)

        # 2. 5 gün önce oynanmış maç
        d_5days = (now_tr - timedelta(days=5)).strftime("%d.%m.%Y")
        m2_id = database.save_match(conn, "PAST_5D", "Team P3", "Team P4", "Lig", d_5days, "18:00")
        database.save_result(conn, m2_id, ht_home=0, ht_away=0, ft_home=1, ft_away=1)

        # 3. Gelecekteki maç (henüz oynanmadı)
        database.save_match(conn, "FUT_MATCH", "Team F1", "Team F2", "Lig", "01.01.2099", "20:00")

        # Son 2 günde bitenler sorgusu
        last_2d = database.get_completed_matches(conn, days_limit=2)
        codes_2d = [m["code"] for m in last_2d]
        self.assertIn("PAST_1D", codes_2d)
        self.assertNotIn("PAST_5D", codes_2d)
        self.assertNotIn("FUT_MATCH", codes_2d)
        self.assertEqual(len(last_2d), 1)

        # Tüm bitenler sorgusu (limit yok)
        all_comp = database.get_completed_matches(conn, days_limit=None)
        codes_all = [m["code"] for m in all_comp]
        self.assertIn("PAST_1D", codes_all)
        self.assertIn("PAST_5D", codes_all)
        self.assertNotIn("FUT_MATCH", codes_all)
        self.assertEqual(len(all_comp), 2)
        conn.close()

    def test_db_session_commits_and_rolls_back(self):
        with database.db_session(TEST_DB) as conn:
            database.save_match(conn, "SESSION_OK", "S1", "S2", "Lig", "01.01.2099", "20:00")

        with self.assertRaises(RuntimeError):
            with database.db_session(TEST_DB) as conn:
                database.save_match(conn, "SESSION_FAIL", "S3", "S4", "Lig", "01.01.2099", "20:00")
                raise RuntimeError("test")

        with database.db_session(TEST_DB) as conn:
            codes = database.get_existing_match_codes(conn)
        self.assertIn("SESSION_OK", codes)
        self.assertNotIn("SESSION_FAIL", codes)

class TestRules(unittest.TestCase):

    def test_result_labels(self):
        self.assertEqual(rules.result_label("MS", 2, 1), "MS 1")
        self.assertEqual(rules.result_label("MS", 1, 1), "MS 0")
        self.assertEqual(rules.result_label("İY", 0, 1), "İY 2")

    def test_three_goal_boundary(self):
        self.assertEqual(rules.over_under_label(2, 1), "ALT")
        self.assertEqual(rules.over_under_label(3, 0), "ALT")
        self.assertEqual(rules.over_under_label(2, 2), "ÜST")
        self.assertEqual(rules.over_under_label(4, 0), "ÜST")

    def test_kg(self):
        self.assertEqual(rules.kg_label(1, 1), "KG VAR")
        self.assertEqual(rules.kg_label(2, 0), "KG YOK")

    def test_flexible_result_matching(self):
        self.assertTrue(rules.is_result_prediction_correct("MS 1", "ms 1", "MS"))
        self.assertTrue(rules.is_result_prediction_correct("MS 1", "MS1", "MS"))
        self.assertFalse(rules.is_result_prediction_correct("MS 1", "MS 2", "MS"))
        self.assertFalse(rules.is_result_prediction_correct("MS 1", None, "MS"))

    def test_score_matching(self):
        self.assertTrue(rules.is_score_prediction_correct(2, 1, " 2 - 1 "))
        self.assertFalse(rules.is_score_prediction_correct(2, 1, "1-2"))

class TestNesineApi(unittest.TestCase):

    def test_extract_odds_labels_and_fallbacks(self):
        event = {
            "MA": [
                # İlk yarı: bilinmeyen seçenek numarası şablonla etiketlenir
                {"MT": 3, "MTID": 7, "OCA": [{"N": 1, "O": 2.0}, {"N": 4, "O": 9.0}]},
                # Diğer toplam gol baremi
                {"MT": 4, "MST": 101, "SOV": 3.5, "OCA": [{"N": 1, "O": 1.4}, {"N": 2, "O": 2.8}]},
                # Korner
                {"MT": 5, "MTID": 216, "SOV": 9.5, "OCA": [{"N": 2, "O": 1.9}]},
                # Genel fallback: bültendeki isimler kullanılır
                {"MT": 6, "MN": "Handikap", "OCA": [{"N": 1, "O": 3.0, "ON": "H1"}, {"N": 2, "O": 2.0}]},
                # Tanınmayan ve isimsiz market atlanır
                {"MT": 7, "OCA": [{"N": 1, "O": 5.0}]}
            ]
        }
        odds = [(o["market_name"], o["option_name"], o["odds_value"]) for o in nesine_api.extract_odds_from_event(event)]
        self.assertEqual(odds, [
            ("İlk Yarı Sonucu", "İY 1", 2.0),
            ("İlk Yarı Sonucu", "İY 4", 9.0),
            ("3.5 Gol Alt/Üst", "3.5 Alt", 1.4),
            ("3.5 Gol Alt/Üst", "3.5 Üst", 2.8),
            ("Toplam Korner 9.5 Alt/Üst", "Korner 9.5 Üst", 1.9),
            ("Handikap", "H1", 3.0),
            ("Handikap", "Seçenek 2", 2.0),
        ])

    def test_extract_odds_without_markets(self):
        self.assertEqual(nesine_api.extract_odds_from_event({}), [])
        self.assertEqual(nesine_api.extract_odds_from_event({"MA": None}), [])

    def test_clean_nesine_data_json(self):
        raw = '{"Data": "{\\"LeagueTable\\":[{\\"Rank\\":1}]}"}'
        self.assertEqual(nesine_api._clean_nesine_data_json(raw), {"LeagueTable": [{"Rank": 1}]})
        self.assertEqual(nesine_api._clean_nesine_data_json("<!DOCTYPE html><html></html>"), {})
        self.assertEqual(nesine_api._clean_nesine_data_json(""), {})

    def test_fetch_match_stats_maps_to_save_match_stats_params(self):
        responses = {
            "lastmatches": {"HomeTeamMatches": [{"id": 1}], "AwayTeamMatches": [{"id": 2}]},
            "competitionhistory?filter=false": {"Competitions": []},
            "missingplayers": {"HomeTeamMissingPlayers": []},
            "leaguetable": {"LeagueTable": []},
        }
        with mock.patch.object(nesine_api, "_fetch_stats", side_effect=lambda code, endpoint: responses[endpoint]):
            stats = nesine_api.fetch_match_stats("123")

        self.assertEqual(stats["home_last"], [{"id": 1}])
        self.assertEqual(stats["away_last"], [{"id": 2}])
        self.assertEqual(stats["h2h_data"], {"Competitions": []})
        # database.save_match_stats ile birebir uyumlu anahtarlar
        database.init_db(TEST_DB)
        with database.db_session(TEST_DB) as conn:
            match_id = database.save_match(conn, "STATS_1", "A", "B", "Lig", "01.01.2099", "20:00")
            database.save_match_stats(conn, match_id, **stats)
            ctx = database.get_match_full_context(conn, match_id)
        os.remove(TEST_DB)
        self.assertEqual(ctx["away_last"], [{"id": 2}])

class TestGeminiEngine(unittest.TestCase):

    def test_client_has_timeout(self):
        with mock.patch.object(config, "GEMINI_API_KEY", "test-key"):
            client = gemini_engine.get_gemini_client()
        self.assertEqual(client._api_client._http_options.timeout, config.GEMINI_TIMEOUT_SECONDS * 1000)

    # best_bet / alt_bet yanıtta olmasa bile her zaman (None) eklenir
    PREDICTION = {"predicted_score": "2-1", "confidence_score": 80, "best_bet": None, "alt_bet": None}

    def _run_with_responses(self, side_effect):
        client = mock.MagicMock()
        client.models.generate_content.side_effect = side_effect
        with mock.patch.object(gemini_engine, "get_gemini_client", return_value=client), \
             mock.patch.object(gemini_engine.time, "sleep"):
            result = gemini_engine.generate_prediction({"match": {}})
        return result, client

    def test_falls_back_to_no_search_on_tool_error(self):
        ok = mock.MagicMock(parsed=None, text='{"predicted_score": "2-1", "confidence_score": 80}')
        result, client = self._run_with_responses([ValueError("search tool error"), ok])
        self.assertEqual(result, self.PREDICTION)
        configs = [call.kwargs["config"] for call in client.models.generate_content.call_args_list]
        self.assertIsNotNone(configs[0].tools)
        self.assertIsNone(configs[1].tools)

    def test_raises_after_max_retries(self):
        with self.assertRaises(ValueError):
            self._run_with_responses(ValueError("kalıcı hata"))

class TestWebServer(unittest.TestCase):

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        database.init_db(TEST_DB)
        import web_server
        self.client = web_server.app.test_client()
        with database.db_session(TEST_DB) as conn:
            self.match_id = database.save_match(conn, "WEB_1", "Ev FK", "Dep FK", "Web Lig", "01.01.2099", "20:00")
            database.save_match_stats(conn, self.match_id, h2h_data=[{"score": "1-0"}], home_last=[{"score": "2-0"}],
                                      away_last=[{"score": "0-0"}], missing=None, league_table=None)

    def tearDown(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def _prediction(self, score):
        return {"predicted_ht_result": "İY 1", "predicted_ft_result": "MS 1", "predicted_over_under_3": "ALT",
                "predicted_kg": "KG YOK", "predicted_score": score, "confidence_score": 70, "analysis_summary": "x"}

    def test_pages_render(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        res = self.client.get(f"/matches/{self.match_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn(f"const MATCH_ID = {self.match_id};".encode(), res.data)

    def test_list_shows_match_once_with_latest_prediction(self):
        with database.db_session(TEST_DB) as conn:
            database.save_prediction(conn, self.match_id, self._prediction("1-0"))
            database.save_prediction(conn, self.match_id, self._prediction("2-0"))

        matches = self.client.get("/api/matches/unfinished").get_json()["matches"]
        rows = [m for m in matches if m["id"] == self.match_id]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["predicted_score"], "2-0")

        data = self.client.get(f"/api/matches/{self.match_id}/details").get_json()["data"]
        self.assertEqual([p["predicted_score"] for p in data["predictions"]], ["2-0", "1-0"])
        self.assertIsNone(data["result"])

    def test_save_result_validation_and_evaluation(self):
        url = f"/api/matches/{self.match_id}/result"
        bad_inputs = [
            {"ht_home": 1, "ht_away": 0, "ft_home": "x", "ft_away": 0},
            {"ht_home": True, "ht_away": 0, "ft_home": 1, "ft_away": 0},
            {"ht_home": 0, "ht_away": 0, "ft_home": 100, "ft_away": 0},
            {"ht_home": 2, "ht_away": 0, "ft_home": 1, "ft_away": 0},
        ]
        for body in bad_inputs:
            self.assertEqual(self.client.post(url, json=body).status_code, 400, body)
        self.assertEqual(self.client.post("/api/matches/999999/result",
                                          json={"ht_home": 0, "ht_away": 0, "ft_home": 0, "ft_away": 0}).status_code, 404)

        with database.db_session(TEST_DB) as conn:
            database.save_prediction(conn, self.match_id, self._prediction("2-1"))
        res = self.client.post(url, json={"ht_home": 1, "ht_away": 0, "ft_home": "2", "ft_away": 1})
        self.assertEqual(res.get_json(), {"success": True, "over_under": "ALT", "kg": "KG VAR"})

        data = self.client.get(f"/api/matches/{self.match_id}/details").get_json()["data"]
        self.assertEqual(data["match"]["status"], "COMPLETED")
        self.assertEqual(data["predictions"][0]["evaluation"],
                         {"ht": True, "ft": True, "over_under": True, "kg": False, "score": True, "total_diff": None, "best_bet": None, "alt_bet": None})

    def test_sync_stats_keeps_existing_fields(self):
        url = f"/api/matches/{self.match_id}/sync_stats"
        empty = {"h2h_data": {}, "home_last": None, "away_last": None, "missing": {}, "league_table": {}}
        with mock.patch.object(data_source, "fetch_match_stats", return_value=empty):
            self.assertEqual(self.client.post(url).status_code, 502)

        table = {"LeagueTable": [{"Rank": 1, "TeamName": "Ev FK"}]}
        with mock.patch.object(data_source, "fetch_match_stats", return_value={**empty, "league_table": table}):
            res = self.client.post(url).get_json()
        self.assertEqual(res["updated_fields"], ["league_table"])

        with database.db_session(TEST_DB) as conn:
            ctx = database.get_match_full_context(conn, self.match_id)
        self.assertEqual(ctx["league_table"], table)
        self.assertEqual(ctx["h2h"], [{"score": "1-0"}])

    def test_sync_bulletin(self):
        events = [{"C": "NEW_1", "HN": "A", "AN": "B", "league_name": "Lig", "D": "01.12.2099", "T": "20:00", "TYPE": "1",
                   "MA": [{"MT": 1, "MTID": 1, "OCA": [{"N": 1, "O": 1.5}]}]},
                  {"C": "NEW_2", "HN": "C", "AN": "D", "league_name": "Lig", "D": "01.12.2099", "T": "20:00", "TYPE": "2"}]
        # Bülten sonrası arka plan istatistik işi bu testte çalışmasın (gerçek ağ isteği atmasın)
        no_job = mock.patch.object(stats_sync, "start_background", return_value={"started": False})
        no_job.start()
        self.addCleanup(no_job.stop)
        with mock.patch.object(data_source, "fetch_bulletin", return_value=events):
            res = self.client.post("/api/admin/sync_bulletin", json={}).get_json()
        self.assertEqual((res["new_football"], res["new_basketball"], res["odds_total"], res["total_events"]), (1, 0, 1, 2))

        with mock.patch.object(data_source, "fetch_bulletin", return_value=events):
            res = self.client.post("/api/admin/sync_bulletin", json={"include_basketball": True}).get_json()
        self.assertEqual((res["new_football"], res["new_basketball"], res["skipped"]), (0, 1, 1))

        with mock.patch.object(data_source, "fetch_bulletin", return_value=[]):
            self.assertEqual(self.client.post("/api/admin/sync_bulletin", json={}).status_code, 502)

    def test_details_never_calls_gemini(self):
        with database.db_session(TEST_DB) as conn:
            bare_id = database.save_match(conn, "WEB_BARE", "X FK", "Y FK", "Web Lig", "01.01.2099", "20:00")
            database.set_gemini_enabled(conn, True)

        empty = {"h2h_data": {}, "home_last": None, "away_last": None, "missing": {}, "league_table": {}}
        # Gemini istemcisi hiç oluşturulmamalı (maç detayı / istatistik için Gemini kullanılmaz)
        with mock.patch.object(data_source, "fetch_match_stats", return_value=empty) as nesine, \
             mock.patch.object(gemini_engine, "get_gemini_client", side_effect=AssertionError("Gemini çağrılmamalı")):
            res = self.client.get(f"/api/matches/{bare_id}/details")
        self.assertEqual(res.status_code, 200)
        nesine.assert_called_once()
        self.assertIsNone(res.get_json()["data"]["h2h"])

    def test_gemini_endpoints_blocked_when_disabled(self):
        # Varsayılan: .env ENABLE_GEMINI_ANALYSIS=False ve panel ayarı yok
        settings = self.client.get("/api/admin/settings").get_json()
        self.assertEqual((settings["gemini"]["enabled"], settings["gemini"]["source"]), (False, "env"))

        with database.db_session(TEST_DB) as conn:
            league_id = database.get_match_full_context(conn, self.match_id)["match"]["league_id"]

        with mock.patch.object(gemini_engine, "generate_prediction", side_effect=AssertionError("çağrılmamalı")):
            for url in (f"/api/matches/{self.match_id}/analyze", f"/api/admin/leagues/{league_id}/analyze"):
                res = self.client.post(url, json={})
                self.assertEqual((res.status_code, res.get_json()["code"]), (403, "GEMINI_DISABLED"), url)

        # Lig analizi engellendiğinde ligin izni de açılmamış olmalı
        with database.db_session(TEST_DB) as conn:
            self.assertFalse(database.is_league_analysis_enabled(conn, league_id))
        self.assertFalse(self.client.get("/api/stats").get_json()["config"]["gemini_analysis_enabled"])

    def test_gemini_setting_and_league_approval_flow(self):
        self.assertEqual(self.client.post("/api/admin/settings/gemini", json={"enabled": "yes"}).status_code, 400)
        self.assertEqual(self.client.post("/api/admin/settings/gemini", json={}).status_code, 400)

        res = self.client.post("/api/admin/settings/gemini", json={"enabled": True}).get_json()
        self.assertEqual((res["gemini"]["enabled"], res["gemini"]["source"]), (True, "panel"))
        self.assertTrue(self.client.get("/api/stats").get_json()["config"]["gemini_analysis_enabled"])

        url = f"/api/matches/{self.match_id}/analyze"
        with mock.patch.object(gemini_engine, "generate_prediction", return_value=self._prediction("2-0")) as gen:
            # Lig izni kapalı (REQUIRE_LEAGUE_APPROVAL=True): Gemini çağrılmadan 409 dönmeli
            blocked = self.client.post(url, json={})
            self.assertEqual((blocked.status_code, blocked.get_json()["code"]), (409, "LEAGUE_DISABLED"))
            self.assertEqual(blocked.get_json()["league_name"], "Web Lig")
            gen.assert_not_called()

            # Kullanıcı onayı ile lig açılır ve analiz yapılır
            ok = self.client.post(url, json={"enable_league": True})
            self.assertEqual(ok.status_code, 200)
            gen.assert_called_once()

        data = self.client.get(f"/api/matches/{self.match_id}/details").get_json()["data"]
        self.assertTrue(data["match"]["league_gemini_analysis"])
        self.assertEqual(data["predictions"][0]["predicted_score"], "2-0")
        self.assertTrue(data["settings"]["gemini_enabled"])

        # .env varsayılanına dönüş
        res = self.client.post("/api/admin/settings/gemini", json={"enabled": None}).get_json()
        self.assertEqual((res["gemini"]["enabled"], res["gemini"]["source"]), (config.ENABLE_GEMINI_ANALYSIS, "env"))

    def test_coupon_odds_settings_flow(self):
        # 1. Başlangıçta .env varsayılanları
        settings = self.client.get("/api/admin/settings").get_json()
        self.assertIn("coupon_odds", settings)
        self.assertEqual(settings["coupon_odds"]["min_match_odds"], config.MIN_MATCH_ODDS)
        self.assertEqual(settings["coupon_odds"]["min_coupon_odds"], config.MIN_COUPON_TOTAL_ODDS)
        self.assertEqual(settings["coupon_odds"]["source"], "env")

        # 2. Panelden güncelleme (1.20 ve 1.80 gibi daha düşük değerler de dahil)
        res = self.client.post("/api/admin/settings/coupon_odds", json={
            "min_match_odds": 1.20,
            "min_coupon_odds": 1.80
        }).get_json()
        self.assertTrue(res["success"])
        self.assertEqual(res["coupon_odds"]["min_match_odds"], 1.20)
        self.assertEqual(res["coupon_odds"]["min_coupon_odds"], 1.80)
        self.assertEqual(res["coupon_odds"]["source"], "panel")

        # GET ile kontrol
        check = self.client.get("/api/admin/settings").get_json()["coupon_odds"]
        self.assertEqual(check["min_match_odds"], 1.20)
        self.assertEqual(check["min_coupon_odds"], 1.80)
        self.assertEqual(check["source"], "panel")

        # 3. .env varsayılanına sıfırlama (null)
        reset_res = self.client.post("/api/admin/settings/coupon_odds", json={"min_match_odds": None, "min_coupon_odds": None}).get_json()
        self.assertTrue(reset_res["success"])
        self.assertEqual(reset_res["coupon_odds"]["source"], "env")
        self.assertEqual(reset_res["coupon_odds"]["min_match_odds"], config.MIN_MATCH_ODDS)
        self.assertEqual(reset_res["coupon_odds"]["min_coupon_odds"], config.MIN_COUPON_TOTAL_ODDS)
        self.assertEqual(self.client.post("/api/admin/settings/coupon_odds", json={"min_match_odds": "abc"}).status_code, 400)

    def test_missing_stats_not_sent_to_gemini(self):
        with database.db_session(TEST_DB) as conn:
            database.set_gemini_enabled(conn, True)
            league_id = database.get_or_create_league(conn, "Açık Lig", gemini_analysis=1)
            match_id = database.save_match(conn, "NO_H2H", "X", "Y", "Açık Lig", "01.01.2099", "20:00")
            # Son maçlar var, H2H yok
            database.save_match_stats(conn, match_id, h2h_data=None, home_last=[{"score": "1-0"}], away_last=[{"score": "2-2"}],
                                      missing=None, league_table=None)

        no_h2h = {"h2h_data": None, "home_last": None, "away_last": None, "missing": None, "league_table": None}
        with mock.patch.object(data_source, "fetch_match_stats", return_value=no_h2h) as fetch, \
             mock.patch.object(gemini_engine, "generate_prediction", side_effect=AssertionError("Gemini çağrılmamalı")):
            res = self.client.post(f"/api/matches/{match_id}/analyze", json={})
            league_res = self.client.post(f"/api/admin/leagues/{league_id}/analyze").get_json()
        self.assertEqual((res.status_code, res.get_json()["code"]), (409, "MISSING_STATS"))
        self.assertEqual(res.get_json()["missing"], ["Aralarındaki maçlar (H2H)"])
        fetch.assert_called()  # göndermeden önce veri kaynağından bir kez daha denenir
        self.assertEqual((league_res["analyzed_count"], league_res["skipped_count"]), (0, 1))

        # Veri kaynağı H2H'yi döndürürse eksik tamamlanır ve analiz yapılır
        with_h2h = {**no_h2h, "h2h_data": [{"score": "0-1"}]}
        with mock.patch.object(data_source, "fetch_match_stats", return_value=with_h2h), \
             mock.patch.object(gemini_engine, "generate_prediction", return_value=self._prediction("1-1")) as gen:
            res = self.client.post(f"/api/matches/{match_id}/analyze", json={})
        self.assertEqual(res.status_code, 200)
        gen.assert_called_once()
        sent_ctx = gen.call_args.args[0]
        self.assertEqual((sent_ctx["h2h"], sent_ctx["home_last"]), ([{"score": "0-1"}], [{"score": "1-0"}]))

    def test_list_shows_stats_ready(self):
        with database.db_session(TEST_DB) as conn:
            database.save_match(conn, "BARE_1", "P", "Q", "Web Lig", "01.01.2099", "21:00")
        rows = {m["code"]: m["stats_ready"] for m in self.client.get("/api/matches/unfinished").get_json()["matches"]}
        self.assertEqual((rows["WEB_1"], rows["BARE_1"]), (1, 0))

    def test_gemini_not_used_for_match_data(self):
        # Gemini ile internetten istatistik çekme (eski 'İnternetten Tara') tamamen kaldırıldı
        with database.db_session(TEST_DB) as conn:
            database.set_gemini_enabled(conn, True)
        self.assertEqual(self.client.post(f"/api/matches/{self.match_id}/refresh_stats").status_code, 404)
        self.assertFalse(hasattr(gemini_engine, "fetch_live_team_stats"))

        # İstatistik endpoint'leri sadece veri kaynağını kullanır, Gemini istemcisi hiç oluşturulmaz
        stats = {"h2h_data": [{"score": "2-2"}], "home_last": None, "away_last": None, "missing": None, "league_table": None}
        with mock.patch.object(data_source, "fetch_match_stats", return_value=stats), \
             mock.patch.object(gemini_engine, "get_gemini_client", side_effect=AssertionError("Gemini çağrılmamalı")):
            self.assertEqual(self.client.post(f"/api/matches/{self.match_id}/sync_stats").status_code, 200)
            self.assertEqual(self.client.post("/api/admin/sync_stats", json={"limit": 5}).status_code, 200)
            stats_sync.wait(10)

    def test_cli_reads_panel_setting(self):
        import app
        with database.db_session(TEST_DB) as conn:
            database.set_gemini_enabled(conn, True)
        with mock.patch("builtins.input", side_effect=AssertionError("soru sorulmamalı")):
            self.assertTrue(app.confirm_when_gemini_disabled(None, "?", "iptal"))

        with database.db_session(TEST_DB) as conn:
            database.set_gemini_enabled(conn, False)
        with mock.patch("builtins.input", return_value="hayır"), mock.patch("builtins.print"):
            self.assertFalse(app.confirm_when_gemini_disabled(None, "?", "iptal"))

class TestPrompt(unittest.TestCase):

    def _history(self, n):
        return [{"date": f"2026-10-{10 + i:02d}", "league": "1. Lig", "home": "Ev FK", "away": f"Rakip {i}",
                 "score": "2-1", "ht_score": "1-0", "result": "G"} for i in range(n)]

    def test_prompt_contains_full_history_table_and_priority_odds(self):
        odds = [{"market_name": f"Özel Market {i}", "option_name": "X", "odds_value": 9.0} for i in range(30)]
        odds.append({"market_name": "Maç Sonucu", "option_name": "MS 1", "odds_value": 1.62})
        ctx = {
            "match": {"home_team": "Ev FK", "away_team": "Dep FK", "league_name": "1. Lig"},
            "odds": odds,
            "home_last": self._history(10), "away_last": self._history(10), "h2h": self._history(4),
            "league_table": {"LeagueTable": [{"Rank": 1, "TeamName": "Ev FK", "Played": 12, "Won": 9, "Draw": 1, "Lost": 2,
                                              "GoalsFor": 26, "GoalsAgainst": 11, "Points": 28, "Form": ["G", "M", "G", "G", "G"]}]},
        }
        prompt = gemini_engine.build_prompt(ctx)
        home_section = prompt.split("Son Maçları:")[1].split("2. Deplasman")[0]
        self.assertEqual(home_section.count("Rakip"), 10)
        self.assertIn("- 2026-10-19 | 1. Lig | Ev FK 2-1 Rakip 9 (İY 1-0) [G]", prompt)
        self.assertIn("1. Ev FK | O:12 G:9 B:1 M:2 | Gol 26:11 | Puan 28 | Son 5: G M G G G", prompt)
        # Ana marketler oran listesinin başında, en fazla 25 oran
        self.assertLess(prompt.index("Maç Sonucu: MS 1"), prompt.index("Özel Market 0"))
        self.assertEqual(prompt.count("-> Oran:"), gemini_engine.MAX_ODDS_ROWS)
        # "Oynanmaması gerekenler" kaldırıldı: prompt'ta ve yanıt şemasında yok (token tasarrufu)
        self.assertNotIn("OYNANMAMASI", prompt)
        self.assertNotIn("avoid_bets", gemini_engine.MatchPrediction.model_fields)
        self.assertNotIn("avoid_bets", gemini_engine.BasketballPrediction.model_fields)
        self.assertNotIn("İnternetten kontrol ediniz).\n\n2. Deplasman", prompt)

class TestBasketball(unittest.TestCase):
    """Basketbol: KG / Alt-Üst yok, maç sonu toplam sayı tahmini, MS 1 / MS 2 (beraberlik yok)."""

    def test_basketball_odds_labels(self):
        event = {"TYPE": "2", "MA": [
            {"MT": 1, "MST": 2, "OCA": [{"N": 1, "O": 1.61, "ON": "1"}, {"N": 2, "O": 1.62, "ON": "2"}]},
            {"MT": 2, "MST": 621, "OCA": [{"N": 1, "O": 1.68}, {"N": 2, "O": 12.7}, {"N": 3, "O": 1.68}]},
            {"MT": 2, "MST": 114, "SOV": 237.5, "OCA": [{"N": 1, "O": 1.62}, {"N": 2, "O": 1.61}]},
            {"MT": 2, "MST": 113, "SOV": -2.5, "MN": "Handikaplı Maç Sonucu (Uzt. Dahil) -2.5",
             "OCA": [{"N": 1, "O": 1.81, "ON": "1"}, {"N": 2, "O": 1.46, "ON": "2"}]},
        ]}
        odds = [(o["market_name"], o["option_name"]) for o in nesine_api.extract_odds_from_event(event)]
        self.assertEqual(odds, [
            ("Maç Sonucu", "MS 1"), ("Maç Sonucu", "MS 2"),
            ("İlk Yarı Sonucu", "İY 1"), ("İlk Yarı Sonucu", "İY 0"), ("İlk Yarı Sonucu", "İY 2"),
            ("Toplam Sayı Alt/Üst", "237.5 Alt"), ("Toplam Sayı Alt/Üst", "237.5 Üst"),
            ("Handikaplı Maç Sonucu (Uzt. Dahil) -2.5", "1"), ("Handikaplı Maç Sonucu (Uzt. Dahil) -2.5", "2"),
        ])

    def test_basketball_prompt(self):
        prompt = gemini_engine.build_prompt({"match": {"home_team": "Atlanta", "away_team": "Cleveland", "sport_type": "BASKETBALL"}, "odds": []})
        self.assertIn("basketbol veri analisti", prompt)
        self.assertIn("MAÇ SONU TOPLAM SAYI", prompt)
        self.assertNotIn("KESİNLİKLE 3 GOL KURALI", prompt)
        self.assertNotIn("Karşılıklı Gol (KG VAR / KG YOK)", prompt)
        self.assertIs(gemini_engine._response_schema("BASKETBALL"), gemini_engine.BasketballPrediction)

    def test_basketball_schema_converts_for_gemini_api(self):
        from google import genai
        from google.genai import _transformers
        schema = _transformers.t_schema(genai.Client(api_key="offline-test")._api_client, gemini_engine.BasketballPrediction)
        self.assertIn("predicted_total_points", schema.required)
        self.assertNotIn("predicted_kg", schema.properties)
        self.assertNotIn("predicted_over_under_3", schema.properties)

    def test_finalize_basketball_prediction(self):
        prediction = {
            "predicted_ht_result": "İY 1", "predicted_ft_result": "MS 1", "predicted_score": "112-105", "predicted_total_points": 230,
        }
        result = gemini_engine._finalize(prediction, {"match": {"sport_type": "BASKETBALL"}, "odds": []})
        # Toplam sayı skorla tutarlı hale getirilir; Alt/Üst ve KG boş kalır
        self.assertEqual(result["predicted_total_points"], 217)
        self.assertIsNone(result["predicted_over_under_3"])
        self.assertIsNone(result["predicted_kg"])

    def test_basketball_evaluation(self):
        result = {"ht_home_score": 56, "ht_away_score": 50, "ft_home_score": 110, "ft_away_score": 104}
        ev = rules.evaluate_prediction({"predicted_ht_result": "İY 1", "predicted_ft_result": "MS 1", "predicted_score": "112-105",
                                        "predicted_total_points": 217, "predicted_over_under_3": "ÜST"}, result, "BASKETBALL")
        self.assertEqual(ev, {"ht": True, "ft": True, "over_under": None, "kg": None, "score": False, "total_diff": 3, "best_bet": None, "alt_bet": None})

    def test_basketball_results_and_accuracy(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        database.init_db(TEST_DB)
        import web_server
        client = web_server.app.test_client()
        with database.db_session(TEST_DB) as conn:
            bb_id = database.save_match(conn, "BB_1", "Atlanta", "Cleveland", "NBA", "01.01.2099", "02:00", sport_type="BASKETBALL")
            fb_id = database.save_match(conn, "FB_1", "A", "B", "Lig", "01.01.2099", "20:00")
            database.save_prediction(conn, bb_id, {"predicted_ht_result": "İY 1", "predicted_ft_result": "MS 1",
                                                   "predicted_score": "120-110", "predicted_total_points": 230})
            database.save_prediction(conn, fb_id, {"predicted_ht_result": "İY 1", "predicted_ft_result": "MS 1",
                                                   "predicted_over_under_3": "ALT", "predicted_kg": "KG YOK", "predicted_score": "1-0"})

        # Basketbol skorlarında üst sınır yok; futbolda 99
        res = client.post(f"/api/matches/{bb_id}/result", json={"ht_home": 61, "ht_away": 55, "ft_home": 130, "ft_away": 121})
        self.assertEqual(res.get_json(), {"success": True, "total_points": 251})
        self.assertEqual(client.post(f"/api/matches/{bb_id}/result", json={"ht_home": -1, "ht_away": 0, "ft_home": 1, "ft_away": 0}).status_code, 400)
        self.assertEqual(client.post(f"/api/matches/{fb_id}/result", json={"ht_home": 0, "ht_away": 0, "ft_home": 100, "ft_away": 0}).status_code, 400)
        client.post(f"/api/matches/{fb_id}/result", json={"ht_home": 1, "ht_away": 0, "ft_home": 1, "ft_away": 0})

        with database.db_session(TEST_DB) as conn:
            bb_result = database.get_match_result(conn, bb_id)
            accuracy = database.calculate_prediction_accuracy(conn)
        os.remove(TEST_DB)
        # Basketbol sonucuna 3 gol / KG yazılmaz
        self.assertIsNone(bb_result["is_over_3_goals"])
        self.assertIsNone(bb_result["is_kg_var"])
        self.assertEqual((accuracy["total_evaluated"], accuracy["ft_total"], accuracy["ou_total"], accuracy["kg_total"]), (2, 2, 1, 1))
        self.assertEqual((accuracy["ft_accuracy"], accuracy["kg_accuracy"]), (100.0, 100.0))
        self.assertEqual((accuracy["total_points_evaluated"], accuracy["total_points_avg_diff"]), (1, 21.0))

class TestBestBet(unittest.TestCase):
    """⭐ En güçlü tercih (oran sınırı yok) + oranı MIN_BEST_BET_ODDS altındaysa oranı eşik üstü 2. tercih; seçimlerin kendisi değerlendirilir."""

    ODDS = [
        {"market_name": "Maç Sonucu", "option_name": "MS 1", "odds_value": 1.10},
        {"market_name": "Maç Sonucu", "option_name": "MS 0", "odds_value": 7.50},
        {"market_name": "Maç Sonucu", "option_name": "MS 2", "odds_value": 15.0},
        {"market_name": "İlk Yarı Sonucu", "option_name": "İY 1", "odds_value": 1.55},
        {"market_name": "3.5 Gol Alt/Üst", "option_name": "3.5 Alt", "odds_value": 1.30},
        {"market_name": "3.5 Gol Alt/Üst", "option_name": "3.5 Üst", "odds_value": 3.10},
        {"market_name": "Karşılıklı Gol", "option_name": "KG VAR", "odds_value": 1.85},
        {"market_name": "Karşılıklı Gol", "option_name": "KG YOK", "odds_value": 1.80},
        {"market_name": "Çifte Şans", "option_name": "X-2", "odds_value": 5.20},
        {"market_name": "2.5 Gol Alt/Üst", "option_name": "2.5 Üst", "odds_value": 1.60},
    ]

    def _ctx(self, sport="FOOTBALL", odds=None):
        return {"match": {"home_team": "A", "away_team": "B", "sport_type": sport}, "odds": self.ODDS if odds is None else odds}

    def test_bet_candidates_respect_min_odds(self):
        candidates = {(c["market"], c["selection"]): c["odds"] for c in rules.bet_candidates(self.ODDS, "FOOTBALL", 1.50)}
        # MS 1 (1.10) ve ALT (1.30) eşiğin altında; 2.5 Gol marketi aday değil (futbolda Alt/Üst 3 gol = 3.5 baremi)
        self.assertEqual(set(candidates), {("ft", "MS 0"), ("ft", "MS 2"), ("ht", "İY 1"), ("over_under", "ÜST"),
                                           ("kg", "KG VAR"), ("kg", "KG YOK"), ("dc", "X-2")})
        basketball = {c["market"] for c in rules.bet_candidates(self.ODDS, "BASKETBALL", 1.50)}
        self.assertEqual(basketball, {"ft", "ht"})
        self.assertEqual(rules.selection_odds(self.ODDS, "over_under", "ALT"), 1.30)

    def test_selection_won(self):
        result = {"ht_home_score": 0, "ht_away_score": 0, "ft_home_score": 1, "ft_away_score": 1}
        cases = [("ft", "MS 0", True), ("ft", "MS 1", False), ("ht", "İY 0", True), ("over_under", "ALT", True),
                 ("over_under", "ÜST", False), ("kg", "KG VAR", True), ("dc", "1-X", True), ("dc", "1-2", False),
                 ("dc", "X-2", True), ("bilinmeyen", "x", None)]
        for market, selection, expected in cases:
            self.assertEqual(rules.selection_won(market, selection, result), expected, (market, selection))

    def test_prompt_lists_all_candidates_with_alt_bet_rule(self):
        with mock.patch.object(config, "MIN_BEST_BET_ODDS", 1.30):
            prompt = gemini_engine.build_prompt(self._ctx())
        self.assertIn("--- EN GÜÇLÜ TERCİH (best_bet) VE 2. TERCİH (alt_bet) ---", prompt)
        self.assertIn("- MS 1 (Maç Sonu) | Oran 1.10 | Piyasa olasılığı %91", prompt)   # düşük oranlı seçenekler de listelenir
        self.assertIn("- KG VAR (Karşılıklı Gol) | Oran 1.85 | Piyasa olasılığı %54", prompt)
        self.assertIn("- X-2 (Çifte Şans) | Oran 5.20", prompt)
        self.assertIn("best_bet seçeneğinin oranı 1.30 altındaysa", prompt)

    def test_finalize_best_bet_and_alt_bet(self):
        def finalize(selection, alt_selection="YOK", alt_confidence=0, odds=None):
            prediction = {"predicted_ft_result": "MS 1", "best_bet_selection": selection, "best_bet_confidence": 90,
                          "best_bet_reason": "gerekçe", "alt_bet_selection": alt_selection, "alt_bet_confidence": alt_confidence}
            with mock.patch.object(config, "MIN_BEST_BET_ODDS", 1.50):
                result = gemini_engine._finalize(prediction, self._ctx(odds=odds))
            return result["best_bet"], result["alt_bet"]

        # Düşük oranlı ⭐ (MS 1 @ 1.10) eskisi gibi kabul edilir; oranı eşik üstü 2. tercih de kaydedilir
        best, alt = finalize("ms 1", "kg var", 62)
        self.assertEqual(best, {"market": "ft", "selection": "MS 1", "odds": 1.10, "confidence": 90, "reason": "gerekçe"})
        self.assertEqual(alt, {"market": "kg", "selection": "KG VAR", "odds": 1.85, "confidence": 62})
        # Çifte Şans 2. tercih olabilir, eşiğin altındaki seçenek (ALT @ 1.30) olamaz
        self.assertEqual(finalize("MS 1", "X-2", 30)[1]["selection"], "X-2")
        self.assertIsNone(finalize("MS 1", "ALT", 80)[1])
        self.assertIsNone(finalize("MS 1", "YOK", 0)[1])
        # ⭐ oranı eşik ve üstüyse 2. tercih tutulmaz
        best, alt = finalize("KG VAR", "ÜST", 40)
        self.assertEqual((best["selection"], alt), ("KG VAR", None))
        # Çifte Şans ve bilinmeyen seçimler ⭐ olamaz
        self.assertIsNone(finalize("X-2")[0])
        self.assertIsNone(finalize("Korner Üst")[0])

    def test_schema_requires_bet_fields(self):
        from google import genai
        from google.genai import _transformers
        client = genai.Client(api_key="offline-test")._api_client
        required_fields = {"best_bet_selection", "best_bet_confidence", "best_bet_reason", "alt_bet_selection", "alt_bet_confidence"}
        for model in (gemini_engine.MatchPrediction, gemini_engine.BasketballPrediction):
            schema = _transformers.t_schema(client, model)
            self.assertTrue(required_fields <= set(schema.required), model.__name__)
            self.assertNotIn("best_bet_market", schema.properties)

    def test_best_bet_saved_evaluated_and_league_stats(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        database.init_db(TEST_DB)
        import web_server
        client = web_server.app.test_client()
        base = {"predicted_ht_result": "İY 1", "predicted_ft_result": "MS 1", "predicted_over_under_3": "ALT",
                "predicted_kg": "KG YOK", "predicted_score": "1-0", "confidence_score": 70}
        with database.db_session(TEST_DB) as conn:
            league_id = database.get_or_create_league(conn, "Test Lig")
            m1 = database.save_match(conn, "BB1", "A", "B", "Test Lig", "01.01.2099", "20:00")
            m2 = database.save_match(conn, "BB2", "C", "D", "Test Lig", "01.01.2099", "21:00")
            database.save_odds(conn, m1, self.ODDS)
            # m1: önce yanlış (MS 0), sonra doğru (KG VAR) değerli tercih -> lig oranında sadece SON tahmin sayılır
            database.save_prediction(conn, m1, {**base, "best_bet": {"market": "ft", "selection": "MS 0", "odds": 7.5, "confidence": 20, "reason": "x"}})
            database.save_prediction(conn, m1, {**base, "best_bet": {"market": "kg", "selection": "KG VAR", "odds": 1.85, "confidence": 62, "reason": "İki takım da gol atıyor"},
                                                "alt_bet": {"market": "ft", "selection": "MS 0", "odds": 7.5, "confidence": 25}})
            # m2: eski tahmin (oran kayıtlı değil) -> çifte şans tuttu
            database.save_prediction(conn, m2, {**base, "best_bet": {"market": "dc", "selection": "X-2", "confidence": 40, "reason": "y"}})
            database.save_match_stats(conn, m1, h2h_data=[{"score": "1-0"}], home_last=None, away_last=None, missing=None, league_table=None)
            database.save_result(conn, m1, 1, 1, 2, 1)   # KG VAR tuttu, MS 0 tutmadı
            database.save_result(conn, m2, 0, 1, 0, 2)   # deplasman kazandı: X-2 tuttu

        data = client.get(f"/api/matches/{m1}/details").get_json()["data"]
        latest = data["predictions"][0]
        self.assertEqual(latest["best_bet"], {"market": "kg", "selection": "KG VAR", "odds": 1.85, "confidence": 62, "reason": "İki takım da gol atıyor"})
        self.assertTrue(latest["evaluation"]["best_bet"])
        self.assertEqual(latest["alt_bet"], {"market": "ft", "selection": "MS 0", "odds": 7.5, "confidence": 25})
        self.assertFalse(latest["evaluation"]["alt_bet"])   # 2-1 bitti: MS 0 tutmadı
        self.assertIsNone(data["predictions"][1]["alt_bet"])
        self.assertFalse(data["predictions"][1]["evaluation"]["best_bet"])

        league = next(l for l in client.get("/api/admin/leagues").get_json()["leagues"] if l["id"] == league_id)
        self.assertEqual((league["evaluated"], league["best_bet_total"], league["best_bet_correct"], league["best_bet_accuracy"]), (2, 2, 2, 100.0))
        self.assertEqual((league["alt_bet_total"], league["alt_bet_correct"], league["alt_bet_accuracy"]), (1, 0, 0.0))

        completed = {m["code"]: m for m in client.get("/api/matches/completed").get_json()["matches"]}
        self.assertEqual((completed["BB1"]["best_bet_correct"], completed["BB2"]["best_bet_correct"]), (True, True))
        self.assertEqual(completed["BB1"]["best_bet_odds"], 1.85)
        self.assertEqual((completed["BB1"]["alt_bet_selection"], completed["BB1"]["alt_bet_correct"]), ("MS 0", False))
        with database.db_session(TEST_DB) as conn:
            accuracy = database.calculate_prediction_accuracy(conn)
        os.remove(TEST_DB)
        self.assertEqual((accuracy["best_bet_total"], accuracy["best_bet_correct"]), (3, 2))
        self.assertEqual((accuracy["alt_bet_total"], accuracy["alt_bet_correct"]), (1, 0))

class TestPredictionBatch(unittest.TestCase):
    """Toplu Gemini analizi: sadece uygun maçlar, arka planda, kota / hata durumunda kendiliğinden durur."""

    STATS = dict(h2h_data=[{"score": "1-0"}], home_last=[{"score": "2-0"}], away_last=[{"score": "0-0"}], missing=None, league_table=None)

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        database.init_db(TEST_DB)
        import web_server
        self.client = web_server.app.test_client()
        # Testlerde istekler arası bekleme olmasın; sıra testleri deterministik olsun diye tek worker (paralel test ayrı)
        for attr, value in (("delay_seconds", 0), ("workers", 1)):
            patcher = mock.patch.object(prediction_batch._job, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with database.db_session(TEST_DB) as conn:
            database.set_gemini_enabled(conn, True)
            database.get_or_create_league(conn, "Açık", gemini_analysis=1)
            database.get_or_create_league(conn, "Kapalı", gemini_analysis=0)
            self.ids = {}
            for code, league, date in (("OK1", "Açık", "01.01.2099"), ("OK2", "Açık", "02.01.2099"), ("DONE", "Açık", "01.01.2020"),
                                       ("NOSTATS", "Açık", "01.01.2099"), ("CLOSED", "Kapalı", "01.01.2099"), ("PREDICTED", "Açık", "01.01.2099")):
                self.ids[code] = database.save_match(conn, code, f"{code} Ev", f"{code} Dep", league, date, "20:00")
                if code != "NOSTATS":
                    database.save_match_stats(conn, self.ids[code], **self.STATS)
            database.save_prediction(conn, self.ids["PREDICTED"], {"predicted_ft_result": "MS 1"})

    def tearDown(self):
        prediction_batch.wait(10)
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def _prediction(self):
        return {"predicted_ht_result": "İY 1", "predicted_ft_result": "MS 1", "predicted_over_under_3": "ALT",
                "predicted_kg": "KG YOK", "predicted_score": "1-0", "confidence_score": 70, "best_bet": None}

    def test_eligible_matches(self):
        res = self.client.get("/api/admin/jobs/predictions").get_json()
        self.assertEqual(res["eligible"], 2)
        self.assertEqual([m["code"] for m in prediction_batch.eligible_matches(TEST_DB)], ["OK1", "OK2"])

    def test_start_blocked_when_gemini_disabled(self):
        with database.db_session(TEST_DB) as conn:
            database.set_gemini_enabled(conn, False)
        res = self.client.post("/api/admin/jobs/predictions/start")
        self.assertEqual((res.status_code, res.get_json()["code"]), (403, "GEMINI_DISABLED"))

    def test_batch_analyzes_eligible_matches(self):
        with mock.patch.object(gemini_engine, "generate_prediction", return_value=self._prediction()) as gen, \
             mock.patch.object(data_source, "fetch_match_stats", side_effect=AssertionError("veri tam, istek atılmamalı")):
            job = self.client.post("/api/admin/jobs/predictions/start").get_json()["job"]
            prediction_batch.wait(10)
        self.assertEqual((job["started"], job["total"]), (True, 2))
        self.assertEqual(gen.call_count, 2)
        status = self.client.get("/api/admin/jobs/predictions").get_json()
        self.assertEqual((status["job"]["status"], status["job"]["analyzed"], status["job"]["percent"], status["eligible"]), ("done", 2, 100.0, 0))
        with database.db_session(TEST_DB) as conn:
            for code in ("OK1", "OK2"):
                self.assertEqual(len(database.get_match_predictions(conn, self.ids[code])), 1)
            self.assertEqual(len(database.get_match_predictions(conn, self.ids["CLOSED"])), 0)

    def test_batch_runs_in_parallel(self):
        with database.db_session(TEST_DB) as conn:
            for i in range(6):
                match_id = database.save_match(conn, f"P{i}", f"P{i} Ev", f"P{i} Dep", "Açık", "03.01.2099", "20:00")
                database.save_match_stats(conn, match_id, **self.STATS)
        active, peak, lock = [0], [0], threading.Lock()

        def slow_prediction(ctx, model_name=None):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.3)
            with lock:
                active[0] -= 1
            return self._prediction()

        with mock.patch.object(prediction_batch._job, "workers", 3), \
             mock.patch.object(gemini_engine, "generate_prediction", side_effect=slow_prediction) as gen:
            started = time.time()
            job = self.client.post("/api/admin/jobs/predictions/start").get_json()["job"]
            prediction_batch.wait(10)
            elapsed = time.time() - started
        status = prediction_batch.get_status()
        self.assertEqual((job["total"], job["workers"]), (8, 3))
        self.assertEqual((status["status"], status["analyzed"], gen.call_count), ("done", 8, 8))
        self.assertEqual(peak[0], 3)            # aynı anda 3 istek
        self.assertLess(elapsed, 8 * 0.3 * 0.7)  # sıralı çalışmadan belirgin şekilde hızlı
        with database.db_session(TEST_DB) as conn:
            counts = [len(database.get_match_predictions(conn, mid)) for mid in
                      (r["id"] for r in conn.execute("SELECT id FROM matches WHERE code IN ('OK1', 'OK2', 'P0', 'P1', 'P2', 'P3', 'P4', 'P5')"))]
        self.assertEqual(counts, [1] * 8)       # paralelde aynı maça iki kez tahmin yazılmaz

    def test_parallel_batch_stops_after_consecutive_errors(self):
        with database.db_session(TEST_DB) as conn:
            for i in range(12):
                match_id = database.save_match(conn, f"E{i}", f"E{i} Ev", f"E{i} Dep", "Açık", "03.01.2099", "20:00")
                database.save_match_stats(conn, match_id, **self.STATS)
        with mock.patch.object(prediction_batch._job, "workers", 3), \
             mock.patch.object(gemini_engine, "generate_prediction", side_effect=RuntimeError("429 RESOURCE_EXHAUSTED")) as gen:
            self.client.post("/api/admin/jobs/predictions/start")
            prediction_batch.wait(10)
        job = prediction_batch.get_status()
        # Limit dolduğunda yolda olan istekler (en fazla workers - 1) de biter; yeni istek başlatılmaz
        self.assertEqual(job["status"], "error")
        self.assertTrue(prediction_batch.MAX_CONSECUTIVE_ERRORS <= gen.call_count <= prediction_batch.MAX_CONSECUTIVE_ERRORS + 2, gen.call_count)
        self.assertLess(job["processed"], job["total"])

    def test_batch_stops_after_consecutive_errors(self):
        with database.db_session(TEST_DB) as conn:
            for i in range(6):
                match_id = database.save_match(conn, f"X{i}", f"X{i} Ev", f"X{i} Dep", "Açık", "03.01.2099", "20:00")
                database.save_match_stats(conn, match_id, **self.STATS)
        with mock.patch.object(gemini_engine, "generate_prediction", side_effect=RuntimeError("429 RESOURCE_EXHAUSTED")) as gen:
            self.client.post("/api/admin/jobs/predictions/start")
            prediction_batch.wait(10)
        job = prediction_batch.get_status()
        self.assertEqual((job["status"], job["errors"], gen.call_count), ("error", prediction_batch.MAX_CONSECUTIVE_ERRORS, 5))
        self.assertIn("RESOURCE_EXHAUSTED", job["message"])

    def test_batch_stops_when_gemini_disabled_mid_run(self):
        def predict_then_disable(ctx, model_name=None):
            with database.db_session(TEST_DB) as conn:
                database.set_gemini_enabled(conn, False)
            return self._prediction()

        with mock.patch.object(gemini_engine, "generate_prediction", side_effect=predict_then_disable) as gen:
            self.client.post("/api/admin/jobs/predictions/start")
            prediction_batch.wait(10)
        job = prediction_batch.get_status()
        self.assertEqual((job["status"], job["analyzed"], gen.call_count), ("error", 1, 1))
        self.assertIn("kapatıldığı", job["message"])

class TestIddaaApi(unittest.TestCase):
    """iddaa.com yanıt formatlarının uygulama formatlarına dönüşümü (ağ çağrısı yapılmaz)."""

    EVENT = {
        "i": 3258192, "bri": 72735878, "hn": "Esenler Erokspor", "an": "Ankara Keçiörengücü", "sid": 1,
        "ci": 1980, "d": 1793379600,
        "m": [
            {"t": 1, "st": 1, "o": [{"no": 1, "odd": 1.69, "wodd": 1.62, "n": "1"}, {"no": 2, "odd": 3.29, "wodd": 3.17, "n": "X"},
                                    {"no": 3, "odd": 3.45, "wodd": 3.31, "n": "2"}]},
            {"t": 2, "st": 101, "sov": "2.5", "o": [{"no": 1, "odd": 1.87, "wodd": 1.8, "n": "Alt"}, {"no": 2, "odd": 1.53, "wodd": 1.47, "n": "Üst"}]},
            {"t": 2, "st": 89, "o": [{"no": 1, "odd": 1.49, "wodd": 1.43, "n": "Var"}, {"no": 2, "odd": 1.95, "wodd": 1.87, "n": "Yok"}]},
            {"t": 2, "st": 555, "sov": "-1", "o": [{"no": 1, "odd": 2.0, "n": "1"}, {"no": 2, "odd": 3.0, "n": "X"}, {"no": 3, "odd": 4.0, "n": "2"}]},
        ]
    }

    def test_bulletin_event_converted_to_nesine_format(self):
        event = iddaa_api._to_nesine_event(self.EVENT, "1", {1980: "Türkiye 1. Lig"}, {"2_555": "Handikaplı Maç Sonucu {h}"})
        self.assertEqual((event["C"], event["TYPE"], event["HN"], event["AN"]), (3258192, "1", "Esenler Erokspor", "Ankara Keçiörengücü"))
        # Epoch -> Türkiye saati (Nesine 'D' / 'T' ile aynı)
        self.assertEqual((event["D"], event["T"]), ("30.10.2026", "20:00"))
        self.assertEqual((event["league_name"], event["league_external_id"]), ("Türkiye 1. Lig", 1980))

        odds = {(o["market_name"], o["option_name"]): o["odds_value"] for o in nesine_api.extract_odds_from_event(event)}
        # Online oran ('wodd') kullanılır
        self.assertEqual(odds[("Maç Sonucu", "MS 1")], 1.62)
        self.assertEqual(odds[("2.5 Gol Alt/Üst", "2.5 Üst")], 1.47)
        self.assertEqual(odds[("Karşılıklı Gol", "KG VAR")], 1.43)
        # 'wodd' yoksa 'odd' kullanılır; market adındaki şablon doldurulur
        self.assertEqual(odds[("Handikaplı Maç Sonucu -1", "1")], 2.0)

    def _fake_stats(self, url, params=None, timeout=None):
        if url.endswith("/standings/3258192"):
            return {"ss": {"n": "1. Lig 26/27"}, "g": [
                {"n": "Grup A", "t": [{"r": 1, "n": "Başka FK", "p": 10, "w": 7, "d": 2, "l": 1, "gf": 20, "ga": 8, "gd": 12, "pt": 23}]},
                {"n": "Grup B", "t": [{"r": 1, "n": "Esenler Erokspor", "p": 10, "w": 6, "d": 2, "l": 2, "gf": 18, "ga": 9, "gd": 9, "pt": 20,
                                       "lfive": ["G", "M", "G", "G", "B"]}]}]}
        history = {
            0: [{"t": 1792924200, "l": {"n": "1. Lig"}, "h": {"n": "Batman Petrolspor", "fhs": 0, "rs": 3},
                 "a": {"n": "Esenler Erokspor", "fhs": 0, "rs": 2}, "mr": "M"}],
            1: [],
            2: [{"t": 1776603600, "l": {"n": "1. Lig"}, "h": {"n": "Esenler Erokspor", "fhs": 0, "rs": 1},
                 "a": {"n": "Ankara Keçiörengücü", "fhs": 0, "rs": 0}}],
        }[params["matchHistoryType"]]
        return {"h": {"n": "Esenler Erokspor"}, "a": {"n": "Ankara Keçiörengücü"}, "m": history}

    def test_special_weekly_events_are_not_matches(self):
        special = {"i": 3357273, "bri": 0, "hn": "LaLiga", "an": "11. Hafta (10 Maç)", "ci": 69907, "d": 1793390400, "m": []}
        real = {**self.EVENT}
        responses = {"/sportsbook/competitions": [], "/sportsbook/get_market_config": {"m": {}},
                     "/sportsbook/events": {"events": [special, real]}}
        def fake_get(url, params=None, timeout=None):
            return next(v for k, v in responses.items() if url.endswith(k))
        with mock.patch.object(iddaa_api, "_get_json", side_effect=fake_get):
            events = iddaa_api.fetch_bulletin()
        # Futbol + basketbol isteği aynı yanıtı döner; her ikisinde de özel etkinlik atlanır
        self.assertEqual({e["C"] for e in events}, {3258192})
        self.assertFalse(iddaa_api.is_real_match(special))
        self.assertTrue(iddaa_api.is_real_match(real))

    def test_match_stats_converted(self):
        with mock.patch.object(iddaa_api, "_get_json", side_effect=self._fake_stats):
            stats = iddaa_api.fetch_match_stats("3258192")

        self.assertEqual(stats["home_last"], [{"date": "2026-10-25", "league": "1. Lig", "home": "Batman Petrolspor",
                                               "away": "Esenler Erokspor", "score": "3-2", "ht_score": "0-0", "result": "M"}])
        self.assertIsNone(stats["away_last"])  # boş liste -> None (save_match_stats NULL yazar)
        self.assertEqual(stats["h2h_data"][0]["score"], "1-0")
        self.assertNotIn("result", stats["h2h_data"][0])
        self.assertIsNone(stats["missing"])
        # Takımların bulunduğu grup seçilir
        self.assertEqual(stats["league_table"]["Group"], "Grup B")
        self.assertEqual(stats["league_table"]["LeagueTable"][0]["Points"], 20)

    def test_match_results_only_finished(self):
        data = {
            "events": [{"eventId": 1, "homeTeam": "A", "awayTeam": "B"}, {"eventId": 2, "homeTeam": "C", "awayTeam": "D"},
                       {"eventId": 3, "homeTeam": "E", "awayTeam": "F"}],
            "sc": {"1": {"s": 6, "ht": {"r": 2, "ht": 1}, "at": {"r": 1, "ht": 0}},
                   "2": {"s": 12, "ht": {"r": 1, "ht": 0}, "at": {"r": 1, "ht": 1}}}
        }
        with mock.patch.object(iddaa_api, "_get_json", return_value=data) as get:
            results = iddaa_api.fetch_match_results("2026-10-28")
        self.assertEqual(get.call_args.kwargs["params"], {"sportId": 1, "date": "2026-10-28"})
        self.assertEqual(results, [{"code": "1", "home_team": "A", "away_team": "B", "ht_home": 1, "ht_away": 0, "ft_home": 2, "ft_away": 1}])

    def test_get_json_handles_errors(self):
        with mock.patch.object(iddaa_api.requests, "get", side_effect=iddaa_api.requests.ConnectionError()):
            self.assertIsNone(iddaa_api._get_json("https://x"))
        failed = mock.MagicMock(status_code=200, json=lambda: {"isSuccess": False, "error": {"code": "EventNotFound"}})
        with mock.patch.object(iddaa_api.requests, "get", return_value=failed):
            self.assertIsNone(iddaa_api._get_json("https://x"))

class TestSourceSwitchAndSync(unittest.TestCase):

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        database.init_db(TEST_DB)
        import web_server
        self.client = web_server.app.test_client()

    def tearDown(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def test_league_matched_by_external_id_keeps_name_and_gemini_flag(self):
        with database.db_session(TEST_DB) as conn:
            # Nesine döneminden kalan lig ve maç (external_id yok)
            league_id = database.get_or_create_league(conn, "İspanya 2. Lig", gemini_analysis=1)
            database.save_match(conn, "OLD_1", "Takım A", "Takım B", "İspanya 2. Lig", "01.12.2099", "20:00")

            # iddaa.com aynı lige farklı isim veriyor; eski maç bültende hâlâ var -> lig ID'si geriye dönük atanır
            events = [
                {"C": "OLD_1", "TYPE": "1", "HN": "Takım A", "AN": "Takım B", "D": "01.12.2099", "T": "20:00",
                 "league_name": "İspanya La Liga 2", "league_external_id": 77},
                {"C": "NEW_1", "TYPE": "1", "HN": "Takım C", "AN": "Takım D", "D": "02.12.2099", "T": "20:00",
                 "league_name": "İspanya La Liga 2", "league_external_id": 77},
                {"C": "NEW_2", "TYPE": "1", "HN": "Takım E", "AN": "Takım F", "D": "02.12.2099", "T": "21:00",
                 "league_name": "Yeni Lig", "league_external_id": 88},
            ]
            database.sync_bulletin_matches_and_odds(conn, events, ("1",))

            new_1 = database.get_match_by_code(conn, "NEW_1")
            self.assertEqual(new_1["league_id"], league_id)
            self.assertEqual(new_1["league_name"], "İspanya 2. Lig")
            self.assertTrue(new_1["league_gemini_analysis"])
            new_2 = database.get_match_by_code(conn, "NEW_2")
            self.assertEqual(new_2["league_name"], "Yeni Lig")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM leagues").fetchone()[0], 2)

    def test_sync_results_saves_finished_matches(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        yesterday = datetime.now(ZoneInfo(config.SOURCE_TIMEZONE)) - timedelta(days=1)
        old = datetime.now(ZoneInfo(config.SOURCE_TIMEZONE)) - timedelta(days=30)
        with database.db_session(TEST_DB) as conn:
            for code, dt in (("R1", yesterday), ("R2", yesterday), ("R_OLD", old)):
                database.save_match(conn, code, f"{code} Ev", f"{code} Dep", "Lig", dt.strftime("%d.%m.%Y"), "18:00")

        # R2: ilk yarı skoru yok (uzatma / penaltı vb.) -> futbolda yazılmaz
        fetched = [{"code": "R1", "ht_home": 1, "ht_away": 0, "ft_home": 3, "ft_away": 1},
                   {"code": "R2", "ht_home": None, "ht_away": None, "ft_home": 0, "ft_away": 0}]
        with mock.patch.object(data_source, "fetch_match_results", return_value=fetched) as fetch:
            res = self.client.post("/api/admin/sync_results", json={"days": 7}).get_json()
            results_sync.wait(10)
        fetch.assert_called_once_with(yesterday.strftime("%Y-%m-%d"), is_basketball=False)
        self.assertTrue(res["job"]["started"])
        self.assertEqual(res["job"]["total"], 2)

        job = self.client.get("/api/admin/jobs/results").get_json()["job"]
        self.assertEqual((job["status"], job["processed"], job["updated"], job["missing"], job["percent"]),
                         ("done", 2, 1, 1, 100.0))

        with database.db_session(TEST_DB) as conn:
            r1 = database.get_match_result(conn, database.get_match_by_code(conn, "R1")["id"])
            self.assertEqual((r1["ft_home_score"], r1["ft_away_score"], r1["is_over_3_goals"]), (3, 1, 1))
            self.assertIsNone(database.get_match_result(conn, database.get_match_by_code(conn, "R2")["id"]))

        # İkinci çalıştırma: Skoru olan maçlar (R1) kesinlikle tekrar sorgulanmaz / güncellenmez
        with mock.patch.object(data_source, "fetch_match_results", return_value=fetched) as fetch2:
            res2 = self.client.post("/api/admin/sync_results", json={"days": 7}).get_json()
            results_sync.wait(10)
        # Sadece skoru olmayan R2 için 1 maç işlenir
        job2 = self.client.get("/api/admin/jobs/results").get_json()["job"]
        self.assertEqual(job2["total"], 1)

        with mock.patch.object(config, "DATA_SOURCE", "nesine"):
            self.assertEqual(self.client.post("/api/admin/sync_results", json={}).status_code, 400)

    def test_sync_stats_background_job(self):
        with database.db_session(TEST_DB) as conn:
            for code in ("S1", "S2"):
                database.save_match(conn, code, f"{code} Ev", f"{code} Dep", "Lig", "01.12.2099", "20:00")

        stats = {"h2h_data": [{"score": "1-0"}], "home_last": None, "away_last": None, "missing": None, "league_table": None}
        empty = {"h2h_data": None, "home_last": None, "away_last": None, "missing": None, "league_table": None}
        with mock.patch.object(data_source, "fetch_match_stats", side_effect=[stats, empty]) as fetch:
            started = self.client.post("/api/admin/sync_stats", json={}).get_json()["job"]
            stats_sync.wait(10)
        self.assertTrue(started["started"])
        self.assertEqual(started["total"], 2)
        self.assertEqual(fetch.call_args_list[0].args, ("S1", "FOOTBALL"))

        job = self.client.get("/api/admin/jobs/stats").get_json()["job"]
        self.assertEqual((job["status"], job["processed"], job["updated"], job["empty"], job["percent"]), ("done", 2, 1, 1, 100.0))
        # Veri gelmeyen maç bir sonraki çalıştırmada yeniden denenir
        self.assertEqual(len(stats_sync.pending_matches(TEST_DB)), 1)

    def test_bulletin_sync_starts_stats_job_and_cancel(self):
        events = [{"C": f"B{i}", "HN": f"Ev {i}", "AN": f"Dep {i}", "league_name": "Lig", "D": "01.12.2099", "T": "20:00", "TYPE": "1"}
                  for i in range(3)]
        gate = threading.Event()
        first_started = threading.Event()

        def slow_fetch(code, sport_type):
            first_started.set()
            gate.wait(5)   # ilk maçta iptal isteği gelene kadar bekle
            return {"h2h_data": [{"score": "1-0"}], "home_last": [{"score": "2-0"}], "away_last": [{"score": "0-0"}],
                    "missing": None, "league_table": None}

        with mock.patch.object(data_source, "fetch_bulletin", return_value=events), \
             mock.patch.object(data_source, "fetch_match_stats", side_effect=slow_fetch):
            res = self.client.post("/api/admin/sync_bulletin", json={}).get_json()
            self.assertTrue(res["stats_job"]["started"])
            self.assertEqual(res["stats_job"]["total"], 3)
            # Çalışırken ikinci başlatma yeni iş açmaz
            self.assertFalse(self.client.post("/api/admin/sync_stats").get_json()["job"]["started"])
            self.assertTrue(first_started.wait(5))
            self.client.post("/api/admin/jobs/stats/cancel")
            gate.set()
            stats_sync.wait(10)
        job = stats_sync.get_status()
        self.assertEqual((job["status"], job["processed"]), ("cancelled", 1))
        self.assertEqual(len(stats_sync.pending_matches(TEST_DB)), 2)

    def test_source_selection(self):
        with mock.patch.object(config, "DATA_SOURCE", "iddaa"), \
             mock.patch.object(iddaa_api, "fetch_bulletin", return_value=["i"]), mock.patch.object(nesine_api, "fetch_bulletin", return_value=["n"]):
            self.assertEqual((data_source.fetch_bulletin(), data_source.source_name()), (["i"], "iddaa.com"))
        with mock.patch.object(config, "DATA_SOURCE", "nesine"), \
             mock.patch.object(iddaa_api, "fetch_bulletin", return_value=["i"]), mock.patch.object(nesine_api, "fetch_bulletin", return_value=["n"]):
            self.assertEqual((data_source.fetch_bulletin(), data_source.source_name()), (["n"], "Nesine"))
            self.assertIsNone(data_source.fetch_match_results("2026-10-28"))

    def test_results_sync_cancel_and_idle(self):
        # Bekleyen maç yoksa job total 0 olarak hemen biter
        res = self.client.post("/api/admin/sync_results", json={"days": 7}).get_json()
        self.assertFalse(res["job"]["started"])
        self.assertEqual(res["job"]["status"], "done")

        # İptal etme testi
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        yesterday = datetime.now(ZoneInfo(config.SOURCE_TIMEZONE)) - timedelta(days=1)
        with database.db_session(TEST_DB) as conn:
            for i in range(3):
                database.save_match(conn, f"C_CANCEL_{i}", f"Ev {i}", f"Dep {i}", "Lig", yesterday.strftime("%d.%m.%Y"), "18:00")

        gate = threading.Event()
        first_started = threading.Event()

        def slow_fetch(date_str, is_basketball=False):
            first_started.set()
            gate.wait(5)
            return [{"code": "C_CANCEL_0", "ht_home": 1, "ht_away": 0, "ft_home": 2, "ft_away": 1}]

        with mock.patch.object(data_source, "fetch_match_results", side_effect=slow_fetch):
            started = self.client.post("/api/admin/sync_results", json={"days": 7}).get_json()["job"]
            self.assertTrue(started["started"])
            self.assertTrue(first_started.wait(5))
            self.client.post("/api/admin/jobs/results/cancel")
            gate.set()
            results_sync.wait(10)
        job = results_sync.get_status()
        # İlk maç sürerken iptal edildi: kalan 2 maç işlenmez
        self.assertEqual((job["status"], job["processed"]), ("cancelled", 1))

    def test_coupon_generator_and_league_weighting(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        started_at = datetime.now(ZoneInfo(config.SOURCE_TIMEZONE)) - timedelta(minutes=10)

        def bet(selection, odds, confidence, market="ft"):
            return {"market": market, "selection": selection, "odds": odds, "confidence": confidence, "reason": "r"}

        def predict(conn, code, league, best, alt=None, date="01.01.2099", time="20:00"):
            mid = database.save_match(conn, code, f"{code} Ev", f"{code} Dep", league, date, time)
            database.save_prediction(conn, mid, {"predicted_ft_result": "MS 1", "predicted_score": "2-0", "confidence_score": 70,
                                                 "best_bet": best, "alt_bet": alt})
            return mid

        with database.db_session(TEST_DB) as conn:
            # Geçmiş: 'Başarılı' ligde ⭐ 3/3, 'Başarısız' ligde 1/3 tuttu
            for i in range(3):
                database.save_result(conn, predict(conn, f"PA{i}", "Başarılı", bet("MS 1", 1.5, 70), date="01.01.2020"), 1, 0, 2, 0)
                database.save_result(conn, predict(conn, f"PB{i}", "Başarısız", bet("MS 1", 1.5, 70), date="01.01.2020"),
                                     0, 0, 2 if i == 0 else 0, 1)
            predict(conn, "FUT_A", "Başarılı", bet("MS 1", 1.50, 80))
            predict(conn, "FUT_B", "Başarısız", bet("MS 1", 1.50, 82))
            # ⭐ oranı düşük (1.10) -> 2. tercih (KG VAR 1.70) kullanılır
            predict(conn, "FUT_ALT", "Üçüncü Lig", bet("MS 1", 1.10, 95), bet("KG VAR", 1.70, 60, "kg"))
            # ⭐ düşük ve 2. tercih yok -> kupona girmez
            predict(conn, "FUT_LOW", "Üçüncü Lig", bet("MS 1", 1.20, 98))
            # Analiz edilmemiş ve başlamış maçlar kupona girmez
            database.save_match(conn, "FUT_NO_PRED", "C1", "C2", "Başarılı", "01.01.2099", "20:00")
            predict(conn, "STARTED", "Başarılı", bet("MS 1", 1.60, 90),
                    date=started_at.strftime("%d.%m.%Y"), time=started_at.strftime("%H:%M"))

        # .env / config varsayılanı: maç >= 1.30, kupon >= 2.00 (kullanıcının .env değerinden bağımsız)
        with mock.patch.multiple(config, MIN_MATCH_ODDS=1.30, MIN_COUPON_TOTAL_ODDS=2.00):
            slip = coupon.generate_coupon(match_count=3, today_only=False, strategy="banko", db_path=TEST_DB)
        self.assertTrue(slip["success"], slip.get("message"))
        by_code = {m["code"]: m for m in slip["matches"]}
        self.assertEqual(set(by_code), {"FUT_A", "FUT_B", "FUT_ALT"})
        self.assertEqual((by_code["FUT_ALT"]["pick"], by_code["FUT_ALT"]["selection"], by_code["FUT_ALT"]["odds"]), ("alt_bet", "KG VAR", 1.70))
        self.assertEqual(slip["total_odds"], round(1.5 * 1.5 * 1.7, 2))
        # Lig isabeti ağırlığı: güveni daha düşük olsa da başarılı ligdeki maç daha yüksek puan alır
        self.assertGreater(by_code["FUT_A"]["coupon_score"], by_code["FUT_B"]["coupon_score"])
        self.assertIn("Yüksek lig başarısı", by_code["FUT_A"]["league_note"])
        self.assertIn("Düşük lig başarısı", by_code["FUT_B"]["league_note"])

        # Kurallar Ayarlar'dan dinamik okunur: min maç oranı 1.60 -> sadece 2. tercihli maç kalır
        self.client.post("/api/admin/settings/coupon_odds", json={"min_match_odds": 1.60, "min_coupon_odds": 1.50})
        res = self.client.post("/api/coupon/generate", json={"match_count": 1, "today_only": False}).get_json()
        self.assertEqual(([m["code"] for m in res["matches"]], res["odds_rules"]["min_match_odds"]), (["FUT_ALT"], 1.60))
        # Min kupon oranı 2.00 -> 1.70'lik tek maçlı kupon kurulamaz; istek kuralları ezemez
        self.client.post("/api/admin/settings/coupon_odds", json={"min_match_odds": 1.60, "min_coupon_odds": 2.00})
        res = self.client.post("/api/coupon/generate", json={"match_count": 1, "today_only": False, "min_total_odds": 1.01}).get_json()
        self.assertFalse(res["success"])
        self.assertIn("2.00", res["message"])
        self.assertEqual(self.client.post("/api/coupon/generate", json={"match_count": "x"}).status_code, 400)

    def test_select_combination_respects_min_total_and_league_limit(self):
        def item(code, league, odds, score):
            return {"match": {"code": code, "league_id": league}, "pick": {"odds": odds}, "score": score}
        scored = [item("a", 1, 1.3, 90), item("b", 1, 1.3, 85), item("c", 1, 1.4, 80), item("d", 2, 1.6, 50)]
        codes = lambda combo: [c["match"]["code"] for c in combo]
        # a+b (1.69) ve a+c (1.82) toplam 2.00'ın altında: puanı en yüksek geçerli ikili a+d (2.08)
        self.assertEqual(codes(coupon.select_combination(scored, 2, 2.0)), ["a", "d"])
        # 3 maçta lig sınırı (2) korunur: a, b, d
        self.assertEqual(codes(coupon.select_combination(scored, 3, 2.0)), ["a", "b", "d"])
        self.assertIsNone(coupon.select_combination(scored, 2, 5.0))

    def test_played_coupon_saved_and_evaluated_from_results(self):
        def bet(selection, odds, market):
            return {"market": market, "selection": selection, "odds": odds, "confidence": 70, "reason": "r"}
        with database.db_session(TEST_DB) as conn:
            m1 = database.save_match(conn, "K1", "A", "B", "Lig", "01.01.2099", "20:00")
            database.save_prediction(conn, m1, {"predicted_score": "2-0", "best_bet": bet("MS 1", 1.50, "ft")})
            m2 = database.save_match(conn, "K2", "C", "D", "Lig", "01.01.2099", "21:00")
            database.save_prediction(conn, m2, {"predicted_score": "1-1", "best_bet": bet("MS 0", 1.10, "ft"),
                                                "alt_bet": bet("KG VAR", 1.80, "kg")})

        # Oran / tercih istemciden alınmaz: maçın kayıtlı tahmininden okunur
        res = self.client.post("/api/coupons", json={"strategy": "banko", "matches": [
            {"id": m1, "pick": "best_bet", "odds": 99}, {"id": m2, "pick": "alt_bet"}]}).get_json()
        self.assertTrue(res["success"])
        data = self.client.get("/api/coupons").get_json()
        saved = data["coupons"][0]
        self.assertEqual((saved["status"], saved["total_odds"]), ("pending", 2.7))
        self.assertEqual([(i["selection"], i["odds"]) for i in saved["items"]], [("MS 1", 1.5), ("KG VAR", 1.8)])
        self.assertEqual(data["summary"], {"total": 1, "won": 0, "lost": 0, "pending": 1, "win_rate": None})

        # Skorlar gelince otomatik değerlendirilir: biri tuttu -> hâlâ bekliyor; ikisi de tuttu -> tuttu
        with database.db_session(TEST_DB) as conn:
            database.save_result(conn, m1, 1, 0, 2, 0)
        self.assertEqual(self.client.get("/api/coupons").get_json()["coupons"][0]["status"], "pending")
        with database.db_session(TEST_DB) as conn:
            database.save_result(conn, m2, 0, 0, 1, 1)
        data = self.client.get("/api/coupons").get_json()
        self.assertEqual(data["coupons"][0]["status"], "won")
        self.assertEqual([i["result"] for i in data["coupons"][0]["items"]], [True, True])
        self.assertEqual(data["summary"]["win_rate"], 100.0)
        # Skor düzeltilip tercih yatarsa kupon da yatar
        with database.db_session(TEST_DB) as conn:
            database.save_result(conn, m2, 0, 0, 1, 0)
        self.assertEqual(self.client.get("/api/coupons").get_json()["coupons"][0]["status"], "lost")

        # Geçersiz istekler
        for body in ({"matches": []}, {"matches": [{"id": m1, "pick": "x"}]},
                     {"matches": [{"id": m1, "pick": "alt_bet"}]}, {"matches": [{"id": m1, "pick": "best_bet"}] * 2}):
            self.assertEqual(self.client.post("/api/coupons", json=body).status_code, 400, body)

        coupon_id = saved["id"]
        self.assertTrue(self.client.delete(f"/api/coupons/{coupon_id}").get_json()["success"])
        self.assertEqual(self.client.delete(f"/api/coupons/{coupon_id}").status_code, 404)
        self.assertEqual(self.client.get("/api/coupons").get_json()["coupons"], [])

    def test_exclude_played_matches_from_new_coupons(self):
        with database.db_session(TEST_DB) as conn:
            ids = []
            for code in ("P1", "P2", "P3"):
                mid = database.save_match(conn, code, f"{code} Ev", f"{code} Dep", f"Lig {code}", "01.01.2099", "20:00")
                database.save_prediction(conn, mid, {"predicted_score": "1-0", "best_bet": {
                    "market": "ft", "selection": "MS 1", "odds": 1.50, "confidence": 70, "reason": "r"}})
                ids.append(mid)
        self.client.post("/api/coupons", json={"strategy": "banko", "matches": [{"id": ids[0], "pick": "best_bet"}]})

        def generate(exclude):
            with mock.patch.multiple(config, MIN_MATCH_ODDS=1.30, MIN_COUPON_TOTAL_ODDS=1.10):
                return self.client.post("/api/coupon/generate", json={"match_count": 3, "today_only": False,
                                                                      "exclude_played": exclude}).get_json()
        # İşaret: oynanan maç listede ve kupon önerisinde 'played'
        played = {m["code"]: m["played"] for m in self.client.get("/api/matches/unfinished").get_json()["matches"]}
        self.assertEqual(played, {"P1": True, "P2": False, "P3": False})
        included = generate(False)
        self.assertTrue(included["success"])
        self.assertEqual({m["code"]: m["played"] for m in included["matches"]}, {"P1": True, "P2": False, "P3": False})
        # Seçenek açıkken oynanan maç gelmez: 3 maçlık kupon için 2 uygun maç kalır
        excluded = generate(True)
        self.assertFalse(excluded["success"])
        self.assertIn("(2/3)", excluded["message"])
        # Kupon silinince işaret de kalkar
        coupon_id = self.client.get("/api/coupons").get_json()["coupons"][0]["id"]
        self.client.delete(f"/api/coupons/{coupon_id}")
        self.assertTrue(generate(True)["success"])

    def test_leagues_accuracy_endpoint(self):
        res = self.client.get("/api/leagues/accuracy")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("summary", data)
        self.assertIn("leagues", data)
        summary = data["summary"]
        for key in ("best_bet_accuracy", "alt_bet_accuracy", "best_bet_total", "alt_bet_total", "total_evaluated_matches"):
            self.assertIn(key, summary)
        self.assertIsInstance(data["leagues"], list)

if __name__ == "__main__":
    unittest.main()
