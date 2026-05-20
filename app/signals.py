import importlib
import inspect
# from pathlib import Path
from tortoise.models import Model
from tortoise.signals import post_save, pre_save, post_delete, pre_delete
# from app.utils.auto_routing import get_module
from pathlib import Path


async def generic_post_save(sender, instance, created, using_db, update_fields):
    print(f"[SIGNAL] {sender.__name__} POST_SAVE -> {instance}")


async def generic_pre_save(sender, instance, using_db, update_fields):
    print(f"[SIGNAL] {sender.__name__} PRE_SAVE -> {instance}")


async def generic_pre_delete(sender, instance, using_db):
    print(f"[SIGNAL] {sender.__name__} PRE_DELETE -> {instance}")


async def generic_post_delete(sender, instance, using_db):
    print(f"[SIGNAL] {sender.__name__} POST_DELETE -> {instance}")


# ---------- GLOBAL REGISTRATION ----------
def register_global_signals(applications_dir: Path):
    for app_dir in applications_dir.iterdir():
        if not app_dir.is_dir():
            continue
        model_files = [
            f for f in app_dir.glob("*.py")
            if f.name not in ["__init__.py", "__pycache__.py"] and not f.name == "signals.py"
        ]

        for file in model_files:
            module_path = f"applications.{app_dir.name}.{file.stem}"
            try:
                module = importlib.import_module(module_path)
            except Exception as e:
                print(f"⚠️ Could not import {module_path}: {e}")
                continue

            for name, cls in inspect.getmembers(module, inspect.isclass):
                if issubclass(cls, Model) and cls is not Model and hasattr(cls, "_meta"):
                    pre_save(cls)(generic_pre_save)
                    post_save(cls)(generic_post_save)
                    pre_delete(cls)(generic_pre_delete)
                    post_delete(cls)(generic_post_delete)
                    print(f"[SIGNAL] Generic signals registered for {app_dir.name}.{name}")
