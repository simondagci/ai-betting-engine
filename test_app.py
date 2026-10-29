import unittest
import os
import sqlite3
import config
import database
import nesine_api
import gemini_engine

TEST_DB = "test_betting_engine.db"

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

if __name__ == "__main__":
    unittest.main()
