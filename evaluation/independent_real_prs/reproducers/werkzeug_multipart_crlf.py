"""A file upload ending near a 64 KiB parser boundary must not prefix the next field."""

import io

from werkzeug.formparser import MultiPartParser

BOUNDARY = "----WebKitFormBoundaryXyz789"
PREFIX = (
    f"--{BOUNDARY}\r\n"
    'Content-Disposition: form-data; name="amount"\r\n\r\n550\r\n'
    f"--{BOUNDARY}\r\n"
    'Content-Disposition: form-data; name="image"; filename="slip.jpeg"\r\n'
    "Content-Type: image/jpeg\r\n\r\n"
).encode()
SUFFIX = (
    f"\r\n--{BOUNDARY}\r\n"
    'Content-Disposition: form-data; name="text_field"\r\n\r\n'
    f"A73290_SN3\r\n--{BOUNDARY}--\r\n"
).encode()
body = PREFIX + b"\xff" * 65226 + SUFFIX
form, _files = MultiPartParser(buffer_size=65536).parse(
    io.BytesIO(body), BOUNDARY.encode(), len(body)
)
assert form["text_field"] == "A73290_SN3", repr(form["text_field"])
