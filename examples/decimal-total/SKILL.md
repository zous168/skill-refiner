---
name: decimal-total
description: Calculate the exact signed monetary total from a local JSON amounts file. Use when the user asks to total monetary amounts from such a file.
---

# Decimal total

Read the JSON input file requested by the user. It contains an `amounts` array of
decimal strings. Run the bundled [total script](scripts/total.py) with the file path:

```sh
python3 <installed-skill-directory>/scripts/total.py input.json
```

Return the script's JSON output verbatim, without Markdown or manual recalculation.
Always execute the script; do not replace it with an independent implementation.
The output contract is described in [the requirements](references/requirements.md).

