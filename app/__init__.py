# app/main.py or app/__init__.py
from pathlib import Path
from app.signals import register_global_signals

APPLICATIONS_DIR = Path(__file__).parent.parent / "applications"
register_global_signals(APPLICATIONS_DIR)
