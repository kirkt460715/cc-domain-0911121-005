#!/bin/sh
set -eu
python -m compileall -q rights_graph demo_support
python -m unittest discover -s tests -v
python -m rights_graph --db :memory: demo --fixture fixtures/demo.json
python demo_support/check.py
