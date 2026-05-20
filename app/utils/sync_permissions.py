"""
sync_permissions.py
────────────────────────────────────────────────────────────────────────────
Utility for seeding default FeaturePermission rows for every Role × Feature
combination on application startup.

This file does NOT auto-create permissions from model introspection
(the old approach assumed a codename-based Permission model that no longer
exists).  Instead it ensures every existing Role has a FeaturePermission row
for every FEATURES value, defaulting all flags to False so admins can then
toggle them through the /permissions API.

Call `await seed_feature_permissions()` inside your Tortoise on_startup hook:

    from tortoise import Tortoise

    @app.on_event("startup")
    async def startup():
        await Tortoise.init(...)
        await generate_schemas()
        from app.sync_permissions import seed_feature_permissions
        await seed_feature_permissions()
"""

from applications.user.models import FEATURES, FeaturePermission, Role


async def seed_feature_permissions() -> None:
    """
    For every Role in the database, ensure a FeaturePermission row exists
    for each FEATURES value.  Existing rows are left untouched (their flags
    are NOT reset), so it is safe to call this on every startup.
    """
    roles = await Role.all()
    features = list(FEATURES)

    created_count = 0
    for role in roles:
        for feature in features:
            _, created = await FeaturePermission.get_or_create(
                role=role,
                feature=feature,
                defaults={
                    "can_view":   False,
                    "can_create": False,
                    "can_edit":   False,
                    "can_delete": False,
                },
            )
            if created:
                created_count += 1

    if created_count:
        print(f"[permissions] Seeded {created_count} new FeaturePermission rows.", flush=True)
    else:
        print("[permissions] All FeaturePermission rows already present.", flush=True)