from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.utils.logging import configure_logging


class LoggingTests(unittest.TestCase):
    @staticmethod
    def _close_handlers() -> None:
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()

    def tearDown(self) -> None:
        self._close_handlers()

    def test_configure_logging_rotates_and_compresses_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                log_dir=tmp,
                log_max_bytes=128,
                log_backup_count=3,
                log_compress_backups=True,
            )

            log_path = configure_logging(settings)
            for index in range(20):
                logging.getLogger("test.rotation").info(
                    "rotation-line-%s-%s", index, "x" * 80
                )
            for handler in logging.getLogger().handlers:
                handler.flush()

            self.assertEqual(log_path, Path(tmp).resolve() / "bot.log")
            self.assertTrue(log_path.exists())
            self.assertTrue((Path(tmp) / "bot.log.1.gz").exists())
            self.assertLessEqual(len(list(Path(tmp).glob("bot.log.*.gz"))), 3)
            self._close_handlers()


if __name__ == "__main__":
    unittest.main()
