from typing import NotRequired, TypedDict


class DocumentItem(TypedDict):
    url: str
    canonical_url: str
    title: str
    text: str
    blocks: NotRequired[list[str]]
