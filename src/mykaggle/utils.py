import base64
import os
import tempfile
from typing import Optional

from rich.console import Console
from rich.text import Text


def normalize_nl(text):
    """Normalize CRLF/CR line endings to LF.

    Rich drops lines containing carriage returns, and some kernels emit
    CRLF (e.g. `!` shell output). Normalize before display.

    >>> normalize_nl("a\\r\\nb\\r\\n")
    'a\\nb\\n'
    >>> normalize_nl(["a\\r\\n", "b\\r"])
    ['a\\n', 'b\\n']
    >>> normalize_nl("plain\\n")
    'plain\\n'
    """
    if isinstance(text, list):
        return [normalize_nl(t) for t in text]
    if isinstance(text, str):
        return text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def render_display_data(data: dict):
    """Pick the richest printable representation, colab-style."""
    if "text/plain" in data:
        text = data["text/plain"]
        if isinstance(text, list):
            text = "".join(text)
        return Text.from_ansi(normalize_nl(str(text)))
    for mime in ("text/markdown", "text/html"):
        if mime in data:
            text = data[mime]
            if isinstance(text, list):
                text = "".join(text)
            return Text(normalize_nl(str(text)))
    return None


def handle_image(b64_data: str, mime_type: str, target_path: Optional[str] = None):
    ext = "png" if mime_type == "image/png" else "jpg"
    if target_path:
        path = target_path
    else:
        fd, path = tempfile.mkstemp(suffix=f".{ext}", prefix="mykaggle-plot-")
        os.close(fd)
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64_data))
    Console().print(f"[dim]Image saved to {path}[/dim]")
    return path


def print_output(output: dict, console: Console, output_image: Optional[str] = None):
    """Render one kernel output. Shared by exec/repl/install."""
    if "text" in output:
        console.print(Text.from_ansi(normalize_nl(output["text"])), end="")
    elif "data" in output:
        data = output["data"]

        image_displayed = False
        for mime_type in ["image/png", "image/jpeg"]:
            if mime_type in data:
                handle_image(data[mime_type], mime_type, target_path=output_image)
                image_displayed = True
                break

        text = render_display_data(data)
        if text is not None:
            if isinstance(text, Text) and image_displayed:
                if any(
                    x in text.plain
                    for x in ["<IPython.core.display.Image", "<Figure size"]
                ):
                    return
            console.print(text)
    elif output.get("output_type") == "error":
        ename = output.get("ename", "Error")
        evalue = output.get("evalue", "")
        traceback = output.get("traceback", [])
        if traceback:
            console.print(Text.from_ansi(normalize_nl("".join(traceback))))
        else:
            console.print(f"[bold red][!][/bold red] {ename}: {evalue}")
