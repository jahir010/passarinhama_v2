from fastapi import HTTPException, status
import re

async def phone_number(value: str) -> str:
    if not value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Phone number is required."
        )
    value = value.strip().replace(" ", "").replace("-", "")
    if value.startswith("00"):
        value = "+" + value[2:]
    if not value.startswith("+") and value.isdigit():
        value = "+" + value
    phone_regex = r'^\+[1-9]\d{7,14}$'
    if not re.match(phone_regex, value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Enter a valid international phone number (E.164 format)."
        )
    return value
