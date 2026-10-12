"""Safety checks for the SVG documents an asset graphic carries (PHASE4 C1).

An SVG inside a library file -- or inside a model file, once an asset has been
placed -- is untrusted DATA, exactly like the rest of the file (ARCHITECTURE
7.6). It is never executed, never fetched from and never allowed to reach
anything outside itself. This module is the single gate: `check_svg` runs at
library validation time AND again before the canvas hands a string to QtSvg,
so a graphic that was hand-edited into a model file afterwards is caught too.

Stdlib only, leaf module (no imports from `schema`/`problems`), so both
`product/libraries/` and `product/ui/` can use it.

What is refused, and why:

* `<!DOCTYPE` / `<!ENTITY` / `<?xml-stylesheet` -- rejected TEXTUALLY, before
  any parser sees the document. That is what makes the stdlib parser safe to
  use here without `defusedxml`: with no doctype there is no entity to expand,
  so billion-laughs and external-entity reads cannot start.
* `<script>`, `<foreignObject>`, `<use>`, `<image>`, `<a>`, `<iframe>`,
  `<embed>`, `<object>`, `<audio>`, `<video>`, `<handler>` -- every element
  that can execute, embed or navigate. `<use>` is refused outright rather than
  only for external targets: a same-document `<use>` buys an icon nothing.
* any `on*` attribute -- event handlers.
* any `href`/`xlink:href` that is not a `#fragment`, and any `url(...)` that
  does not point at a `#fragment` -- no external references at all.
* `javascript:` / `data:` anywhere in an attribute value.
* the same `url(...)` and `http(s):` references in an element's TEXT (a `<style>`
  holds CSS), and `@import` anywhere: no external references at all.
* an animation (`animate`, `animateTransform`, `animateMotion`) that targets an
  `href` or whose `to`/`from`/`values`/`by` holds an address; `<set>` is refused
  outright. An animation could otherwise point an allowed element elsewhere.
* more than one `id="fill"`, or a rectangular one outside the viewBox -- not a
  safety rule but the same gate: the fill indicator (D75) reads exactly one
  region, and a graphic the renderer cannot read is refused where it is
  authored rather than silently drawn wrong.
* a byte cap per graphic (`MAX_SVG_BYTES`) and per library
  (`MAX_LIBRARY_SVG_BYTES`), so a library cannot bloat a model file or a
  renderer's cache.

Everything else (paths, shapes, gradients, groups, transforms, text) is left
alone: QtSvg's static renderer draws it and nothing more.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from functools import lru_cache

__all__ = ["MAX_SVG_BYTES", "MAX_LIBRARY_SVG_BYTES", "THEME_TOKENS", "FILL_ELEMENT_ID",
           "check_svg", "substitute_tokens"]

# The element an icon may mark as its own fill region (PHASE4 T2, D75): the
# body of a tank, the membrane of a gas holder. The renderer scales the level's
# fraction into its bounds instead of drawing the default bar.
FILL_ELEMENT_ID = "fill"
_FILL_EPS = 1e-6

# Per graphic and per library, in bytes of UTF-8. A hand-drawn state icon is a
# few hundred bytes; 64 KB leaves room for a detailed Figma export and still
# bounds what a library can push into a model file.
MAX_SVG_BYTES = 64_000
MAX_LIBRARY_SVG_BYTES = 1_000_000

# Colour placeholders the renderer substitutes so an icon follows the app
# theme (docs/ASSET_GRAPHICS.md). `currentColor` is real SVG; the others are
# plain names an author writes in place of a hex value.
THEME_TOKENS = ("currentColor", "product-fill", "product-accent", "product-muted")

_FORBIDDEN_TAGS = {"script", "foreignobject", "use", "image", "a", "iframe", "embed",
                   "object", "audio", "video", "handler", "set"}
_FORBIDDEN_TEXT = ("<!doctype", "<!entity", "<?xml-stylesheet", "@import")
_ANIMATIONS = {"animate", "animatetransform", "animatemotion"}
_ANIMATED_VALUES = ("to", "from", "values", "by")
_ADDRESS = re.compile(r"https?:|//|url\(", re.IGNORECASE)
_URL_REF = re.compile(r"url\(\s*(['\"]?)([^)'\"]*)\1\s*\)", re.IGNORECASE)


def _localname(tag: str) -> str:
    """"{http://www.w3.org/2000/svg}rect" -> "rect", lowercased."""
    return str(tag).rpartition("}")[2].lower()


def check_svg(text: str, *, max_bytes: int = MAX_SVG_BYTES) -> list[str]:
    """Every reason *text* may not be rendered, as human-readable strings;
    an empty list means it is safe. Never raises."""
    if not isinstance(text, str) or not text.strip():
        return ["the graphic is empty"]
    return list(_check_svg(text, max_bytes))


# Memoised: the verdict is a pure function of the text, and every palette
# refresh re-validates every library (hundreds of the same graphics each
# time -- most of a `MainWindow.new_model()`). A fresh list per call, so a
# caller can never edit the cached verdict.
@lru_cache(maxsize=2048)
def _check_svg(text: str, max_bytes: int) -> tuple[str, ...]:
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        return (f"the graphic is {size} bytes, over the {max_bytes} byte cap",)

    lowered = text.lower()
    found = [f"the graphic contains '{marker}'" for marker in _FORBIDDEN_TEXT
             if marker in lowered]
    if found:
        return tuple(found)  # never parse a document with a doctype or an entity

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return (f"the graphic is not well-formed XML ({exc})",)
    if _localname(root.tag) != "svg":
        return (f"the graphic's root element is <{_localname(root.tag)}>, not <svg>",)

    problems: list[str] = []
    fills = []
    for element in root.iter():
        tag = _localname(element.tag)
        if tag in _FORBIDDEN_TAGS:
            problems.append(f"the graphic uses <{tag}>, which is not allowed")
        if element.get("id") == FILL_ELEMENT_ID:
            fills.append(element)
        for raw_key, value in element.attrib.items():
            problems += _check_attribute(tag, _localname(raw_key), str(value))
        problems += _check_text(tag, element.text or "")
        if tag in _ANIMATIONS:
            problems += _check_animation(tag, element)
    problems += _check_fill(root, fills)
    return tuple(problems[:10])  # a broken file repeats itself; ten is plenty


def _check_fill(root, fills: list) -> list[str]:
    """The `id="fill"` region a level fill indicator is drawn in (PHASE4 T2,
    D75). The renderer asks QtSvg for ONE element's bounds, so a second one is
    ambiguous, and a region outside the viewBox would put the indicator off the
    icon. Only a `<rect>` with numeric geometry is bounds-checked -- a path's
    bounds need a path parser, which is not worth carrying here."""
    if not fills:
        return []
    if len(fills) > 1:
        return [f"the graphic has {len(fills)} elements with id=\"{FILL_ELEMENT_ID}\"; "
                f"the fill region must be exactly one"]
    box = _numbers(root.get("viewBox") or "")
    element = fills[0]
    if len(box) != 4 or _localname(element.tag) != "rect":
        return []
    try:
        x, y = float(element.get("x", 0.0)), float(element.get("y", 0.0))
        w, h = float(element.get("width", 0.0)), float(element.get("height", 0.0))
    except ValueError:
        return []
    if x < box[0] - _FILL_EPS or y < box[1] - _FILL_EPS \
            or x + w > box[0] + box[2] + _FILL_EPS or y + h > box[1] + box[3] + _FILL_EPS:
        return [f"the id=\"{FILL_ELEMENT_ID}\" region is outside the viewBox"]
    return []


def _numbers(text: str) -> list[float]:
    try:
        return [float(part) for part in re.split(r"[,\s]+", text.strip()) if part]
    except ValueError:
        return []


def _check_text(tag: str, text: str) -> list[str]:
    """An element's text (CSS in a `<style>`) holds no external reference."""
    lowered = text.lower()
    if re.search(r"https?:", lowered):
        return [f"<{tag}>'s text names an address"]
    for _quote, target in _URL_REF.findall(text):
        if not target.strip().startswith("#"):
            return [f"<{tag}>'s text points at '{target[:60]}' outside the document"]
    return []


def _check_animation(tag: str, element) -> list[str]:
    """An animation may not retarget a reference or animate toward an address."""
    attribute = {_localname(key): str(value) for key, value in element.attrib.items()}
    if attribute.get("attributename", "").strip().lower().rpartition(":")[2] == "href":
        return [f"<{tag}> animates a reference (href)"]
    for key in _ANIMATED_VALUES:
        if _ADDRESS.search(attribute.get(key, "")):
            return [f"<{tag}>'s '{key}' holds an address"]
    return []


def _check_attribute(tag: str, key: str, value: str) -> list[str]:
    lowered = value.strip().lower()
    if key.startswith("on"):
        return [f"<{tag}> carries the event handler '{key}'"]
    if key == "href" and not value.strip().startswith("#"):
        return [f"<{tag}> references '{value[:60]}' outside the document"]
    if "javascript:" in lowered.replace(" ", ""):
        return [f"<{tag}>'s '{key}' contains a javascript: URL"]
    if "data:" in lowered:
        return [f"<{tag}>'s '{key}' embeds a data: URL"]
    for _quote, target in _URL_REF.findall(value):
        if not target.strip().startswith("#"):
            return [f"<{tag}>'s '{key}' points at '{target[:60]}' outside the document"]
    return []


def substitute_tokens(text: str, colors: dict) -> str:
    """Replace the theme tokens in *text* with concrete colours. Plain string
    replacement on purpose: it works on a Figma export untouched, and a token
    is only ever written where a colour value goes."""
    for token in THEME_TOKENS:
        replacement = colors.get(token)
        if replacement:
            text = text.replace(token, str(replacement))
    return text
