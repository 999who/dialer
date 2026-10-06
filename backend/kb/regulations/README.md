Put official regulations, price lists and scripts here as Markdown (`*.md`).
`tools/ingest_kb.py` splits them by headings (~900 characters per chunk), embeds
each chunk and stores it as `kind=regulation`. The heading becomes the chip title
in the overlay ("Regulamin rabatów § 4.2"). Front matter is optional:

    ---
    topic: rabaty
    verified: true
    source_ref: https://emanager.pro/regulamin
    ---
