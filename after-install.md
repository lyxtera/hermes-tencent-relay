# after-install.md

Thank you for installing **Hermes Tencent Relay**!

This plugin relays Hermes to **your** [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) Memory Core. Deploy the hub first — see [INSTALL.md](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/INSTALL.md).

> If Memory Core already runs on this machine at `127.0.0.1:8420`, you don't need this relay. Skip to `hermes memory setup memory_tencentdb`.

## Next Steps

```bash
# 1. Configure relay → your Memory Core URL
hermes tencent-relay setup

# 2. Start the relay
hermes tencent-relay start

# 3. Verify
hermes tencent-relay health

# 4. Configure Hermes memory (interactive wizard)
hermes memory setup memory_tencentdb

# 5. Optional: auto-start on boot
hermes tencent-relay enable
```

## `hermes memory setup memory_tencentdb`

Install the provider from the hub repo if it isn't listed yet:

```bash
ln -sf /path/to/TencentDB-Agent-Memory/hermes-plugin/memory/memory_tencentdb \
      ~/.hermes/plugins/memory_tencentdb
```

Then run the wizard. When using this relay, use:

| Prompt | Value |
|---|---|
| Gateway command | `/bin/true` |
| Gateway host | `127.0.0.1` |
| Gateway port | `8420` (or your relay listen port) |
| Gateway API key | hub `user_key` from the admin panel |
| LLM fields | your hub's LLM credentials |

## Connection modes

**Direct** — enter your Memory Core URL during setup (`http://host:8420` or `https://…`). Leave Cloudflare fields blank.

**Cloudflare Access** — same URL plus Service Token ID/Secret if your hub is behind Cloudflare Tunnel.

## Troubleshooting

- `upstream URL not configured` → run `hermes tencent-relay setup`
- `hermes tencent-relay health` fails → check the hub is reachable; test `curl <your-upstream-url>/health`
- `Connection refused` → relay isn't running — `hermes tencent-relay start`
- Logs: `~/.hermes/state/hermes-tencent-relay.log`

## Uninstall

```bash
hermes tencent-relay disable
hermes plugins uninstall hermes-tencent-relay
```
