# Submission status · 2026-11-10

## Completed

| Artifact | Public evidence |
| --- | --- |
| Plugin source 0.1.0 | [snuff12/hermes-sell](https://github.com/snuff12/hermes-sell), commit `03f1acee3187c62505ac45e8d987193381b7fa2a` |
| Server source and setup | [snuff12/hermes-sell-server](https://github.com/snuff12/hermes-sell-server) |
| Catalog application | [NousResearch/hermes-agent#152641](https://github.com/NousResearch/hermes-agent/pull/152641), one catalog YAML file |
| Working demo | [84-second MP4](https://github.com/snuff12/hermes-sell/releases/download/v0.1.0/hermes-sell-demo.mp4) |
| Captions and sanitized capture evidence | [Release assets](https://github.com/snuff12/hermes-sell/releases/tag/v0.1.0) |
| Official plugin validation | [Public CI](https://github.com/snuff12/hermes-sell/actions), 16 checks passed with zero warnings |
| Server validation | [Public CI](https://github.com/snuff12/hermes-sell-server/actions), production build, 273 Python tests and 38 browser tests passed |

The public source commits and the catalog PR's sole commit are SSH-signed.
GitHub reports `verification.verified=true` and `reason=valid`. The catalog PR
has no file conflict. Maintainer review and a maintainer-started catalog CI run
are pending; the plugin is not yet an accepted catalog entry.

The exact published plugin commit was cloned, its dependencies installed and
the plugin enabled through the normal Hermes CLI in a fresh disposable home.
The personal Hermes profile was not changed.

## What the video demonstrates

- Native `service_search` in a clean Hermes v0.23.0 host with a simulation service.
- Seller setup, an existing frozen service, and the shared directory.
- Recovery of an existing paid Base Sepolia order, its actual model result and receipt.
- An actual review signature and purchase/chain verification.
- Buyer history and spending, and seller execution/payment analytics.

The recording uses a dedicated automated test-wallet bridge. It signs one login
message and one review message, with zero payment signatures and no new purchase.
The two original seller dispatch and settlement intents remain unchanged.
The video is edited to 84.13 seconds, has English narration and captions, and
uses H.264/AAC MP4. Native tool output is explicitly labeled as captured output;
browser scenes are recordings of the running local application.

## Entrant handoff

The entrant will handle the X post and final submission form later. No further
account details are being requested, and neither action has been performed.

- Publish the video on X, tagging `@NousResearch`.
- Submit the X post and catalog PR URLs, title, description and entrant contact
  information through [the official form](https://form.typeform.com/to/FwJLvOln).
- Wait for catalog maintainer review; a PR can be used for the hackathon form
  before it is merged.

Browser control became unavailable during submission: native window operations
reported `cgWindowNotFound`, and browser-tab creation reported a missing app-server
executable. No X post or form receipt was produced. The remaining publication
and form steps are now reserved for the entrant. Contact details are not stored
in public source.

The advertised deadline is November 11, 2026, 17:00 EST (November 12, 07:00 KST).
The form's thank-you text also mentions 13:00 PST, one hour earlier. Use November
12, 06:00 KST as the internal cutoff. A later form auto-close setting is not an
announced deadline extension.

External server hosting and open Internet seller enrollment remain deferred.
