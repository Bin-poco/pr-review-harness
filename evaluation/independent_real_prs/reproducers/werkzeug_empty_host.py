"""A missing Host header is valid for HTTP/0.9 and HTTP/1.0 requests."""

from werkzeug.sansio.utils import get_host

assert get_host("http", None) == ""
