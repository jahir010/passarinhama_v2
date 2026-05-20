# from tortoise.signals import post_save
# from applications.user.models import User, CustomerProfile, VendorProfile, RiderProfile


# async def create_user_profile(sender, instance: User, created: bool, **kwargs):
#     if created:
#         # Create or get a CustomerProfile safely
#         profile, is_created = await CustomerProfile.get_or_create(user=instance)
#         if is_created:
#             print(f"✅ CustomerProfile created for user {instance.phone}")
#         else:
#             print(f"ℹ️ CustomerProfile already exists for user {instance.phone}")

#         # (Optional) — create other profiles conditionally
#         if instance.is_vendor:
#             vendor_profile, v_created = await VendorProfile.get_or_create(user=instance)
#             if v_created:
#                 print(f"✅ VendorProfile created for user {instance.phone}")

#         if instance.is_rider:
#             rider_profile, r_created = await RiderProfile.get_or_create(user=instance)
#             if r_created:
#                 print(f"✅ RiderProfile created for user {instance.phone}")


# post_save(User)(create_user_profile)
