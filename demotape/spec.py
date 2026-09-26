"""Pipeline specification models, validated with pydantic.

The whole point of this module is to fail fast with precise, per-field errors
before any browser is launched. `DemoSpec.model_json_schema()` powers
`demotape --schema` so agents can generate correct pipelines without guessing.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# Pacing constants shared by the runner (which enforces them on the video
# clock), the --estimate arithmetic, and the Pipeline builder's docs.
#: words a second of silent reading, measured against full takes
READING_WORDS_PER_SECOND = 3.5
#: no caption stays up for less than this, however short it is
READING_FLOOR_S = 2.3
#: nor longer than this, however long it is
READING_CEILING_S = 11.0
#: words a second the TTS voice reads, measured on af_heart over a full take
TTS_WORDS_PER_SECOND = 3.55


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


class UploadStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["upload"]
    selector: str = Field(description='CSS selector of the <input type="file"> element; it may be hidden.')
    path: str = Field(
        description="File to give to the input. A relative path resolves against the working directory of the recorder."
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


class SayStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["say"]
    title: str = Field(
        default="",
        description="Overlay title text. Empty string hides the title.",
    )
    description: str = Field(
        default="",
        description="Overlay description text, and the narration voice's spoken text "
        "(the title is spoken when this is empty). Empty string hides the description.",
    )
    position: Literal["top", "bottom"] = Field(
        default="top",
        description="Vertical placement of the narration bar. Both bars share it, so "
        "they render as one card (title on the first line, description under it).",
    )
    align: Literal["left", "center", "right"] = Field(
        default="center", description="Horizontal alignment of the narration bar."
    )


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
        UploadStep,
        ScrollStep,
        TitleStep,
        DescriptionStep,
        SayStep,
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
    text_to_speech: bool = Field(
        default=False,
        description="Narrate the overlay text with a local TTS model (Kokoro, downloaded "
        "automatically on first use). The description is spoken, or the title when there is "
        "no description; the voice starts when the text appears, actions keep running while "
        "it plays, and the next text change waits for the voice to finish. Requires the tts "
        "extra: pip install 'demo-tape[tts]'.",
    )
    tts_voice: str = Field(
        default="af_heart",
        description="Kokoro voice. The first letter selects the language "
        "(a=en-us, b=en-gb, e=es, f=fr-fr, h=hi, i=it, j=ja, p=pt-br, z=zh), e.g. "
        "af_heart, bf_emma, im_nicola (Italian), jm_kumo. Only meaningful with "
        "text_to_speech.",
    )
    reading_pace: float | None = Field(
        default=None,
        gt=0,
        description="Silent-reading pace in words per second (measured: 3.5). When set, "
        "every caption stays up for at least words / reading_pace (clamped to "
        f"{READING_FLOOR_S}-{READING_CEILING_S}s) before the next text change — measured on the "
        "video clock, so app work that ran under the caption counts toward it. With "
        "text_to_speech the hold is max(voice end, reading time). Unset: no reading hold.",
    )
    steps: list[Step] = Field(min_length=1)


def _shorten(text: str, limit: int = 60) -> str:
    text = text.replace("\n", " ")
    return f'"{text[:limit]}{"…" if len(text) > limit else ""}"'


def narration_event_texts(steps: list[Step]) -> list[str]:
    """The text of each narration event, in order.

    One event per visual text change, mirroring the runner's pairing: a `title`
    immediately followed by a `description` is ONE event spoken as the
    description; `say` is one event; a lone `title` (or `description`) is its
    own event spoken as its text. Used by the duration estimate.
    """
    events: list[str] = []
    i = 0
    while i < len(steps):
        step = steps[i]
        if step.action == "say":
            events.append(step.description or step.title)
            i += 1
        elif step.action == "title":
            nxt = steps[i + 1] if i + 1 < len(steps) else None
            if nxt is not None and nxt.action == "description":
                events.append(nxt.text or step.text)
                i += 2
            else:
                events.append(step.text)
                i += 1
        elif step.action == "description":
            events.append(step.text)
            i += 1
        else:
            i += 1
    return [text for text in events if text.strip()]


def describe_step(step: Step) -> str:
    """One-line detail of what a step targets, for the runner's progress lines.

    Lives here so the per-action fields are described next to their models
    instead of being mirrored in the runner.
    """
    kind = step.action  # type: ignore[union-attr]
    if kind == "navigate":
        return step.url  # type: ignore[union-attr]
    if kind in ("click", "highlight", "scroll"):
        return step.selector  # type: ignore[union-attr]
    if kind == "type":
        return f'{step.selector} <- "{step.text}"'  # type: ignore[union-attr]
    if kind == "drag":
        target = step.to_selector or f"{step.axis}-axis to {step.to:.0%}"  # type: ignore[union-attr]
        return f"{step.selector} -> {target}"  # type: ignore[union-attr]
    if kind == "select":
        return f"{step.selector} option={step.option}"  # type: ignore[union-attr]
    if kind == "upload":
        return f"{step.selector} <- {step.path}"  # type: ignore[union-attr]
    if kind == "title" or kind == "description":
        return _shorten(step.text)  # type: ignore[union-attr]
    if kind == "say":
        text = step.description or step.title  # type: ignore[union-attr]
        return _shorten(text)
    if kind == "assert_text":
        return _shorten(step.text)  # type: ignore[union-attr]
    if kind == "assert_value":
        return f"{step.selector} == {step.value}"  # type: ignore[union-attr]
    if kind == "wait":
        return f"{step.seconds:.1f}s"  # type: ignore[union-attr]
    return ""
