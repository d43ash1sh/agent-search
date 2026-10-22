from __future__ import annotations

from services.common.models import SearchHit


class CitationFormatter:
    """Formats retrieved search evidence for LLM prompt grounding and citation."""

    @staticmethod
    def format_markdown(hits: list[SearchHit]) -> str:
        """Format hits into academic Markdown footnotes with clickable links."""
        lines = ["### Retrieved Evidence Sources\n"]
        for idx, hit in enumerate(hits, start=1):
            title = hit.title or "Untitled Document"
            clean_snippet = hit.text.replace("\n", " ").strip()
            lines.append(
                f"[^{idx}]: [{title}]({hit.canonical_url})\n"
                f'    > "{clean_snippet}"\n'
                f"    *Score: {hit.score:.4f} | Tokens: ~{hit.estimated_tokens} | ID: {hit.evidence_id}*\n"
            )
        return "\n".join(lines)

    @staticmethod
    def format_xml(hits: list[SearchHit]) -> str:
        """Format hits into structured XML grounding blocks for LLMs."""
        lines = ["<search_grounding>"]
        for hit in hits:
            clean_text = hit.text.strip()
            lines.append(
                f'  <evidence id="{hit.evidence_id}" url="{hit.canonical_url}" title="{hit.title}">\n'
                f"    <![CDATA[{clean_text}]]>\n"
                f"  </evidence>"
            )
        lines.append("</search_grounding>")
        return "\n".join(lines)

    @staticmethod
    def format_agent_prompt_context(hits: list[SearchHit], query: str) -> str:
        """Complete prompt-ready grounding context block instructing the agent on citation."""
        evidence_body = CitationFormatter.format_markdown(hits)
        return (
            f"You are answering the user's question using only the verified evidence below.\n"
            f"User Question: {query}\n\n"
            f"{evidence_body}\n"
            f"Strict Instruction: Always cite your claims using footnotes [^1], [^2] matching the evidence sources above."
        )
