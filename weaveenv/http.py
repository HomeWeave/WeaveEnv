import json
import logging
import os

from bottle import Bottle, static_file, request, response


logger = logging.getLogger(__name__)


class WeaveHTTPServer(Bottle):
    def __init__(self, modules, static_path="static"):
        super().__init__()
        self.static_path = static_path

        self.route("/")(self.handle_root)
        self.route("/static/<path:path>")(self.handle_static)

        for prefix, module in modules:
            for method, path_suffix, callback in module.get_registrations():
                path = os.path.join("/api", prefix.lstrip("/"), path_suffix)
                logger.info("Registering: %s at %s", callback, path)
                self.route(path, method)(self.handle_api(method, callback))

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
                params = request.forms
            try:
                status_code, resp = callback(params)
                return return_response(status_code, resp)
            except Exception as e:
                logger.exception("Internal server error.")
                return return_response(500, {"error": "Internal Server Error."})
        return process_request
