import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import market_watch


class MarketWatchTests(unittest.TestCase):
    def test_email_body_includes_alert_url_once(self) -> None:
        alert = market_watch.Alert(
            category="news",
            title="Market event",
            severity="high",
            summary="Summary",
            url="https://example.com/event",
            score=90,
        )

        text_body, _ = market_watch.build_email_body("Test", [alert], [])

        self.assertEqual(text_body.count("URL: https://example.com/event"), 1)

    def test_run_once_marks_only_emailable_alerts_as_emailed(self) -> None:
        config = {"alerting": {"min_email_score": 55}}
        high = market_watch.Alert("news", "High score", "high", "Summary", score=90)
        low = market_watch.Alert("news", "Low score", "medium", "Summary", score=40)

        with tempfile.TemporaryDirectory() as directory:
            conn = market_watch.init_db(Path(directory) / "test.sqlite3")
            dashboard = Path(directory) / "dashboard.html"
            with patch.object(market_watch, "collect_alerts", return_value=([high, low], [])):
                with patch.object(market_watch, "send_email", return_value=True):
                    market_watch.run_once(config, conn, dashboard)

            rows = dict(conn.execute("SELECT title, emailed FROM alerts").fetchall())
            conn.close()

        self.assertEqual(rows, {"High score": 1, "Low score": 0})

    def test_alert_email_batching_and_critical_override(self) -> None:
        config = {"alerting": {"min_email_score": 55, "critical_email_score": 85, "batch_email_minutes": 60}}
        first = market_watch.Alert("news", "First notable", "medium", "Summary", score=60)
        second = market_watch.Alert("news", "Second notable", "medium", "Summary", score=62)
        critical = market_watch.Alert("market", "Critical move", "high", "Summary", score=92)

        with tempfile.TemporaryDirectory() as directory:
            conn = market_watch.init_db(Path(directory) / "test.sqlite3")
            dashboard = Path(directory) / "dashboard.html"
            with patch.object(market_watch, "send_email", return_value=True) as send:
                with patch.object(market_watch, "collect_alerts", return_value=([first], [])):
                    market_watch.run_once(config, conn, dashboard)
                self.assertEqual(send.call_count, 1)

                with patch.object(market_watch, "collect_alerts", return_value=([second], [])):
                    market_watch.run_once(config, conn, dashboard)
                self.assertEqual(send.call_count, 1, "notable alert inside batch window must not email")

                with patch.object(market_watch, "collect_alerts", return_value=([critical], [])):
                    market_watch.run_once(config, conn, dashboard)
                self.assertEqual(send.call_count, 2, "critical alert must bypass the batch window")

            rows = dict(conn.execute("SELECT title, emailed FROM alerts").fetchall())
            conn.close()

        self.assertEqual(
            rows,
            {"First notable": 1, "Second notable": 1, "Critical move": 1},
            "pending notable alert should ride along with the critical email",
        )

    def test_earnings_reminder_emailed_flag_matches_send_result(self) -> None:
        config = {"alerting": {"min_email_score": 55}}
        earnings = market_watch.Alert(
            "stock",
            "Apple earnings in 3 day(s)",
            "medium",
            "Summary",
            source="FMP/Nasdaq earnings calendar",
            score=62,
        )
        low_score_news = market_watch.Alert("news", "Low score news", "medium", "Summary", score=10)

        with tempfile.TemporaryDirectory() as directory:
            conn = market_watch.init_db(Path(directory) / "test.sqlite3")
            dashboard = Path(directory) / "dashboard.html"
            with patch.object(market_watch, "collect_alerts", return_value=([earnings, low_score_news], [])):
                with patch.object(market_watch, "send_email", return_value=True):
                    market_watch.run_once(config, conn, dashboard)

            rows = dict(conn.execute("SELECT title, emailed FROM alerts").fetchall())
            conn.close()

        self.assertEqual(rows, {"Apple earnings in 3 day(s)": 1, "Low score news": 0})

    def test_import_historical_market_data_does_not_duplicate_on_rerun(self) -> None:
        config = {
            "market_data": {
                "instruments": [{"symbol": "TEST", "name": "Test Instrument"}],
            }
        }
        chart_result = {
            "timestamp": [1000, 2000, 3000],
            "indicators": {"quote": [{"close": [1.0, 2.0, 3.0]}]},
        }

        with tempfile.TemporaryDirectory() as directory:
            conn = market_watch.init_db(Path(directory) / "test.sqlite3")
            with patch.object(market_watch, "yahoo_chart", return_value=chart_result):
                market_watch.import_historical_market_data(config, conn, days=5)
                market_watch.import_historical_market_data(config, conn, days=5)

            count = conn.execute("SELECT COUNT(*) FROM metric_history").fetchone()[0]
            conn.close()

        self.assertEqual(count, 3)

    def test_price_move_alerts_dedupe_within_window(self) -> None:
        first = market_watch.Alert("stock", "AMD moved up 7.53%", "high", "Summary", value="556.81 (+7.53%)", score=90)
        repeat = market_watch.Alert("stock", "AMD moved up 7.38%", "high", "Summary", value="556.03 (+7.38%)", score=89)
        reverse = market_watch.Alert("stock", "AMD moved down 4.10%", "high", "Summary", value="534.00 (-4.10%)", score=70)

        with tempfile.TemporaryDirectory() as directory:
            conn = market_watch.init_db(Path(directory) / "test.sqlite3")
            market_watch.save_alert(conn, first, False)

            self.assertTrue(market_watch.seen_alert(conn, repeat), "same direction move must dedupe")
            self.assertFalse(market_watch.seen_alert(conn, reverse), "direction change is a new event")
            conn.close()

    def test_news_topic_cooldown_suppresses_rewordings(self) -> None:
        config = {"news": {"topic_cooldown_hours": 4, "topic_escalation_score": 15}}
        original = market_watch.Alert("news", "Oil prices fall 2% as tensions ease", "medium", "Matched: oil, iran", score=60)
        reworded = market_watch.Alert("news", "Crude slides as markets bet on de-escalation", "medium", "Matched: oil, iran", score=62)
        superset = market_watch.Alert("news", "Oil drops after strikes pause", "medium", "Matched: oil, war, iran", score=64)
        escalated = market_watch.Alert("news", "Oil spikes as strait closed", "high", "Matched: oil, iran", score=85)
        unrelated = market_watch.Alert("news", "Fed weighs rate cut in September", "medium", "Matched: fed, rate cut", score=60)

        with tempfile.TemporaryDirectory() as directory:
            conn = market_watch.init_db(Path(directory) / "test.sqlite3")
            market_watch.save_alert(conn, original, False)

            self.assertTrue(market_watch.seen_alert(conn, reworded, config), "same topic rewording must be suppressed")
            self.assertTrue(market_watch.seen_alert(conn, superset, config), "superset topic within cooldown must be suppressed")
            self.assertFalse(market_watch.seen_alert(conn, escalated, config), "score escalation must pass through")
            self.assertFalse(market_watch.seen_alert(conn, unrelated, config), "different topic must pass through")
            conn.close()

    def test_econ_calendar_alerts_on_release_day(self) -> None:
        config = {"econ_calendar": {"enabled": True, "alert_days_before": [1, 0], "horizon_days": 3, "events": ["CPI"]}}
        today = market_watch.local_now().date()
        rows_by_offset = {
            1: [
                {"country": "United States", "eventName": "CPI", "gmt": "08:30"},
                {"country": "United States", "eventName": "CPI, n.s.a", "gmt": "08:30"},
                {"country": "Germany", "eventName": "CPI", "gmt": "02:00"},
            ],
        }

        def fake_events(day):
            return rows_by_offset.get((day - today).days, [])

        with patch.object(market_watch, "nasdaq_econ_events_on_date", side_effect=fake_events):
            alerts, snapshots = market_watch.scan_econ_calendar(config, None)

        self.assertEqual(len(snapshots), 1)
        self.assertIn("Next CPI", snapshots[0].name)
        self.assertEqual(len(alerts), 1)
        self.assertIn("CPI release in 1 day(s)", alerts[0].title)
        self.assertIn("08:30 ET", alerts[0].value)

    def test_monitor_health_badge_reflects_poll_age(self) -> None:
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as directory:
            conn = market_watch.init_db(Path(directory) / "test.sqlite3")

            self.assertIn("No poll recorded yet", market_watch.monitor_health_badge(conn))

            market_watch.set_state(conn, "last_poll_at", market_watch.utc_now())
            self.assertIn("Monitor active", market_watch.monitor_health_badge(conn))

            stale = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S UTC")
            market_watch.set_state(conn, "last_poll_at", stale)
            self.assertIn("monitor may be stopped", market_watch.monitor_health_badge(conn))
            conn.close()


if __name__ == "__main__":
    unittest.main()
