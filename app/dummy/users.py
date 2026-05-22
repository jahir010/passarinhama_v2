from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from applications.user.models import Role, User, UserStatus

ROLES_DATA = [
    {
        "name": "Administrator",
        "slug": "admin",
        "description": "Full system access",
    },
    {
        "name": "Manager",
        "slug": "manager",
        "description": "Can manage operational workflows",
    },
    {
        "name": "Editor",
        "slug": "editor",
        "description": "Can create and edit content",
    },
    {
        "name": "Member",
        "slug": "member",
        "description": "Standard user role",
    },
]

USERS_DATA = [
    {
        "email": "admin@gmail.com",
        "password": "admin",
        "first_name": "Admin",
        "last_name": "User",
        "role_slug": "admin",
        "is_superuser": True,
    },
    {
        "email": "user@gmail.com",
        "password": "user",
        "first_name": "General",
        "last_name": "User",
        "role_slug": "manager",
        "is_superuser": False,
    },
    {
        "email": "user1@gmail.com",
        "password": "user",
        "first_name": "User",
        "last_name": "One",
        "role_slug": "editor",
        "is_superuser": False,
    },
    {
        "email": "user2@gmail.com",
        "password": "user",
        "first_name": "User",
        "last_name": "Two",
        "role_slug": "member",
        "is_superuser": False,
    },
    {
        "email": "user3@gmail.com",
        "password": "user",
        "first_name": "User",
        "last_name": "Three",
        "role_slug": "member",
        "is_superuser": False,
    },
]


async def _seed_roles() -> dict[str, Role]:
    role_map: dict[str, Role] = {}

    for role_data in ROLES_DATA:
        slug = role_data["slug"]
        role, _ = await Role.get_or_create(
            slug=slug,
            defaults={
                "name": role_data["name"],
                "description": role_data.get("description"),
            },
        )

        updated = False
        if role.name != role_data["name"]:
            role.name = role_data["name"]
            updated = True

        if role.description != role_data.get("description"):
            role.description = role_data.get("description")
            updated = True

        if updated:
            await role.save()

        role_map[slug] = role

    return role_map


async def create_test_users() -> None:
    role_map = await _seed_roles()
    created_count = 0
    updated_count = 0

    for data in USERS_DATA:
        email = data["email"]
        role = role_map[data["role_slug"]]

        try:
            async with in_transaction() as conn:
                defaults = {
                    "email": email,
                    "first_name": data["first_name"],
                    "last_name": data["last_name"],
                    "role_id": role.id,
                    "status": UserStatus.ACTIVE,
                    "is_superuser": data.get("is_superuser", False),
                    "is_active_2fa": False,
                    "password": User.set_password(data["password"]),
                }

                user, created = await User.get_or_create(
                    email=email,
                    defaults=defaults,
                    using_db=conn,
                )

                if created:
                    created_count += 1
                    print(f"[dummy-user] created: {email}")
                    continue

                updated = False
                for field in ["first_name", "last_name", "role_id", "status", "is_superuser", "is_active_2fa"]:
                    if getattr(user, field) != defaults[field]:
                        setattr(user, field, defaults[field])
                        updated = True

                password_valid = False
                if user.password:
                    try:
                        password_valid = user.verify_password(data["password"])
                    except Exception:
                        password_valid = False

                if not password_valid:
                    user.password = defaults["password"]
                    updated = True

                if updated:
                    await user.save(using_db=conn)
                    updated_count += 1
                    print(f"[dummy-user] updated: {email}")
                else:
                    print(f"[dummy-user] exists: {email}")

        except IntegrityError as error:
            print(f"[dummy-user] integrity error for {email}: {error}")
        except Exception as error:
            print(f"[dummy-user] unexpected error for {email}: {error}")

    print(f"[dummy-user] seeding completed (created={created_count}, updated={updated_count})")
