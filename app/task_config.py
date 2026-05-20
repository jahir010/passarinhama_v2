import importlib
import pkgutil
import inspect
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

import tasks

scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

def is_task(func):
    return callable(func) and not func.__name__.startswith("_")

def load_tasks():
    print("🔍 Scanning task modules...")
    for loader, module_name, is_pkg in pkgutil.iter_modules(tasks.__path__):
        print(f"📦 Importing module: tasks.{module_name}")
        module = importlib.import_module(f"tasks.{module_name}")

        for name, func in inspect.getmembers(module, is_task):
            schedule = getattr(func, "_schedule", None)
            job_id = f"{module_name}_{name}"  # unique job ID

            try:
                if schedule:
                    if "seconds" in schedule or "minutes" in schedule:
                        scheduler.add_job(func, IntervalTrigger(**schedule), id=job_id)
                    else:
                        scheduler.add_job(func, CronTrigger(**schedule), id=job_id)
                else:
                    scheduler.add_job(func, IntervalTrigger(minutes=1), id=job_id)

                print(f"   ✔ Job added: {job_id} -> {schedule}")
            except Exception as e:
                print(f"   ❌ Error scheduling {job_id}: {e}")

def start_scheduler():
    load_tasks()
    print("🚀 Starting APScheduler...")
    scheduler.start()
    print("✅ Scheduler started and running...")

def start():
    threading.Thread(target=start_scheduler, daemon=True).start()

