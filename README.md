# Radoff integration for Home Assistant
Install this repository using HACS

### Limitations
Currently supported devices:
- Now+

> Unfortunately I don't have any other Radoff device but I would be glad to extend this integration.

## Installation

You can install it using HACS or manually.

### With HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=raelix&category=integration&repository=ha-radoff-integration)

More informations about HACS [here](https://hacs.xyz/).

#### Manually

Clone this repository and copy `custom_components/radoff` to your Home Assistant config durectory (ex : `config/custom_components/radoff`)

Restart Home Assistant.

## Configuration

Once your Home Assistant has restarted, go to `Settings -> Devices & Services -> Add an  integration`.

Search for `radoff` and select the `Radoff` integration.

Enter your Radoff credentials.

If connection is working, you should have a list of devices configured on your account.

Select the device you want to add.

### Required parameters

- ```username```
- ```password```

### Dev
If you want to test out the integration just open it in 
[![Open in Dev Containers](https://img.shields.io/static/v1?label=Dev%20Containers&message=Open&color=blue&logo=visualstudiocode)](https://vscode.dev/redirect?url=vscode://ms-vscode-remote.remote-containers/cloneInVolume?url=https://github.com/raelix/ha-radoff-integration)

`scripts/develop` runs Home Assistant against the local dev harness at
`.devcontainer/config/`, which is where you configure a real Radoff device
while developing.

**Warning — that directory contains cleartext credentials.** On first run, HA
writes `.devcontainer/config/.storage/core.config_entries` there, which holds
the Radoff username/password and Cognito tokens for whatever account you use
to test the integration, plus the local HA instance's own auth tokens and
SQLite database. Only `.devcontainer/config/configuration.yaml` is tracked in
git (`.gitignore` excludes everything else under that path) — never force-add
anything else in `.devcontainer/config/`, and never run `git add -A` /
`git add -f` there. If you think credentials from this harness may have been
committed at some point, stop and rotate them (Radoff account password, HA
local user, and revoke HA refresh tokens) before doing anything else; do not
rely on removing the files from the working tree, since that does not remove
them from git history.

### Warnings

Please do not use this against the real Radoff APIs as they are not intended to be exposed so I'm not responsible for any wrong use of this repository.