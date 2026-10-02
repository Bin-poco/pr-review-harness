"""Capture the prepared request locally; no HTTP connection is made."""

import requests
from requests.adapters import BaseAdapter


class Capture(BaseAdapter):
    def send(self, request, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = b""
        response.request = request
        return response

    def close(self):
        pass


session = requests.Session()
session.trust_env = False
session.mount("http://", Capture())
request = requests.Request(
    "GET", "http://example.invalid/", headers={"Proxy-Authorization": "Bearer marker"}
).prepare()
response = session.send(request, allow_redirects=False)
assert response.request.headers.get("Proxy-Authorization") == "Bearer marker", dict(
    response.request.headers
)
