from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


Weekday = Annotated[int, Field(strict=True, ge=0, le=6)]
DaysOfWeek = Annotated[list[Weekday], Field(min_length=1, max_length=7)]
ScheduleTime = Annotated[str, Field(pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')]
Target = Annotated[int, Field(strict=True, ge=0, le=7)]
SourceId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
Action = Literal['start', 'stop', 'switch_source']


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    enabled: StrictBool = True
    days_of_week: DaysOfWeek
    time: ScheduleTime
    tx_id: Target
    action: Action
    source_id: SourceId | None = None

    @model_validator(mode='after')
    def validate_rule(self):
        if len(self.days_of_week) != len(set(self.days_of_week)):
            raise ValueError('days_of_week must contain distinct days')
        if self.action == 'switch_source' and self.source_id is None:
            raise ValueError('source_id is required for switch_source')
        if self.action == 'stop' and self.source_id is not None:
            raise ValueError('source_id is forbidden for stop')
        return self


class SchedulePatch(BaseModel):
    model_config = ConfigDict(extra='forbid')

    enabled: StrictBool | None = None
    days_of_week: DaysOfWeek | None = None
    time: ScheduleTime | None = None
    tx_id: Target | None = None
    action: Action | None = None
    source_id: SourceId | None = None


class ScheduleRule(ScheduleConfig):
    id: int
