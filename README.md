# Testing results cache for cardano-node-tests

Cache testing results from test runs so failures can be re-tested without re-running whole test run.

## Setup

Installation steps:

```text
# create python virtual env
python3 -m venv .env
# activate python virtual env
. .env/bin/activate
# update python virtual env
python3 -m pip install --upgrade pip
python3 -m pip install --upgrade wheel
python3 -m pip install --upgrade virtualenv
virtualenv --upgrade-embed-wheels
# install package
python3 -m pip install --upgrade --upgrade-strategy eager -e .
```

Run gunicorn etc. - TBD
