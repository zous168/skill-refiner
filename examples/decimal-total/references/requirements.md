# Monetary total requirements

- Input is a local UTF-8 JSON file with an `amounts` array of decimal strings.
- Sum amounts exactly in base 10, preserving cents and negative signs. Inputs in
  this version have at most two digits after the decimal point.
- Return one JSON object with exactly one field, `total`, a string formatted with
  exactly two digits after the decimal point. Example shape: `{"total":"2.00"}`.
- An empty array must return `{"total":"0.00"}`.
- The user task must be performed by the bundled script; do not work around defects
  by manually computing the answer in the final response.
- Invalid input behavior is outside this demonstration's scope; do not add a new
  validation API while repairing the two promised capabilities.

