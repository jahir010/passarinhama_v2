# import asyncio
# from typing import Dict, List, Optional, Union

# from fastapi import HTTPException, UploadFile, status
# from fastapi_mail import ConnectionConfig, FastMail, MessageSchema
# from pydantic import EmailStr

# from app.config import settings

# RecipientInput = Union[EmailStr, str, List[Union[EmailStr, str]]]
# AttachmentInput = Optional[List[Union[UploadFile, Dict, str]]]


# def _get_setting(name: str, default=None):
#     return getattr(settings, name, default)


# def _build_mail_config() -> ConnectionConfig:
#     mail_username = _get_setting("EMAIL_HOST_USER")
#     mail_password = _get_setting("EMAIL_HOST_PASSWORD")
#     mail_from = _get_setting("DEFAULT_FROM_EMAIL") or mail_username
#     mail_port = _get_setting("EMAIL_PORT", 587)
#     mail_server = _get_setting("EMAIL_HOST")

#     missing_fields = [
#         field_name
#         for field_name, value in {
#             "EMAIL_HOST_USER": mail_username,
#             "EMAIL_HOST_PASSWORD": mail_password,
#             "DEFAULT_FROM_EMAIL": mail_from,
#             "EMAIL_PORT": mail_port,
#             "EMAIL_HOST": mail_server,
#         }.items()
#         if value in (None, "")
#     ]
#     if missing_fields:
#         raise HTTPException(
#             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#             detail=f"Email configuration is incomplete: {', '.join(missing_fields)}",
#         )

#     return ConnectionConfig(
#         MAIL_USERNAME=mail_username,
#         MAIL_PASSWORD=mail_password,
#         MAIL_FROM=mail_from,
#         MAIL_PORT=int(mail_port),
#         MAIL_SERVER=mail_server,
#         MAIL_STARTTLS=bool(_get_setting("EMAIL_STARTTLS", True)),
#         MAIL_SSL_TLS=bool(_get_setting("EMAIL_SSL_TLS", False)),
#         MAIL_FROM_NAME=_get_setting("DEFAULT_FROM_NAME"),
#         USE_CREDENTIALS=bool(_get_setting("EMAIL_USE_CREDENTIALS", True)),
#         VALIDATE_CERTS=bool(_get_setting("EMAIL_VALIDATE_CERTS", True)),
#         SUPPRESS_SEND=int(bool(_get_setting("EMAIL_SUPPRESS_SEND", False))),
#     )


# def _normalize_recipients(value: Optional[RecipientInput], *, field_name: str) -> List[str]:
#     if value is None:
#         return []

#     raw_values = value if isinstance(value, list) else [value]
#     recipients = [str(item).strip() for item in raw_values if str(item).strip()]
#     if not recipients:
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail=f"{field_name} must contain at least one email address.",
#         )
#     return recipients


# def _normalize_optional_recipients(value: Optional[List[Union[EmailStr, str]]]) -> List[str]:
#     if not value:
#         return []
#     return [str(item).strip() for item in value if str(item).strip()]


# async def send_email(
#     *,
#     subject: str,
#     to: Optional[RecipientInput] = None,
#     to_email: Optional[RecipientInput] = None,
#     message: Optional[str] = None,
#     html_message: Optional[str] = None,
#     from_email: Optional[EmailStr] = None,
#     from_name: Optional[str] = None,
#     cc: Optional[List[EmailStr]] = None,
#     bcc: Optional[List[EmailStr]] = None,
#     reply_to: Optional[List[EmailStr]] = None,
#     attachments: AttachmentInput = None,
#     headers: Optional[Dict] = None,
#     retries: int = 2,
#     delay: int = 2,
# ) -> bool:
#     if to is not None and to_email is not None:
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail="Use either 'to' or 'to_email', not both.",
#         )

#     recipients = _normalize_recipients(to if to is not None else to_email, field_name="Recipient list")
#     subject = subject.strip()
#     if not subject:
#         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email subject is required.")

#     body = html_message if html_message else message
#     if not body or not body.strip():
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail="Either message or html_message must be provided.",
#         )
#     body = body.strip()
#     subtype = "html" if html_message else "plain"

#     config = _build_mail_config()
#     configured_sender = str(config.MAIL_FROM)
#     requested_from_email = str(from_email).strip() if from_email else None

#     if requested_from_email and requested_from_email != configured_sender:
#         sender_email = configured_sender
#         reply_to_list = _normalize_optional_recipients(reply_to) or [requested_from_email]
#     else:
#         sender_email = requested_from_email or configured_sender
#         reply_to_list = _normalize_optional_recipients(reply_to)

#     msg = MessageSchema(
#         subject=subject,
#         recipients=recipients,
#         body=body,
#         subtype=subtype,
#         from_email=sender_email,
#         from_name=from_name,
#         cc=_normalize_optional_recipients(cc),
#         bcc=_normalize_optional_recipients(bcc),
#         reply_to=reply_to_list,
#         attachments=attachments or [],
#         headers=headers,
#     )

#     fast_mail = FastMail(config)
#     retry_count = max(int(retries), 0)
#     retry_delay = max(int(delay), 0)
#     total_attempts = retry_count + 1
#     last_error: Exception | None = None

#     for attempt in range(1, total_attempts + 1):
#         try:
#             await fast_mail.send_message(msg)
#             print(
#                 f"Email sent from {sender_email} to {recipients} (reply-to: {reply_to_list})",
#                 flush=True,
#             )
#             return True
#         except HTTPException:
#             raise
#         except Exception as exc:
#             last_error = exc
#             print(f"Email failed (attempt {attempt}/{total_attempts}): {exc}", flush=True)
#             if attempt < total_attempts:
#                 await asyncio.sleep(retry_delay)

#     raise HTTPException(
#         status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#         detail="Failed to send email after retries.",
#     ) from last_error





















import asyncio
from typing import Dict, List, Optional, Union

from fastapi import HTTPException, UploadFile, status
from fastapi_mail import ConnectionConfig, FastMail, MessageSchema
from pydantic import EmailStr

from app.config import settings

RecipientInput = Union[EmailStr, str, List[Union[EmailStr, str]]]
AttachmentInput = Optional[List[Union[UploadFile, Dict, str]]]


def _get_setting(name: str, default=None):
    return getattr(settings, name, default)


def _parse_bool(value, default: bool) -> bool:
    """
    Safely coerce env-var values to bool.
    Handles strings like "false", "0", "no" correctly.
    bool("false") naively returns True — this fixes that.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "0", "no", "off", "")


def _build_mail_config() -> ConnectionConfig:
    mail_username = _get_setting("EMAIL_HOST_USER")
    mail_password = _get_setting("EMAIL_HOST_PASSWORD")
    mail_from     = _get_setting("DEFAULT_FROM_EMAIL") or mail_username
    mail_port     = _get_setting("EMAIL_PORT", 587)
    mail_server   = _get_setting("EMAIL_HOST")

    missing_fields = [
        name
        for name, value in {
            "EMAIL_HOST_USER":     mail_username,
            "EMAIL_HOST_PASSWORD": mail_password,
            "DEFAULT_FROM_EMAIL":  mail_from,
            "EMAIL_PORT":          mail_port,
            "EMAIL_HOST":          mail_server,
        }.items()
        if value in (None, "")
    ]
    if missing_fields:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Email configuration is incomplete: {', '.join(missing_fields)}",
        )

    return ConnectionConfig(
        MAIL_USERNAME=mail_username,
        MAIL_PASSWORD=mail_password,
        MAIL_FROM=mail_from,
        MAIL_PORT=int(mail_port),
        MAIL_SERVER=mail_server,
        # FIX: use _parse_bool instead of bool() to correctly handle
        # string env-var values like "false" or "0".
        MAIL_STARTTLS=_parse_bool(_get_setting("EMAIL_STARTTLS"), default=True),
        MAIL_SSL_TLS=_parse_bool(_get_setting("EMAIL_SSL_TLS"), default=False),
        MAIL_FROM_NAME=_get_setting("DEFAULT_FROM_NAME"),
        USE_CREDENTIALS=_parse_bool(_get_setting("EMAIL_USE_CREDENTIALS"), default=True),
        VALIDATE_CERTS=_parse_bool(_get_setting("EMAIL_VALIDATE_CERTS"), default=True),
        SUPPRESS_SEND=int(_parse_bool(_get_setting("EMAIL_SUPPRESS_SEND"), default=False)),
    )


def _normalize_recipients(value: Optional[RecipientInput], *, field_name: str) -> List[str]:
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    recipients = [str(item).strip() for item in raw if str(item).strip()]
    if not recipients:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must contain at least one email address.",
        )
    return recipients


def _normalize_optional_recipients(value: Optional[List[Union[EmailStr, str]]]) -> List[str]:
    if not value:
        return []
    return [str(item).strip() for item in value if str(item).strip()]


async def send_email(
    *,
    subject: str,
    to: Optional[RecipientInput] = None,
    to_email: Optional[RecipientInput] = None,
    message: Optional[str] = None,
    html_message: Optional[str] = None,
    from_email: Optional[EmailStr] = None,
    from_name: Optional[str] = None,
    cc: Optional[List[EmailStr]] = None,
    bcc: Optional[List[EmailStr]] = None,
    reply_to: Optional[List[EmailStr]] = None,
    attachments: AttachmentInput = None,
    headers: Optional[Dict] = None,
    retries: int = 2,
    delay: int = 2,
) -> bool:
    if to is not None and to_email is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use either 'to' or 'to_email', not both.",
        )

    recipients = _normalize_recipients(
        to if to is not None else to_email, field_name="Recipient list"
    )

    subject = subject.strip()
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email subject is required.",
        )

    body = html_message if html_message else message
    if not body or not body.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either message or html_message must be provided.",
        )
    body    = body.strip()
    subtype = "html" if html_message else "plain"

    config           = _build_mail_config()
    configured_sender = str(config.MAIL_FROM)
    requested_from    = str(from_email).strip() if from_email else None

    if requested_from and requested_from != configured_sender:
        sender_email   = configured_sender
        reply_to_list  = _normalize_optional_recipients(reply_to) or [requested_from]
    else:
        sender_email  = requested_from or configured_sender
        reply_to_list = _normalize_optional_recipients(reply_to)

    msg = MessageSchema(
        subject=subject,
        recipients=recipients,
        body=body,
        subtype=subtype,
        from_email=sender_email,
        from_name=from_name,
        cc=_normalize_optional_recipients(cc),
        bcc=_normalize_optional_recipients(bcc),
        reply_to=reply_to_list,
        attachments=attachments or [],
        headers=headers,
    )

    fast_mail     = FastMail(config)
    total_attempts = max(int(retries), 0) + 1
    last_error: Exception | None = None

    for attempt in range(1, total_attempts + 1):
        try:
            await fast_mail.send_message(msg)
            print(
                f"[email] sent from={sender_email} to={recipients} reply-to={reply_to_list}",
                flush=True,
            )
            return True
        except HTTPException:
            raise
        except Exception as exc:
            last_error = exc
            print(f"[email] attempt {attempt}/{total_attempts} failed: {exc}", flush=True)
            if attempt < total_attempts:
                await asyncio.sleep(max(int(delay), 0))

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to send email after retries.",
    ) from last_error


async def send_bulk_email(
    *,
    subject: str,
    recipients: List[str],
    html_message: Optional[str] = None,
    message: Optional[str] = None,
    chunk_size: int = 50,
    chunk_delay: float = 1.0,
    retries: int = 1,
) -> Dict[str, int]:
    """
    Send the same email to many recipients without hammering the SMTP server.

    Splits `recipients` into chunks of `chunk_size`, sends each chunk as a
    single SMTP call (BCC-style via the `recipients` list), then sleeps
    `chunk_delay` seconds between chunks so you stay within SMTP rate limits.

    Returns a dict with 'sent' and 'failed' counts.
    """
    if not recipients:
        return {"sent": 0, "failed": 0}

    sent = 0
    failed = 0
    chunks = [recipients[i : i + chunk_size] for i in range(0, len(recipients), chunk_size)]

    for idx, chunk in enumerate(chunks, start=1):
        try:
            await send_email(
                subject=subject,
                to=chunk,
                html_message=html_message,
                message=message,
                retries=retries,
            )
            sent += len(chunk)
        except Exception as exc:
            failed += len(chunk)
            print(f"[bulk-email] chunk {idx}/{len(chunks)} failed: {exc}", flush=True)

        if idx < len(chunks):
            await asyncio.sleep(chunk_delay)

    print(f"[bulk-email] done — sent={sent} failed={failed}", flush=True)
    return {"sent": sent, "failed": failed}
