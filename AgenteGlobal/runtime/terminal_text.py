"""Small terminal-only text normalizers.

The model may use Markdown emphasis, while the interactive terminal deliberately
does not render Markdown.  Remove bold delimiters without changing the stored
conversation or other punctuation.  The streaming filter preserves delimiters
split across provider chunks.
"""

from __future__ import annotations


def plain_terminal_markdown(text: str) -> str:
    """Return terminal-friendly text without literal Markdown bold markers."""

    return str(text).replace("**", "")


class StreamingTerminalMarkdownFilter:
    """Strip ``**`` safely when the two asterisks arrive in separate chunks."""

    def __init__(self) -> None:
        self._pending_star = False

    def feed(self, text: str) -> str:
        output: list[str] = []
        for char in str(text):
            if self._pending_star:
                if char == "*":
                    self._pending_star = False
                    continue
                output.append("*")
                self._pending_star = False
            if char == "*":
                self._pending_star = True
            else:
                output.append(char)
        return "".join(output)

    def flush(self) -> str:
        if not self._pending_star:
            return ""
        self._pending_star = False
        return "*"


__all__ = ["StreamingTerminalMarkdownFilter", "plain_terminal_markdown"]
