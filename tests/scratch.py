"""Workspace-contained test folders with ordinary inherited Windows ACLs."""
import shutil
import uuid
from pathlib import Path


class TestDirectory:
    def __init__(self, prefix="case-"):
        self.root = Path(__file__).resolve().parents[1] / ".test-tmp"
        self.root.mkdir(exist_ok=True)
        self.path = self.root / (prefix + uuid.uuid4().hex)
        self.path.mkdir()
        self.name = str(self.path)

    def __enter__(self):
        return self.name

    def __exit__(self, *args):
        self.cleanup()

    def cleanup(self):
        if self.path.resolve().parent != self.root.resolve():
            raise RuntimeError("Test cleanup escaped workspace")
        if self.path.exists():
            shutil.rmtree(self.path)
