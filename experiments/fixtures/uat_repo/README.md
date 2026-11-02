# UAT CSV Fixture

A small Python project used for AtlasOS V2 real user acceptance.

The project parses simple CSV text into rows. It currently handles unquoted fields but not
RFC-4180-style quoted fields containing commas or escaped double quotes.

Requirements for the acceptance task:

- preserve the public `parse_csv(text: str) -> list[list[str]]` API;
- fix quoted commas and escaped double quotes;
- keep existing behavior for plain unquoted rows;
- add regression tests;
- do not change unrelated files.
