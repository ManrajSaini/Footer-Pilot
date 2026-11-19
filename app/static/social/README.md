# Social icons

48x48 px PNGs (shown at 22 px), white glyph on a neutral grey circle. Signatures link to
them by absolute URL: `{ASSET_BASE_URL}/static/social/<network>.png`. File names must match
the network keys in `app/signatures/schema.py`.

- Glyphs for X, Facebook, Instagram, YouTube and GitHub come from
  [Simple Icons](https://simpleicons.org) (CC0 1.0).
- LinkedIn is not part of that set, so it is drawn as an "in" lettermark.
- The marks themselves are trademarks of their owners and are used only to link to
  profiles on those services.

Regenerate or extend the set with `scripts/build_social_icons.mjs`.

Do not rename or delete a file once signatures are live: mailboxes keep pointing at
the old URL until the next deployment.
