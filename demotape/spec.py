"""Pipeline specification models, validated with pydantic.

The whole point of this module is to fail fast with precise, per-field errors
before any browser is launched. `DemoSpec.model_json_schema()` powers
`demotape --schema` so agents can generate correct pipelines without guessing.
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


class BackStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["back"]
    wait_seconds: float = Field(
        default=1.0, ge=0, description="Extra settle time after the history navigation."
    )


class WaitStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["wait"]
    seconds: float = Field(ge=0, description="How long to pause, in seconds.")


class ClickStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["click"]
    selector: str = Field(description="CSS selector of the element to click.")


class HighlightStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["highlight"]
    selector: str = Field(description="CSS selector of the element to spotlight.")
    duration_seconds: float = Field(
        default=2.0, ge=0, description="How long the ring stays on the element, in seconds."
    )
    spotlight: bool = Field(
        default=False,
        description="Dim the rest of the page while the element is highlighted.",
    )


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


class DragStep(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    action: Literal["drag"]
    selector: str = Field(
        description="CSS selector of the element to PRESS on. For sliders this is the track "
        "(React Native Web sliders render divs with role=\"slider\", no <input type=range>). "
        "For drag-and-drop it is the card/item to pick up."
    )
    to_selector: str | None = Field(
        default=None,
        description="CSS selector of the element to RELEASE on (drop target). Omit to drag "
        "across `selector` itself (slider-style: from/to are positions along its box).",
    )
    to: float = Field(
        default=0.5,
        ge=0,
        le=1,
        description="Release position along the release target's box: 0.0 = left/top edge, "
        "0.5 = center, 1.0 = right/bottom edge.",
    )
    from_: float | None = Field(
        default=None,
        alias="from",
        ge=0,
        le=1,
        description="Press position along `selector`'s box. Default: 0.0 (edge) when dragging "
        "across `selector` itself (a slider's value snaps to the press point, so starting at "
        "an edge avoids a jump), 0.5 (center) when dropping on another element (grab the card "
        "by its middle). To move a slider that already holds a value without a snap, press on "
        "its thumb: selector = thumb element, to_selector = the track.",
    )
    duration_seconds: float = Field(
        default=0.6,
        ge=0.05,
        description="Total time the pointer takes to travel from the press point to the release "
        "point, in seconds. The pointer interpolates in a straight line between them.",
    )
    axis: Literal["x", "y"] = Field(
        default="x", description="Direction the from/to fractions run along. Default x."
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
        BackStep,
        WaitStep,
        ClickStep,
        HighlightStep,
        TypeStep,
        DragStep,
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
