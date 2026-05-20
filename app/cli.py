import asyncio
from getpass import getpass
from typing import Optional

import typer
from tortoise import Tortoise

from app.config import init_db
from applications.user.models import User, UserRole, UserStatus

cli = typer.Typer(
    help="Project management commands.",
    no_args_is_help=True,
    add_completion=False,
)


@cli.callback()
def main() -> None:
    """Management command entrypoint."""


async def _close_db() -> None:
    await Tortoise.close_connections()


async def _create_superuser(
    email: str,
    first_name: str,
    last_name: str,
    password: str,
    phone: Optional[str],
    mobile: Optional[str],
) -> None:
    await init_db()
    try:
        normalized_email = email.strip().lower()
        existing_user = await User.get_or_none(email=normalized_email)
        if existing_user:
            typer.echo(f"User with email '{normalized_email}' already exists.")
            raise typer.Exit(code=1)

        user = await User.create(
            email=normalized_email,
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            password=User.set_password(password),
            phone=phone.strip() if phone else None,
            mobile=mobile.strip() if mobile else None,
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            is_active=True,
            is_superuser=True,
            is_email_verified=True,
        )

        typer.echo("Superuser created successfully.")
        typer.echo(f"id: {user.id}")
        typer.echo(f"email: {user.email}")
        typer.echo(f"role: {user.role}")
        typer.echo(f"status: {user.status}")
    finally:
        await _close_db()


@cli.command("create-superuser")
@cli.command("createsuperuser")
def create_superuser(
    email: Optional[str] = typer.Option(None, prompt=True, help="Email address"),
    first_name: Optional[str] = typer.Option(None, prompt=True, help="First name"),
    last_name: Optional[str] = typer.Option(None, prompt=True, help="Last name"),
    phone: Optional[str] = typer.Option(None, prompt=False, help="Phone number"),
    mobile: Optional[str] = typer.Option(None, prompt=False, help="Mobile number"),
) -> None:
    password = getpass("Password: ")
    confirm_password = getpass("Password (again): ")

    if not email or not email.strip():
        raise typer.BadParameter("Email is required.")
    if not first_name or not first_name.strip():
        raise typer.BadParameter("First name is required.")
    if not last_name or not last_name.strip():
        raise typer.BadParameter("Last name is required.")
    if not password.strip():
        raise typer.BadParameter("Password is required.")
    if password != confirm_password:
        raise typer.BadParameter("Passwords do not match.")

    asyncio.run(
        _create_superuser(
            email=email,
            first_name=first_name,
            last_name=last_name,
            password=password,
            phone=phone,
            mobile=mobile,
        )
    )


if __name__ == "__main__":
    cli()
