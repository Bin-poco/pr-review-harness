"""Stringifying request headers must include values read from WSGI environ."""

from werkzeug.datastructures import EnvironHeaders

headers = EnvironHeaders({"HTTP_X_EVAL": "marker"})
assert "marker" in str(headers), repr(str(headers))
