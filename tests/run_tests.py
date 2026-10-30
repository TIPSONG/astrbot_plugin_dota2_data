"""Run offline tests with a disposable AstrBot root, never the live Bot's data."""
import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path
from scratch import TestDirectory

parser = argparse.ArgumentParser()
parser.add_argument("--astrbot-app", type=Path, help="Directory containing the astrbot package")
args = parser.parse_args()
repo = Path(__file__).resolve().parents[1]
scratch = repo / ".test-tmp"
scratch.mkdir(exist_ok=True)
# Keep all temporary writes inside the checkout in restricted desktop sandboxes.
tempfile.tempdir = str(scratch)
sys.path.insert(0, str(repo.parent))
if args.astrbot_app:
    sys.path.insert(0, str(args.astrbot_app.resolve()))
with TestDirectory(prefix="dota2-offline-") as sandbox:
    os.environ["ASTRBOT_ROOT"] = sandbox
    sys.dont_write_bytecode = True
    suite = unittest.defaultTestLoader.discover(str(repo / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    # Framework logging can retain an open file on Windows.
    import logging
    logging.shutdown()
    if "astrbot.core" in sys.modules:
        from astrbot.core.log import LogManager
        # Close ordinary Python handlers without touching live framework processes.
        for logger in logging.Logger.manager.loggerDict.values():
            if isinstance(logger, logging.Logger):
                for handler in logger.handlers[:]:
                    handler.close()
                    logger.removeHandler(handler)
sys.exit(0 if result.wasSuccessful() else 1)
