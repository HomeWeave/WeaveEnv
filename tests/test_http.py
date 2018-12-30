import os
import time
from contextlib import contextmanager
from threading import Thread

import requests

from weaveenv.http import WeaveHTTPServer


@contextmanager
def http_run_wrapper(http, port):
    thread = Thread(target=http.run, kwargs=dict(host="localhost", port=port))
    thread.start()

    while True:
        try:
            requests.get("http://localhost:15000/random-url")
            break
        except IOError:
            time.sleep(1)
            continue
    yield

    http.stop()
    thread.join()


class TestWeaveHTTPServer(object):
    def test_static_files(self):
        this_file = __file__
        http = WeaveHTTPServer([], os.path.dirname(this_file))

        with http_run_wrapper(http, 15000):
            url = "http://localhost:15000/static/" + os.path.basename(this_file)
            response = requests.get(url)

        with open(__file__) as f:
            assert response.text == f.read()

    def test_handle_api_get(self):
        class DummyModule():
            def get_registrations(self):
                return [("GET", "/test", lambda x: (200, x.pop("a")))]
        modules = [
            ("/dummy", DummyModule())
        ]

        http = WeaveHTTPServer(modules, ".")

        with http_run_wrapper(http, 15000):
            url = "http://localhost:15000/api/dummy/test?a=23"
            assert requests.get(url).json() == "23"

    def test_handle_api_post(self):
        class DummyModule():
            def get_registrations(self):
                return [("POST", "/test", lambda x: (200, x.pop("a")))]
        modules = [
            ("/dummy", DummyModule())
        ]

        http = WeaveHTTPServer(modules, ".")

        with http_run_wrapper(http, 15000):
            url = "http://localhost:15000/api/dummy/test"
            assert requests.post(url, json={"a": 23}).json() == 23
