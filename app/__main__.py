import atexit
import logging
import os

from waitress import serve

from .web import create_app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app(start_engine=True)
atexit.register(app.extensions["engine"].stop)
serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threads=8)
