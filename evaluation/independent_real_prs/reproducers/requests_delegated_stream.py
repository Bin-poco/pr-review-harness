"""File wrappers using __getattr__ must be treated as rewindable upload streams."""

import io

import requests


class DelegatingStream:
    def __init__(self):
        self.stream = io.BytesIO(b"example payload")

    def __getattr__(self, name):
        return getattr(self.stream, name)


stream = DelegatingStream()
request = requests.Request("PUT", "https://example.invalid/upload", data=stream).prepare()
assert request.body is stream
assert request._body_position == 0
