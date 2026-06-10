# SafetyLLMGateWay

A local safety gateway for LLM API traffic. The first implementation provides a minimal reverse proxy that reads a local settings file once at startup and forwards requests to the configured upstream.

## Install for development

```powershell
conda create -n lsg python=3.13
conda activate lsg
python -m pip install -r requirements-dev.txt
python -m pip install -e .
```

## Configuration

The gateway uses a JSON settings file. It does not automatically read `.env` files.

Default settings path: `~/.lsg/settings.json`

For development, point the gateway at a repo-local ignored file:

```powershell
lsg config init --config .\.dev\settings.json
$env:LSG_CONFIG_PATH = ".\.dev\settings.json"
```

Then edit `.dev/settings.json` and fill in the upstream values:

```json
{
  "version": 1,
  "server": {
    "host": "127.0.0.1",
    "port": 8178
  },
  "defaultUpstream": "default",
  "auth": {
    "enabled": false,
    "downstreamApiKeys": []
  },
  "upstreams": {
    "default": {
      "baseUrl": "https://api.openai.com",
      "apiKey": "replace-with-upstream-api-key",
      "routePrefix": null
    }
  }
}
```

`LSG_CONFIG_PATH` overrides the default path. The process loads and validates settings once when `lsg start` runs; it does not reread the settings file for every proxied request.

## Runtime state

`lsg start` writes runtime state for the currently managed gateway process. Runtime state is separate from `settings.json` and does not store API keys.

Default runtime state path: `~/.lsg/state.json`

`LSG_RUNTIME_STATE_PATH` overrides the default runtime state path.

## Run

```powershell
lsg start
```

The gateway exposes public `GET /health` locally. It returns only minimal liveness JSON:

```json
{"status":"ok"}
```

The public health response does not require downstream authentication or a local management token, is not proxied to an upstream, and does not include config paths, upstream aliases, API keys, or other privileged operational details. All other paths are forwarded to the configured default upstream with the original method, path, query string, and body. The gateway replaces incoming `Authorization` credentials with the configured upstream API key before forwarding.

Check a running local gateway:

```powershell
lsg health
```

If runtime state is missing, `lsg health` falls back to the resolved settings file and checks the expected local URL from `server.host` and `server.port`.

## Test

```powershell
pytest
```
