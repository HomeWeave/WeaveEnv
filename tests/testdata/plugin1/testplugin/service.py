from threading import Event

from weavelib.services import BasePlugin


class TestPluginService(BasePlugin):
    def __init__(self, *args, **kwargs):
        super(TestPluginService, self).__init__(*args, **kwargs)
        self.exited = Event()

    def on_service_start(self, *args, **kwargs):
        self.notify_start()
        self.exited.wait()

    def on_service_stop(self):
        self.exited.set()
