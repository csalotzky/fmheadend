from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


Recurrence = Literal['daily', 'weekdays', 'weekends', 'selected_weekdays']
Weekday = Annotated[int, Field(strict=True, ge=0, le=6)]
ScheduleTime = Annotated[str, Field(pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')]
Target = Annotated[int, Field(strict=True, ge=0, le=7)]
SourceId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
Action = Literal['start', 'stop', 'switch_source']


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    enabled: StrictBool = True
    recurrence: Recurrence
    weekdays: list[Weekday] = Field(default_factory=list, max_length=7)
    time: ScheduleTime
    tx_id: Target
    action: Action
    source_id: SourceId | None = None

    @model_validator(mode='after')
    def validate_rule(self):
        if self.recurrence == 'selected_weekdays':
            if not self.weekdays or len(self.weekdays) != len(set(self.weekdays)):
                raise ValueError('selected_weekdays requires distinct weekdays (Monday=0, Sunday=6)')
        elif self.weekdays:
            raise ValueError('weekdays may only be supplied with selected_weekdays')
        if self.action == 'switch_source' and self.source_id is None:
            raise ValueError('source_id is required for switch_source')
        if self.action == 'stop' and self.source_id is not None:
            raise ValueError('source_id is forbidden for stop')
        return self


class SchedulePatch(BaseModel):
    model_config = ConfigDict(extra='forbid')

    enabled: StrictBool | None = None
    recurrence: Recurrence | None = None
    weekdays: list[Weekday] | None = Field(default=None, max_length=7)
    time: ScheduleTime | None = None
    tx_id: Target | None = None
    action: Action | None = None
    source_id: SourceId | None = None


class ScheduleRule(ScheduleConfig):
    id: int
