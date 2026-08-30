"""Pipeline specification models, validated with pydantic.

The whole point of this module is to fail fast with precise, per-field errors
before any browser is launched. `DemoSpec.model_json_schema()` powers
`demo-record --schema` so agents can generate correct pipelines without guessing.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class Viewport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=1536, ge=1)
    height: int = Field(default=864, ge=1)


class NavigateStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["navigate"]
    url: str = Field(description="Absolute URL to navigate to.")
    wait_seconds: float = Field(
        default=1.0, ge=0, description="Extra settle time after load, in seconds."
    )


class WaitStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["wait"]
    seconds: float = Field(ge=0, description="How long to pause, in seconds.")


class ClickStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["click"]
    selector: str = Field(description="CSS selector of the element to click.")


class TypeStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["type"]
    selector: str = Field(description="CSS selector of the input/textarea/contenteditable.")
    text: str = Field(description="The final text to type.")
    type_delay_ms: int = Field(
        default=70, ge=0, description="Delay between individual keystrokes, in ms."
    )
    clear: bool = Field(
        default=True, description="Clear the field's existing value before typing."
    )


class SelectStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["select"]
    selector: str = Field(description="CSS selector of the native <select> element.")
    option: str = Field(
        description="Option value, or visible label if no value matches, to select."
    )


class ScrollStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["scroll"]
    selector: str = Field(description="CSS selector of the element to bring to the center of the viewport.")


class TitleStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["title"]
    text: str = Field(description="Overlay title text. Empty string hides the title.")
    position: Literal["top", "bottom"] = Field(default="top", description="Vertical placement of the title bar.")
    align: Literal["left", "center", "right"] = Field(default="center", description="Horizontal alignment of the title bar.")


class DescriptionStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["description"]
    text: str = Field(description="Overlay description text. Empty string hides the description.")
    position: Literal["top", "bottom"] = Field(default="bottom", description="Vertical placement of the description bar.")
    align: Literal["left", "center", "right"] = Field(default="center", description="Horizontal alignment of the description bar.")


class AssertTextStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["assert_text"]
    text: str = Field(description="Text that must be present in the page body.")
    case_sensitive: bool = Field(
        default=False,
        description="By default the match is case-insensitive, because CSS text-transform "
        "(e.g. uppercase labels) changes what innerText returns vs the source.",
    )


class AssertValueStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["assert_value"]
    selector: str = Field(description="CSS selector of the input, textarea or select to check.")
    value: str = Field(description="Exact value the element must hold (what a controlled input actually kept).")


class DoneStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["done"]


Step = Annotated[
    Union[
        NavigateStep,
        WaitStep,
        ClickStep,
        TypeStep,
        SelectStep,
        ScrollStep,
        TitleStep,
        DescriptionStep,
        AssertTextStep,
        AssertValueStep,
        DoneStep,
    ],
    Field(discriminator="action"),
]


class DemoSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    viewport: Viewport = Field(default_factory=Viewport)
    headless: bool = Field(
        default=False, description="Run without a visible window. Video is recorded either way."
    )
    output_dir: str = Field(
        default="./demo_out", description="Directory for video.mp4 and error.png."
    )
    delay_ms: int = Field(
        default=500,
        ge=0,
        description="Pause applied after EVERY step, in ms. Use explicit wait steps for longer pauses.",
    )
    steps: list[Step] = Field(min_length=1)
