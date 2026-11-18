# Auto-close Sigstore and Chainguard auth tabs (Firefox)

Based on the Chrome extension [imjasonh/chrome-sigstore](https://github.com/imjasonh/chrome-sigstore).

`cosign sign`, `gitsign` and `chainctl auth login` finish their OAuth flows on a
localhost "auth successful" page that browsers won't let close itself. This
extension closes those tabs as soon as they load. The toolbar badge counts how
many it has closed.

It closes a tab only when both of these hold:

- the URL is `http://localhost:<any port>/...` or `http://127.0.0.1:<any port>/...`
- the title is `Sigstore Authentication` or `Chainguard Authentication`

Closing the tab doesn't affect the Fulcio certificate, the signature or the
chainctl token.

## Changes from the Chrome version

| Chrome | Firefox |
|---|---|
| `background.service_worker` | `background.scripts` (Firefox MV3 runs background scripts as an event page) |
| Match pattern `http://localhost:*/*` | Firefox match patterns can't contain ports, so `background.js` checks the hostname itself. It also matches `127.0.0.1` |
| Re-queries every tab on every tab update | Checks only the tab that changed, filtered to `url`, `title` and `status` changes |
| `host_permissions: http://localhost/*` | Dropped. `tabs` already exposes URL and title, so only `tabs` and `storage` are requested |
| — | `browser_specific_settings.gecko`: add-on ID (needed for `storage.sync` and signing) and `data_collection_permissions: none` (required for new AMO submissions) |
| — | Restores the badge count at startup and closes any matching tabs already open, such as tabs restored from the last session |

Requires Firefox 140 or later. Tested in Zen 1.24.1b, which is built on Firefox 159.

## Install

### Try it (until Firefox restarts)

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on…** and pick `manifest.json` from this folder.

### Install permanently

Release Firefox only installs signed extensions. To sign this one for your own
use without listing it publicly:

1. Create API credentials at <https://addons.mozilla.org/developers/addon/api/key/>.
2. Export them as `WEB_EXT_API_KEY` and `WEB_EXT_API_SECRET`, then run this
   from this folder, so `web-ext-config.mjs` keeps the tests out of the package:

   ```sh
   npx web-ext sign --channel=unlisted
   ```

3. Open the `.xpi` from `web-ext-artifacts/` in Firefox.

The add-on ID `sigstore-close-post-auth@gruffled` in `manifest.json` gets tied
to the AMO account that signs it first. Change the ID before signing if you
want a different one.

Firefox Developer Edition and Nightly can install it unsigned if you set
`xpinstall.signatures.required` to `false` in `about:config`. Package it with:

```sh
zip -r -FS ../firefox-sigstore.xpi manifest.json background.js images LICENSE
```

## Testing

```sh
# Stubbed browser API, no browser needed
node test/background.test.mjs

# Launches Zen on a throwaway profile with the extension loaded.
# Pass a Firefox binary path to test in Firefox instead.
python3 test/browser_e2e.py
```

## Recommended CLI config

Set a default identity provider so the browser goes straight to the IdP
without asking:

```sh
git config --global gitsign.connectorID https://accounts.google.com
chainctl config set default.social-login google-oauth2
```
