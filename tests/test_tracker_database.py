import tempfile
import unittest
from pathlib import Path

from src.common.tracker_database import connect_config_database, connect_content_database


class TrackerDatabaseTests(unittest.TestCase):
    def test_config_connection_initializes_all_config_tables_and_upgrades(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "AI-Tool-Session-Tracker-config.db"
            with connect_config_database(database_path) as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )}
                columns = {row[1] for row in connection.execute("PRAGMA table_info(model_costs)")}
            self.assertTrue({"source_routing", "source_otel", "model_mappings", "model_costs"}.issubset(tables))
            self.assertIn("cache_threshold", columns)
            self.assertTrue(database_path.is_file())

    def test_content_connection_initializes_only_content_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "AI-Tool-Session-Tracker-content.db"
            with connect_content_database(database_path) as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )}
                indexes = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )}
            self.assertTrue({"otel_spans", "otel_logs", "otel_metrics"}.issubset(tables))
            self.assertTrue({"idx_otel_logs_session", "idx_otel_metrics_session"}.issubset(indexes))
            self.assertNotIn("model_costs", tables)
            self.assertTrue(database_path.is_file())
