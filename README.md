# Testing results cache for cardano-node-tests

Cache testing results from test runs so failures can be re-tested without re-running whole test run.

## Install

Create `tcache` user and group

```sh
sudo groupadd tcache
sudo useradd --gid tcache --create-home --comment "testing cache API" tcache
```

Switch to `tcache` user

```sh
sudo -i -u tcache
```

Clone the repo

```sh
git clone https://github.com/mkoura/testing-results-cache.git
cd testing-results-cache
```

Create and activate python virtual env

```sh
python3 -m venv .env
. .env/bin/activate
```

Update python virtual env

```sh
python3 -m pip install --upgrade pip
python3 -m pip install --upgrade wheel
python3 -m pip install --upgrade virtualenv
virtualenv --upgrade-embed-wheels
```

Install the package

```sh
python3 -m pip install --upgrade --upgrade-strategy eager -e .
```

## Setup

Initialize database

```sh
flask --app testing_results_cache.app:create_app init-db
```

Add user(s)

```sh
$ flask --app testing_results_cache.app:create_app add-user --username team
Password:
Repeat for confirmation:
Added user team.
```

## Run the service

Copy & edit `start_service.sh`

```sh
cp examples/start_service.sh .
vim start_service.sh
```

Create systemd unit file and start the service

```sh
sudo cp examples/tcache.service /etc/systemd/system/tcache.service
sudo vim /etc/systemd/system/tcache.service
sudo systemctl daemon-reload
sudo systemctl enable --now tcache
```

Setup a proxy HTTP server (e.g. [Caddy](https://caddyserver.com/)) and point it to the service.

For Caddy, the `/etc/caddy/Caddyfile` would look like

```text
tcache-3-74-115-22.nip.io {
        reverse_proxy /results/* 127.0.0.1:8000
}
```
