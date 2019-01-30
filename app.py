import eventlet
eventlet.monkey_patch()  # NOLINT

import logging
import os
import sys

from weaveenv.http import WeaveHTTPServer
from weaveenv.plugins import PluginManager


logging.basicConfig()


def handle_main():
    plugin_manager = PluginManager(os.environ["PLUGINS_DIR"])
    http_modules = [
        ("/plugins", plugin_manager),
    ]
    plugin_manager.start()
    http = WeaveHTTPServer(http_modules)

    http.run(port=15000, host="")
