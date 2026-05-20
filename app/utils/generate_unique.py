import uuid
from slugify import slugify
from typing import Optional

def generate_random_suffix(length: int = 6) -> str:
    return uuid.uuid4().hex[:length]

async def generate_unique(
    model,
    field: str = "slug",
    text: Optional[str] = None,
    max_length: int = 100,
    suffix_length: int = 6,
    max_attempts: int = 10
) -> str:
    if not text:
        text = uuid.uuid4().hex 
    base_slug = slugify(text)[: max_length - suffix_length]

    for attempt in range(max_attempts):
        slug = f"{base_slug}{generate_random_suffix(suffix_length)}"
        if not await model.filter(**{field: slug}).exists():
            return slug

    raise ValueError(
        f"Unable to generate a unique slug after {max_attempts} attempts."
    )
