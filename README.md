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
account credentials, then polls your account's devices (every 5 minutes by
default - configurable, see "Options" below) and creates one Home Assistant
sensor entity per measured property per device.

This is a **cloud polling** integration (`iot_class: cloud_polling`): it does
not talk to your Radoff device directly or over the local network, so it only
works while both Home Assistant and the device have internet access.

## Supported devices

Officially supported and verified in this release:

- **Radoff Now+**

**Other device types (Now, Sense, City, Sismoff):** no longer hidden. Earlier
versions dropped every device that was not a Now+, so they simply did not
appear in Home Assistant. This version reads the measurement schema Radoff
publishes for each device type, so a device of another type is created with
the entities its type declares - units, labels and quality bands included.

That is best effort, not a promise: only Now+ is tested end to end against a
real device, entity names for other types may change as their schemas do, and
a device whose type Radoff's catalogue does not know at all still appears,
with its readings published as plain numbers and a warning in the log.

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
see one additional step asking you to pick which domain to use, listed by
name; if it only has access to one, the integration entry is created
immediately after authentication succeeds, with no extra question. There is
no device-selection step: every device visible on the selected domain is added
automatically - and if the domain you pick has no device at all, setup stops
and says so rather than creating an integration with nothing in it.

If your Radoff account password changes, Home Assistant will show a
**Reconfigure** notification on the integration instead of silently failing
(see "Known limitations" below for the current timing).

## Options

Once the integration is configured, open **Settings → Devices & Services →
Radoff** and select **Configure** to change:

- **Polling interval** - how often, in seconds, the integration polls the
  Radoff API. Defaults to 300 seconds (5 minutes); the form enforces a
  minimum of 60 seconds and a maximum of 3600 (one hour). Saving reloads the
  integration and the new interval takes effect immediately, no restart
  needed. See "How much this integration polls" below for why those are the
  numbers.
- **Generate index entities** - whether to also create a qualitative
  `*_index` sensor (Excellent, High, Good, Poor, Terrible - Low, Good, High
  for temperature and humidity) alongside each applicable measurement, in
  addition to its raw value. Enabled by default.
  Toggling this off removes the `*_index` entities on the next reload;
  toggling it back on recreates them.

If the Radoff API responds with a rate-limit error (HTTP 429), the log
message names your account's currently configured polling interval and
points you at this options page.

## How much this integration polls

The cost of one installation, in requests:

- **one request every 300 seconds** (the configured interval), plus one more
  per additional page if your account has more than 200 devices - the device
  list carries each device's telemetry inline, so a cycle costs the same
  whether you own one device or fifty;
- **one request per device type, per restart** - the measurement schema of a
  type, fetched once when the integration loads and then cached for as long
  as it stays loaded. Radoff's catalogue has five types, so this is at most
  five requests at startup and none afterwards.

The two numbers behind the interval:

- **60 seconds is the floor because of the device, not the API.** A Radoff
  device emits one message per minute, and the radon value it carries is
  aggregated over at least five minutes. Polling faster returns the same
  reading again - it does not make your data fresher.
- **300 seconds is the default because the quota is shared.** Radoff's API
  allows 50 requests/second sustained (100 in a burst) *per environment*,
  shared by the Radoff mobile app, the web app and every integration
  together; there is no separate allowance per user or per installation.

Each installation also polls on a small fixed offset of its own (up to 10% of
the interval, derived from the config entry, so it is the same across
restarts). Without it, every installation configured at the default would
send its request on the same round minute and turn a load the shared quota
absorbs comfortably into a periodic spike.

On a rate-limit error the integration skips the cycle rather than retrying
immediately, backing off from about 5 seconds and doubling while the errors
continue. Your entities keep the readings from the previous cycle during a
skipped one: a 429 means the request was not made, not that your device went
offline.

## Entities produced

For every device found on your account, the integration creates one sensor
entity per measurement its device type declares. It does not decide that list
itself: it asks Radoff's API which measurements a device type has, and with
which unit and which quality bands. On a Now+ that is temperature, humidity,
pressure, CO₂ (eCO₂), TVOC, PM1, PM2.5, PM10 and an air quality index; a
Sense adds radon, a Sismoff adds carbon monoxide and methane.

If the "Generate index entities" option is enabled for the config entry
(enabled by default - see "Options" above), an additional qualitative
`*_index` sensor (Excellent, High, Good, Poor, Terrible - Low, Good, High for
temperature and humidity) is created alongside each measurement that has
quality bands.

Two entities behave differently on purpose:

- **Air quality index**: created **disabled**. Radoff is aware of a defect in
  how the index's temperature component is computed on their side, so the
  published value is currently unreliable. The entity is there and can be
  enabled from **Settings → Devices & Services → Radoff → entities**; it will
  become enabled by default again once the calculation is fixed, with no
  effect on installations that enabled it by hand.
- **Pressure**: reported in pascals (Pa), the unit the API sends. To see it
  in hPa or mbar, change the unit on the entity itself
  (entity settings → **Unit of measurement**); Home Assistant converts it for
  you and keeps the history.

**TVOC** deserves a note: Radoff's API declares its unit as `V - Ix`, which is
not a concentration. The entity is published with that unit exactly as the API
gives it and with no device class - the alternative, a bare number with no
unit, looks like a concentration whose unit went missing. If you are upgrading
from a version that showed TVOC in µg/m³, see "Updating from an earlier
version" below: the unit change breaks that entity's long-term statistics.

## Updating from an earlier version

Updating to this version migrates your existing entities rather than replacing
them. Every sensor keeps its entity ID, so **your dashboards, automations and
history keep working**, and nothing has to be removed and added again. What
changes underneath is the identifier the integration uses internally: entities
used to be keyed on Radoff's internal device ID, and are now keyed on the
device's serial number, which is the only identifier the current API has.

You may be asked to pick your domain once, after the update. Configurations
created before this version either never recorded one or recorded it in a form
the current API does not accept, and there is no way to work it out without
asking Radoff - so the integration raises a repair under **Settings → System →
Repairs** and the choice is one click. Your devices and their history are
untouched while it is pending.

Four things do change visibly, and they are worth knowing about *before* you
see them in a graph:

- **Temperature jumps by about 4 °C.** The released version multiplied the
  raw reading by a wrong factor. The new values are the correct ones; the step
  in your history is where the fix landed, not a sensor fault.
- **TVOC's unit changes** from µg/m³ to `V - Ix` (see above). Home Assistant
  treats a unit change on an existing entity as a break in its long-term
  statistics: the old series is closed and a new one starts, so expect a gap
  in that one graph. The short-term history is unaffected and the entity keeps
  working throughout.
- **The quality levels are renamed.** They now come from Radoff's API instead
  of being invented here: `medium` no longer exists, and the scale is
  Excellent → High → Good → Poor → Terrible (Low → Good → High for temperature
  and humidity). If an automation compares an `*_index` sensor against a state
  string, check it - the numeric sensors are unaffected.
- **The air quality index behaves differently depending on where you came
  from.** It is created disabled on a new installation (see "Entities produced"
  above), but an AQI entity you already had stays enabled: an update does not
  take away something you were already using. Two otherwise identical
  installations can therefore show it differently.

New entities simply appear where your devices support them - radon on a Sense,
carbon monoxide and methane on a Sismoff - with no history behind them, because
the released version never created them.

## Known limitations

- **Device coverage**: only Now+ is verified end to end. Devices of other
  types are created from the schema their type declares, without that
  verification (see "Supported devices" above).
- **Per-device configuration**: the measurement schema is published per device
  *type*, not per device. An individual unit with a measurement switched off
  in its Radoff configuration will still get that entity, and it will show
  `unknown` permanently - from here it is indistinguishable from a measurement
  the device has simply not sent yet.
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
- **Availability**: an entity is unavailable when Radoff reports its device
  as disconnected, and at no other time. In particular, a connected device
  that sends nothing keeps its entities available: Radoff's device list looks
  at a six-hour window and does not widen it, so a device quieter than that
  is reported without telemetry while still being online, and treating that
  as a fault would report working devices as broken.

  What such an entity shows is the last value it received, restored across
  Home Assistant restarts - so history stays continuous - or `unknown` if it
  has never received one. **The state alone does not tell you how old a value
  is**: use the entity's `last_measured_at` attribute, which always carries
  the timestamp of the value actually being shown. Radon is where this
  matters most, since it is sampled more slowly than everything around it.

  Two more attributes are there for support: `connection_status_updated_at`
  and `status` (the administrative status, which is a different field and
  does not affect availability). `connection_status_updated_at` is when
  Radoff last **changed** the device's connection status, not when it last
  checked it - so on a device that has been online for a month it reads a
  month old, and that is what a healthy device looks like, not a stale
  reading.

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