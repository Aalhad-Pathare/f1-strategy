"""Lambda entrypoint: adapts the FastAPI app to the Lambda event model.

`lifespan="off"` skips ASGI startup/shutdown. That is correct for this image:
the only startup work is launching ingest workers, which is disabled here
(F1_INGEST=off), and skipping it keeps cold starts down.
"""

from mangum import Mangum

from api import app

handler = Mangum(app, lifespan="off")
