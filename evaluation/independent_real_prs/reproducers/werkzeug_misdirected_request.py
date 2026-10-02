"""The new HTTP 421 exception should be discoverable by status code."""

from werkzeug.exceptions import MisdirectedRequest, default_exceptions

error = MisdirectedRequest()
assert error.code == 421
assert default_exceptions[421] is MisdirectedRequest
assert error.get_response().status_code == 421
