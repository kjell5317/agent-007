from typing import TYPE_CHECKING

from pydantic import BaseModel, field_validator

if TYPE_CHECKING:
    from app.db.models.label import Label

# owner/repo, as GitHub allows: alphanumerics plus . _ - on both sides.
_REPO_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class LabelUpdate(BaseModel):
    description: str = ""
    github_repo: str | None = None

    @field_validator("description")
    @classmethod
    def _trim(cls, value: str) -> str:
        return value.strip()

    @field_validator("github_repo")
    @classmethod
    def _owner_repo(cls, value: str | None) -> str | None:
        repo = (value or "").strip().removeprefix("https://github.com/").removesuffix(".git")
        if not repo:
            return None
        owner, _, name = repo.partition("/")
        if not owner or not name or set(owner + name) - _REPO_CHARS:
            raise ValueError("GitHub repo must look like owner/repo")
        return repo


class LabelRead(BaseModel):
    google_id: str
    name: str
    color: str
    description: str
    github_repo: str | None

    @classmethod
    def from_row(cls, row: "Label") -> "LabelRead":
        return cls(
            google_id=row.google_id,
            name=row.name,
            color=row.background_color,
            description=row.description,
            github_repo=row.github_repo,
        )
