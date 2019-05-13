import json
import logging
import os
from wsgiref.simple_server import make_server, WSGIRequestHandler

from bottle import Bottle, ServerAdapter, static_file, request, response


logger = logging.getLogger(__name__)


# From: https://stackoverflow.com/a/16056443/227884
class MyWSGIRefServer(ServerAdapter):
    server = None

    def run(self, handler):
        if self.quiet:
            class QuietHandler(WSGIRequestHandler):
                def log_request(*args, **kw): pass
            self.options['handler_class'] = QuietHandler
        self.server = make_server(self.host, self.port, handler, **self.options)
        self.server.serve_forever()

    def stop(self):
        self.server.server_close()
        self.server.shutdown()


class WeaveHTTPServer(Bottle):
    def __init__(self, modules, static_path="static"):
        super(WeaveHTTPServer, self).__init__()
        self.server = None
        self.static_path = static_path

        self.route("/")(self.handle_root)
        self.route("/static/<path:path>")(self.handle_static)

        for prefix, module in modules:
            for method, path_suffix, callback in module.get_registrations():
                path = os.path.join("/api", prefix.lstrip("/"),
                                    path_suffix.lstrip("/"))
                logger.info("Registering: %s at %s", callback, path)
                self.route(path, method)(self.handle_api(method, callback))

    def run(self, host="", port=15000):
        self.server = MyWSGIRefServer(host=host, port=port)
        super().run(server=self.server)

    def stop(self):
        self.server.stop()

    def handle_root(self):
        return self.handle_static("/index.html")

    def handle_static(self, path):
        return static_file(path, root=os.path.join(self.static_path))

    def handle_api(self, method, callback):
        def return_response(code, obj):
            response.status = code
            response.content_type = 'application/json'
            return json.dumps(obj)

        def process_request():
            if method == "POST":
                params = json.load(request.body)
            elif method == "GET":
                params = request.query
            try:
                status_code, resp = callback(params)
                return return_response(status_code, resp)
            except Exception:
                logger.exception("Internal server error.")
                return return_response(500, {"error": "Internal Server Error."})
        return process_request
