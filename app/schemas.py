"""请求体模型与入参校验。校验失败由 FastAPI 返回 422，不产生任何落库状态。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PublishCommand(BaseModel):
    """发布命令：值守员提交一条新告警。"""

    model_config = ConfigDict(extra="forbid")

    request_no: str = Field(min_length=1, max_length=128, description="请求号，幂等键")
    alert_no: int = Field(gt=0, description="告警号，正整数，全局不可复用")
    areas: list[str] = Field(min_length=1, description="覆盖区域列表，非空且互不重复")
    level: int = Field(ge=1, le=5, description="告警级别 1~5，5 最高")
    message: str = Field(min_length=1, max_length=2000, description="播发文案，非空")
    delay_seconds: int = Field(ge=0, description="延迟秒数，相对受理时间")
    duration_seconds: int = Field(gt=0, description="持续秒数，正整数")

    @field_validator("request_no")
    @classmethod
    def request_no_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("request_no 不能为空或全为空白字符")
        return v

    @field_validator("areas")
    @classmethod
    def areas_non_blank_and_unique(cls, v: list[str]) -> list[str]:
        if any(not a or not a.strip() for a in v):
            raise ValueError("区域名称不能为空或全为空白字符")
        if len(set(v)) != len(v):
            raise ValueError("区域列表存在重复项")
        return v

    @field_validator("message")
    @classmethod
    def message_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("文案不能为空或全为空白字符")
        return v


class RevokeCommand(BaseModel):
    """撤销命令：按受理时间终止既有告警的后续播发。"""

    model_config = ConfigDict(extra="forbid")

    request_no: str = Field(min_length=1, max_length=128, description="请求号，幂等键")
    alert_no: int = Field(gt=0, description="待撤销的告警号")

    @field_validator("request_no")
    @classmethod
    def request_no_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("request_no 不能为空或全为空白字符")
        return v


class AmendCommand(BaseModel):
    """修订命令：对进行中告警整体替换文案、级别与覆盖区域。

    校验沿用发布规则：文案非空、级别 1~5、区域列表非空/无空白/不重复。
    不含延迟与持续时长——修订不改变原生效终点。
    """

    model_config = ConfigDict(extra="forbid")

    request_no: str = Field(min_length=1, max_length=128, description="请求号，幂等键")
    alert_no: int = Field(gt=0, description="待修订的告警号")
    areas: list[str] = Field(min_length=1, description="完整的新覆盖区域列表，非空且互不重复")
    level: int = Field(ge=1, le=5, description="新告警级别 1~5，5 最高")
    message: str = Field(min_length=1, max_length=2000, description="完整的新播发文案，非空")

    @field_validator("request_no")
    @classmethod
    def request_no_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("request_no 不能为空或全为空白字符")
        return v

    @field_validator("areas")
    @classmethod
    def areas_non_blank_and_unique(cls, v: list[str]) -> list[str]:
        if any(not a or not a.strip() for a in v):
            raise ValueError("区域名称不能为空或全为空白字符")
        if len(set(v)) != len(v):
            raise ValueError("区域列表存在重复项")
        return v

    @field_validator("message")
    @classmethod
    def message_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("文案不能为空或全为空白字符")
        return v
