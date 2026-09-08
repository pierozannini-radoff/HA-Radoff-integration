# Radoff integration for Home Assistant

Official Home Assistant integration for Radoff air quality devices. It connects
to your Radoff cloud account and exposes your devices' readings as Home
Assistant sensor entities.

> **Placeholder notice:** the GitHub organization/repository this integration
> will live under has not been finalized yet. All links in this document
> currently point to `https://github.com/radoff/ha-radoff-integration` as a
> placeholder and will be corrected when the repository transfer (tracked
> separately) is completed.

## What this integration does

The integration authenticates against the Radoff cloud API with your Radoff
account credentials, then polls your account's devices (every 60 seconds by
default - configurable, see "Options" below) and creates one Home Assistant
sensor entity per measured property per device.

This is a **cloud polling** integration (`iot_class: cloud_polling`): it does
not talk to your Radoff device directly or over the local network, so it only
works while both Home Assistant and the device have internet access.

## Supported devices

Currently supported, in this release:

- **Radoff Now+**

**Not yet supported:** Radoff Now (legacy) and Radoff Sense. Devices of these
types are not detected by this version of the integration. Support for both
is planned and tracked in the roadmap for a future release (see the `L`
milestone in the project's internal planning); this README will be updated
once they land.

## Installation

### With HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=radoff&category=integration&repository=ha-radoff-integration)

More information about HACS [here](https://hacs.xyz/).

### Manually

Clone this repository and copy `custom_components/radoff` into your Home
Assistant config directory (for example `config/custom_components/radoff`),
then restart Home Assistant.

## Configuration

Once Home Assistant has restarted, go to **Settings → Devices & Services →
Add Integration**, search for `Radoff` and select it.

The setup form asks for two fields:

- `username` - the email address of your Radoff account (the same one used
  in the Radoff mobile app)
- `password` - the password of that account

If your account has access to more than one Radoff domain (tenant), you will
see one additional step asking you to pick which domain to use; if it only
has access to one, the integration entry is created immediately after
authentication succeeds. There is no device-selection step: every Now+ device
visible on the selected domain is added automatically.

If your Radoff account password changes, Home Assistant will show a
**Reconfigure** notification on the integration instead of silently failing
(see "Known limitations" below for the current timing).

## Options

Once the integration is configured, open **Settings → Devices & Services →
Radoff** and select **Configure** to change:

- **Polling interval** - how often, in seconds, the integration polls the
  Radoff API. Defaults to 60 seconds; the form enforces a minimum (30
  seconds today - see "Known limitations") and a maximum of 3600 seconds
  (one hour). Saving reloads the integration and the new interval takes
  effect immediately, no restart needed.
- **Generate index entities** - whether to also create a qualitative
  `*_index` sensor (Excellent, Good, Medium, Poor, Terrible) alongside each
  applicable measurement, in addition to its raw value. Enabled by default.
  Toggling this off removes the `*_index` entities on the next reload;
  toggling it back on recreates them.

If the Radoff API responds with a rate-limit error (HTTP 429), the log
message names your account's currently configured polling interval and
points you at this options page.

## Entities produced

For every Now+ device found on your account, the integration creates one
sensor entity per measured property, currently: temperature, humidity,
pressure, CO₂ (eCO₂), TVOC, PM1, PM2.5, PM10, and an air quality index.

If the "Generate index entities" option is enabled for the config entry
(enabled by default - see "Options" above), an additional qualitative
`*_index` sensor (e.g. Excellent, Good, ...) is created alongside each of the
applicable measurements.

## Known limitations

- **Device coverage**: only Now+ is supported today; Now (legacy) and Sense
  are not detected (see "Supported devices" above).
- **Polling interval floor**: the options form currently enforces a 30-second
  minimum. This is a conservative, provisional value, not one derived from
  any rate limit Radoff has confirmed - the integration polls with an N+1
  request pattern (one search call plus one per-device call every cycle),
  so a lower floor risks tripping rate limits on accounts with several
  devices. It will be revisited once Radoff's backend team confirms the
  API's actual rate limits.
- **Re-authentication timing**: if your Radoff account password changes, Home
  Assistant will prompt you to re-enter it (**Settings → Devices & Services →
  Radoff → Reconfigure**) without removing or re-adding the integration and
  without losing entity history. Since this integration now renews its
  Cognito session via a lightweight token refresh instead of a full login on
  every cycle, how quickly the prompt appears depends on when that refresh
  (or a live request) actually gets rejected by Radoff's cloud, rather than
  on a fixed token lifetime: this can be as soon as the next poll or two (if
  Radoff invalidates sessions immediately), after two consecutive rejected
  refresh attempts, or - in the worst case, if the existing session is
  neither actively revoked nor ever fails a refresh - as late as the
  underlying Cognito session's own refresh-token lifetime, which has not
  been independently verified against a real account (Cognito's own default
  is 30 days). Reloading the integration or restarting Home Assistant forces
  an immediate full login instead of waiting.
- **Availability**: entities do not yet reflect the freshness of the
  underlying device data; a device that has stopped reporting to Radoff may
  continue to show its last known values.

## Getting support

If something isn't working, please open an issue on this repository's issue
tracker rather than emailing logs around. Before opening an issue:

1. In Home Assistant, go to **Settings → Devices & Services → Radoff**, open
   the integration, and download the diagnostics file (⋮ menu → **Download
   diagnostics**).
2. Attach that diagnostics file to the issue instead of pasting raw logs.
   Diagnostics are redacted of credentials and tokens before download; raw
   debug logs are not, and may contain data that should not be shared
   publicly.
3. Describe what you expected to happen and what happened instead, including
   your Radoff device model.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for how to
set up the development environment, run lint and tests, and the branching and
versioning conventions used in this repository.

## License

This project is licensed under the MIT License - see [LICENSE](LICENSE) for
details. See [NOTICE](NOTICE.md) for attribution to the `integration_blueprint`
template this repository was originally scaffolded from.