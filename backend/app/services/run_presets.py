"""Named stack presets that expand into a set of registry entries.

These are plain module constants rather than a table: nothing about them is per-workspace or
user-editable, and a preset is only ever read once — at the moment the user applies it, after
which the rows it created are ordinary registry entries they can edit freely.

Deliberately *not* implemented here: shipping a run configuration on a `workspace_templates` row
(Epic 10's "Future Enhancements"). Detection covers real repositories; presets exist for the case
where a repository has nothing to detect yet.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PresetCommand:
    name: str
    command: str
    role: str
    cwd: str | None = None
    port: int | None = None


@dataclass(frozen=True)
class RunPreset:
    slug: str
    name: str
    description: str
    commands: tuple[PresetCommand, ...]

    def as_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "commands": [asdict(command) for command in self.commands],
        }


RUN_PRESETS: tuple[RunPreset, ...] = (
    RunPreset(
        slug="node-fullstack",
        name="Node full-stack",
        description="A frontend and an API in one repository, each with its own directory.",
        commands=(
            PresetCommand("install:web", "pnpm install", "install", "frontend"),
            PresetCommand("install:api", "pnpm install", "install", "backend"),
            PresetCommand("dev:web", "pnpm dev", "dev", "frontend", 5173),
            PresetCommand("dev:api", "pnpm dev", "dev", "backend", 3000),
            PresetCommand("test", "pnpm test", "test", None),
        ),
    ),
    RunPreset(
        slug="vite+fastapi",
        name="Vite + FastAPI",
        description="A Vite frontend on 5173 and a FastAPI backend on 8000.",
        commands=(
            PresetCommand("install:web", "pnpm install", "install", "frontend"),
            PresetCommand("install:api", "uv sync", "install", "backend"),
            PresetCommand("migrate", "alembic upgrade head", "migrate", "backend"),
            PresetCommand("dev:web", "pnpm dev --host 0.0.0.0", "dev", "frontend", 5173),
            PresetCommand(
                "dev:api",
                "uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000",
                "dev",
                "backend",
                8000,
            ),
        ),
    ),
    RunPreset(
        slug="django",
        name="Django",
        description="A Django project with migrations and the development server.",
        commands=(
            PresetCommand("install", "pip install -r requirements.txt", "install"),
            PresetCommand("migrate", "python manage.py migrate", "migrate"),
            PresetCommand("seed", "python manage.py loaddata seed", "seed"),
            PresetCommand("dev", "python manage.py runserver 0.0.0.0:8000", "dev", None, 8000),
            PresetCommand("test", "python manage.py test", "test"),
        ),
    ),
    RunPreset(
        slug="nextjs",
        name="Next.js",
        description="A Next.js application served on 3000.",
        commands=(
            PresetCommand("install", "pnpm install", "install"),
            PresetCommand("dev", "pnpm dev", "dev", None, 3000),
            PresetCommand("lint", "pnpm lint", "lint"),
            PresetCommand("test", "pnpm test", "test"),
        ),
    ),
    RunPreset(
        slug="spring-boot",
        name="Spring Boot",
        description="A Maven-wrapped Spring Boot service on 8080.",
        commands=(
            PresetCommand("install", "./mvnw -q dependency:go-offline", "install"),
            PresetCommand("dev", "./mvnw spring-boot:run", "dev", None, 8080),
            PresetCommand("test", "./mvnw test", "test"),
        ),
    ),
    RunPreset(
        slug="laravel",
        name="Laravel",
        description="A Laravel application with Composer dependencies and artisan.",
        commands=(
            PresetCommand("install", "composer install", "install"),
            PresetCommand("migrate", "php artisan migrate", "migrate"),
            PresetCommand("seed", "php artisan db:seed", "seed"),
            PresetCommand(
                "dev", "php artisan serve --host 0.0.0.0 --port 8000", "dev", None, 8000
            ),
            PresetCommand("test", "php artisan test", "test"),
        ),
    ),
)

PRESETS_BY_SLUG = {preset.slug: preset for preset in RUN_PRESETS}


class UnknownPresetError(Exception):
    """The requested preset slug is not one this build ships."""


def get_preset(slug: str) -> RunPreset:
    preset = PRESETS_BY_SLUG.get(slug)
    if preset is None:
        raise UnknownPresetError(slug)
    return preset
