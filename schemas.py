from pydantic import BaseModel, Field

class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    proxy: str | None = None

class ProfileUpdate(BaseModel):
    name: str | None = None
    proxy: str | None = None
