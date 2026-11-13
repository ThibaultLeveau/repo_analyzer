import re
from base64 import b64encode
from html import escape

from merm import render_diagram


def on_page_markdown(markdown: str, **kwargs: object) -> str:
    def image(match: re.Match[str]) -> str:
        name, source = match.groups()
        svg = render_diagram(source, theme="dark").encode()
        content = b64encode(svg).decode("ascii")
        return f'<img alt="{escape(name, quote=True)}" src="data:image/svg+xml;base64,{content}">'

    return re.sub(
        r'^```mermaid "([^"\n]+)"\n(.+?)\n```$',
        image,
        markdown,
        flags=re.MULTILINE | re.DOTALL,
    )
