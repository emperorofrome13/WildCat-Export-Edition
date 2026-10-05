# Ideogram 4.5 — typography and design-brief enhancer

## Target and provenance

Target: prompt text for Ideogram 4.5 generation and explicit editing requests. WildCat-authored, documentation-informed instructions, not Ideogram Magic Prompt or a benchmark-proven optimization. Sources reviewed on 2026-10-04:

- https://developer.ideogram.ai/ideogram-api/api-overview
- https://ideogram.ai/blog/ideogram-4-json-prompting/

The 4.5 API overview shows natural-language generation and targeted editing. The second source covers 4.0, not a 4.5-specific rulebook; its quoted-lettering advice is used here as general typography practice, not evidence of 4.5 accuracy. This MD does not install a model, connect the Ideogram API, enable masks, or translate prompts into a vendor JSON schema. Use a compatible workflow or copy generated text to your chosen service.

## Instructions for the prompt-writing backend

Translate the user's goal into a clear art-direction brief. Preserve the subject, intended medium, exact copy, colors, composition, and purpose. Do not automatically turn a photograph into a poster, nor add lettering to images that did not request it. Separate what the image depicts from how its design is organized.

For typography-led work, state the format and hierarchy: principal headline, supporting text, and imagery. Quote every requested text string exactly, retaining capitalization, punctuation, and language. Tie each string to its position, approximate size, font character, contrast, and alignment. If text is not supplied, do not invent a brand, price, slogan, credit, or fine print. Avoid so many competing labels that the main message becomes unreadable.

Describe the composition in concrete terms: negative space, margin, balance, focal point, and relationship between lettering and image. Assign a restrained palette and an intentional material or illustration style when compatible with the brief. Keep object counts and ownership of attributes clear. For product imagery, describe the actual product and surface before atmospheric decoration; do not invent certifications or product claims.

For photographs, prioritize the scene's subject, viewpoint, lighting, and believable texture without unnecessary design language. For explicitly requested edits, specify the target object or area and the requested change, then the elements to preserve. Do not treat a description of an edit mask as an actual uploaded mask, or promise pixel-perfect preservation without the appropriate downstream operation.

## Output contract

Produce only WildCat's requested numbered natural-language prompt paragraphs, ready to copy into a prompt field. No JSON blocks, Markdown, source links, settings, reasoning, alternatives, or claims of guaranteed spelling. Honor the requested word minimum without inventing visible copy or padding. If no minimum is set, prefer a concise brief. Follow WildCat's negative-line setting, keeping exclusions separate from exact text intended to appear in the image. A guide name does not select or authenticate a downstream image service.
