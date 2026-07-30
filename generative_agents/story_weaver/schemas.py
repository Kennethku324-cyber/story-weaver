"""story_weaver.schemas — API 請求/回應模型（pydantic v2）.

pydantic 只負責形狀/型別/長度；業務規則（撞名、住所衝突、
路徑安全）喺 builder.StoryBuilder.validate() 做。
"""

import re

from pydantic import BaseModel, Field, field_validator

# 路徑危險字符：/ \ 同控制字符；「..」同「|」（關係矩陣 key 分隔符）都拒絕
_UNSAFE_NAME = re.compile(r'[/\\|]|\.\.|[\x00-\x1f]')


def _clean_name(v: str) -> str:
    """前後空格 trim；API 直 call 繞過前端時都唔會產生尾空格名。"""
    return v.strip()


class CharacterIn(BaseModel):
    template_id: str
    display_name: str = Field(min_length=1, max_length=20)
    occupation: str = Field(min_length=1, max_length=200)
    personality: str = Field(min_length=1, max_length=200)
    home: list[str] = Field(min_length=3, max_length=3)  # [world, sector, arena]

    @field_validator("display_name")
    @classmethod
    def _name_safe(cls, v: str) -> str:
        v = _clean_name(v)
        if _UNSAFE_NAME.search(v):
            raise ValueError("角色名唔可以含有 /、\\、| 或「..」呢啲字符")
        if not v:
            raise ValueError("角色名唔可以全係空格")
        return v


class RelationshipIn(BaseModel):
    from_name: str = Field(alias="from")
    to: str
    score: int = 0  # 超界會 clamp，唔係 reject
    desc: str = Field(default="", max_length=200)

    model_config = {"populate_by_name": True}


class SetupCreateRequest(BaseModel):
    story_name: str = Field(min_length=1, max_length=50)
    story_opening: str = Field(min_length=10, max_length=1000)
    characters: list[CharacterIn] = Field(min_length=4, max_length=25)
    relationships: list[RelationshipIn] = Field(default_factory=list)

    @field_validator("story_name")
    @classmethod
    def _story_name_safe(cls, v: str) -> str:
        v = _clean_name(v)
        if _UNSAFE_NAME.search(v):
            raise ValueError("故事名唔可以含有 /、\\、| 或「..」呢啲字符")
        if not v:
            raise ValueError("故事名唔可以全係空格")
        return v


class FieldError(BaseModel):
    field: str  # 例如 "characters[2].display_name" / "story_name"
    message: str  # 繁體中文錯誤訊息


class ClampNotice(BaseModel):
    field: str
    original: int | str
    clamped: int | str


class SetupCreateResponse(BaseModel):
    story_name: str
    story_dir: str
    sim_config_path: str
    characters: list[str]
    template_map: dict[str, str]
    clamped: list[ClampNotice] = []
    filled_relationships: int = 0
    llm_fallback: bool = True
    redirect: str
