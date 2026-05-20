# from tortoise.exceptions import IntegrityError
# from tortoise.transactions import in_transaction

# from applications.user.models import User, UserRole

# USERS_DATA = [
#     {
#         "email": "admin@gmail.com",
#         "password": "admin",
#         "first_name": "Admin",
#         "last_name": "User",
#         "is_superuser": True,
#         "is_active": True,
#         "is_active_2fa": False,
#     },
#     {
#         "email": "staff@gmail.com",
#         "password": "staff",
#         "first_name": "Staff",
#         "last_name": "User",
#         "is_staff": True,
#         "is_active": True,
#         "is_active_2fa": False,
#     },
#     {
#         "email": "m1@gmail.com",
#         "password": "user",
#         "first_name": "Merchant",
#         "last_name": "User 1",
#         "role": UserRole.MERCHANT,
#         "is_active": True,
#         "is_active_2fa": False,
#     },
#     {
#         "email": "m2@gmail.com",
#         "password": "user",
#         "first_name": "Merchant",
#         "last_name": "User 2",
#         "role": UserRole.MERCHANT,
#         "is_active": True,
#         "is_active_2fa": False,
#     },
#     {
#         "email": "m3@gmail.com",
#         "password": "user",
#         "first_name": "Merchant",
#         "last_name": "User 3",
#         "role": UserRole.MERCHANT,
#         "is_active": True,
#         "is_active_2fa": True,
#     },
#     {
#         "email": "v1@gmail.com",
#         "password": "user",
#         "first_name": "Virtual",
#         "last_name": "Assistant 1",
#         "role": UserRole.VIRTUAL_ASSISTANT,
#         "is_active": True,
#         "is_active_2fa": False,
#     },
#     {
#         "email": "v2@gmail.com",
#         "password": "user",
#         "first_name": "Virtual",
#         "last_name": "Assistant 2",
#         "role": UserRole.VIRTUAL_ASSISTANT,
#         "is_active": True,
#         "is_active_2fa": False,
#     },
#     {
#         "email": "v3@gmail.com",
#         "password": "user",
#         "first_name": "Virtual",
#         "last_name": "Assistant 3",
#         "role": UserRole.VIRTUAL_ASSISTANT,
#         "is_active": True,
#         "is_active_2fa": True,
#     },
# ]


# async def create_test_users():
#     created_count = 0
#     updated_count = 0
#     for data in USERS_DATA:
#         email = data["email"]
#         try:
#             async with in_transaction() as conn:
#                 defaults = {
#                     "email": email,
#                     "first_name": data.get("first_name"),
#                     "last_name": data.get("last_name"),
#                     "role": data.get("role", UserRole.MERCHANT),
#                     "is_active": data.get("is_active", True),
#                     "is_superuser": data.get("is_superuser", False),
#                     "is_active_2fa": data.get("is_active_2fa", False),
#                     "password": User.set_password(data["password"]),
#                 }

#                 user, created = await User.get_or_create(
#                     email=email,
#                     defaults=defaults,
#                     using_db=conn,
#                 )

#                 if created:
#                     created_count += 1
#                     print(f"[dummy-user] created: {email}")
#                     continue

#                 updated = False
#                 for field in ["first_name", "last_name", "role", "is_active", "is_superuser", "is_active_2fa"]:
#                     expected = defaults[field]
#                     if getattr(user, field) != expected:
#                         setattr(user, field, expected)
#                         updated = True

#                 # Keep seeded credentials deterministic so login always works.
#                 password_valid = False
#                 if user.password:
#                     try:
#                         password_valid = user.verify_password(data["password"])
#                     except Exception:
#                         password_valid = False

#                 if not password_valid:
#                     user.password = defaults["password"]
#                     updated = True

#                 if updated:
#                     await user.save(using_db=conn)
#                     updated_count += 1
#                     print(f"[dummy-user] updated: {email}")
#                 else:
#                     print(f"[dummy-user] exists: {email}")
#         except IntegrityError as error:
#             print(f"[dummy-user] integrity error for {email}: {error}")
#         except Exception as error:
#             print(f"[dummy-user] unexpected error for {email}: {error}")

#     print(f"[dummy-user] seeding completed (created={created_count}, updated={updated_count})")
