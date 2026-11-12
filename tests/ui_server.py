"""Local UI test server: no scheduler, no automatic pushes, loopback only."""

import os

from waitress import serve

from app.web import create_app


app = create_app(data_dir=os.environ["TEST_DATA_DIR"], password=os.environ["TEST_PASSWORD"])
if os.environ.get("TEST_PRELOAD_MARKET") == "true":
    # First-run examples have no recipients. Real data, no fabricated chart values.
    app.extensions["engine"].run_once(manual=True)
serve(app, host="127.0.0.1", port=18080)
