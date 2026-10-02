"""A Python script's __main__ module has no import spec."""

from flask import Flask

app = Flask(__name__)
assert app.instance_path
