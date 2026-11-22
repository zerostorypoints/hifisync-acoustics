# Install with Docker

The engine ships as a container image for linux/amd64 and linux/arm64:

```
ghcr.io/zerostorypoints/hifisync-acoustics:<version>
ghcr.io/zerostorypoints/hifisync-acoustics:latest
```

Image tags carry no `v`: release `v0.1.0` is published as `0.1.0`. `latest` follows the newest
release that is not a pre-release.

It listens on port 8765 and keeps everything under `/data`. The process runs as UID/GID 10001,
not root.

## Run

Copy [`compose.example.yaml`](../compose.example.yaml) to the NAS, next to an empty `data`
directory, and start it:

```
mkdir data && sudo chown 10001:10001 data
docker compose -f compose.example.yaml up -d
```

The compose file uses `network_mode: host`. The engine advertises itself over mDNS
(`_hifisync-acoustics._tcp.local.`), and multicast does not reach a container on a bridge
network. Host networking does not work on Docker Desktop (macOS and Windows). Without host networking, publish the port instead (`-p 8765:8765`) and start the
engine with `--no-zeroconf`; clients then need its address.

Plain `docker run`:

```
docker run -d --name hifisync-acoustics --network host --restart unless-stopped \
  -v /path/to/data:/data ghcr.io/zerostorypoints/hifisync-acoustics:latest
```

Check it with `curl http://<nas>:8765/api/v1/health`, which needs no token. The image has a
health check on the same route, so `docker ps` shows `healthy` after a few seconds.

## First token

Every route except `/health` needs a token. Create one inside the running container:

```
docker exec <container> hifisync-acoustics token create <name>
docker exec <container> hifisync-acoustics token create <name> --role ha
```

The token is printed once on standard output and cannot be shown again. Use role `ha` for the
Home Assistant integration and the default role `app` for every other client. List and revoke
tokens with `token list` and `token revoke <name>`. See [api.md](api.md#tokens).

## Data directory

`/data` holds the SQLite database (rooms, points, nodes, calibrations, sessions, token
digests) and the uploaded recordings. Mount it from the host so it survives upgrades. If the
mount is a bind mount, its owner must be UID 10001 or the engine cannot write to it.

Backup: stop the container, copy the whole directory, start it again.

```
docker stop <container>
tar -C /path/to -czf hifisync-acoustics-backup.tar.gz data
docker start <container>
```

Restore by unpacking the archive into an empty directory with the same ownership and starting
the container on it. Tokens are part of the data directory, so a restored engine accepts the
tokens that existed when the backup was made.

## Upgrade

```
docker compose pull
docker compose up -d
```

or, with `docker run`, pull the new tag, remove the container and start it again with the same
`-v` mount. The data directory is not touched. Take a backup first when moving between
versions. Sessions left in `analysing` by the stopped process become `failed` with the reason
`interrupted` at the next start.
