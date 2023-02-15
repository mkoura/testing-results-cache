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

## Run the service for local development

Make sure to activate python virtual env and finish setup steps first.

```sh
flask --app 'testing_results_cache.app:create_app()' --debug run
```

## Queries

Submit results:

```sh
curl -X PUT --fail-with-body -u username:password http://localhost:5000/results/testrun1/1/import -F "junitxml=@/home/user/path/to/junit.xml"
```

Get passed tests in given testrun:

```sh
curl -u username:password http://localhost:5000/results/testrun1/passed
```

Get passed tests in given testrun formatted as pytest nodeid:

```sh
curl -u username:password http://localhost:5000/results/testrun1/pypassed
```

Get tests that need re-run in given testrun:

```sh
curl -u username:password http://localhost:5000/results/testrun1/rerun
```

Get tests formatted as pytest nodeid that need re-run in given testrun:

```sh
curl -u username:password http://localhost:5000/results/testrun1/pyrerun
```
